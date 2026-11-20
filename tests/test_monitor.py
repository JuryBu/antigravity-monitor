"""Comprehensive unit tests for Antigravity Monitor core engine, SQLite reader, config, and shortcuts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core.account_ledger import AccountSwitchLedger
from core.config_store import ConfigStore
from core.log_monitor import (
    AntigravityMonitorEngine,
    LanguageServerLogTailer,
    classify_steps,
    compute_cli_exit_code,
    compute_pressure,
    format_detail_text_report,
    format_json_snapshot,
    format_text_summary,
    mask_email,
    short_email_label,
    truncate_display_width,
)
from core.sqlite_reader import ConversationDbReader, parse_gen_metadata_row
from core.startup_manager import install_desktop_shortcut, is_startup_enabled, set_startup_enabled
from ui.capsule_app import compute_steps_color
from ui.icon_builder import build_tray_icon


def _varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_varint_field(fn: int, val: int) -> bytes:
    return _varint((fn << 3) | 0) + _varint(val)


def _pb_bytes_field(fn: int, payload: bytes) -> bytes:
    return _varint((fn << 3) | 2) + _varint(len(payload)) + payload


def _build_fake_gen_blob(
    uncached_in: int,
    cached_in: int,
    total_out: int,
    ttft_s: int,
    strm_s: int,
    resp_id: str = "demo_resp_0001XYZ_stub",
    max_ctx: int = 512000,
) -> bytes:
    usage = (
        _pb_varint_field(2, uncached_in)
        + _pb_varint_field(3, total_out)
        + _pb_varint_field(5, cached_in)
        + _pb_bytes_field(11, resp_id.encode("utf-8"))
    )
    ttft_pb = _pb_varint_field(1, ttft_s) + _pb_varint_field(2, 500_000_000)
    strm_pb = _pb_varint_field(1, strm_s) + _pb_varint_field(2, 0)
    m10 = _pb_varint_field(4, max_ctx)
    m9 = _pb_bytes_field(10, m10)
    m1 = (
        _pb_bytes_field(4, usage)
        + _pb_bytes_field(9, m9)
        + _pb_bytes_field(11, ttft_pb)
        + _pb_bytes_field(12, strm_pb)
    )
    return _pb_bytes_field(1, m1) + _pb_bytes_field(21, b"Gemini 4 Neon (XHigh)")


class TestConfigStore(unittest.TestCase):
    def test_atomic_save_and_sanitize(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            store = ConfigStore(p)
            saved = store.save({"theme": "invalid", "poll_interval_ms": 100, "window_x": "-240", "topmost": False})
            self.assertEqual(saved["theme"], "midnight_blue")
            self.assertEqual(saved["poll_interval_ms"], 1000)
            self.assertEqual(saved["window_x"], -240)
            self.assertEqual(saved["window_alpha"], 0.95)
            self.assertFalse(saved["topmost"])
            reloaded = store.load()
            self.assertEqual(reloaded, saved)
            self.assertEqual(list(Path(td).glob("*.tmp*")), [])


class TestSqliteReader(unittest.TestCase):
    def test_protobuf_decode_and_lru_cache_invalidation(self) -> None:
        blob1 = _build_fake_gen_blob(2000, 98000, 1000, ttft_s=6, strm_s=10)
        parsed = parse_gen_metadata_row(7, blob1)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.uncached_in, 2000)
        self.assertEqual(parsed.cached_in, 98000)
        self.assertEqual(parsed.total_in, 100000)
        self.assertAlmostEqual(parsed.ttft_sec or 0.0, 6.5, places=2)
        self.assertAlmostEqual(parsed.tps or 0.0, 100.0, places=2)
        self.assertEqual(parsed.max_context_tokens, 512000)
        self.assertEqual(parsed.shard_tag, "01XY")

        # Negative/overflow varint duration should safely return None instead of 1e19s
        bad_dur_blob = _pb_bytes_field(1, _pb_bytes_field(11, _pb_varint_field(1, (1 << 64) - 1)))
        bad_parsed = parse_gen_metadata_row(8, bad_dur_blob)
        self.assertIsNotNone(bad_parsed)
        assert bad_parsed is not None
        self.assertIsNone(bad_parsed.ttft_sec)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "demo_conv.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, blob1))
            conn.commit()
            conn.close()

            reader = ConversationDbReader(Path(td), max_cache_entries=2)
            s1 = reader.read("demo_conv")
            s2 = reader.read("demo_conv")
            self.assertIsNotNone(s1)
            self.assertIs(s1, s2)

            time.sleep(0.02)
            blob2 = _build_fake_gen_blob(120000, 0, 500, ttft_s=48, strm_s=20)
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (2, blob2))
            conn.commit()
            conn.close()

            s3 = reader.read("demo_conv")
            self.assertIsNotNone(s3)
            assert s3 is not None and s3.latest is not None
            self.assertEqual(s3.latest.idx, 2)
            self.assertAlmostEqual(s3.latest.tps or 0.0, 25.0, places=2)


class TestLogMonitorLogic(unittest.TestCase):
    def test_mask_email_and_plain_username(self) -> None:
        self.assertEqual(mask_email("User alice99@example.com failed"), "User al***9@example.com failed")
        self.assertEqual(mask_email("ab@test.org"), "a***@test.org")
        self.assertEqual(short_email_label("plainuser"), "pl***r")

    def test_classify_steps_filters_normal_zeros_and_threshold_crossing(self) -> None:
        base = 1_795_000_000.0
        steps = [
            {"step_index": 0, "timestamp": base, "model": "gemini-4-neon-xhigh", "input_tokens": 18000, "cache_read_tokens": 0},
            # Crossing from <32K to >=32K for the first time -> first_cacheable (not abnormal!)
            {"step_index": 1, "timestamp": base + 15, "model": "gemini-4-neon-xhigh", "input_tokens": 45000, "cache_read_tokens": 0},
            {"step_index": 2, "timestamp": base + 30, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 45000},
            # Idle > 180s -> idle_rewarm
            {"step_index": 3, "timestamp": base + 260, "model": "gemini-4-neon-xhigh", "input_tokens": 55000, "cache_read_tokens": 0},
            # Model switch -> model_switch
            {"step_index": 4, "timestamp": base + 280, "model": "gemini-4-carbon-high", "input_tokens": 60000, "cache_read_tokens": 0},
            # Compact drop > 25% -> compact_reset
            {"step_index": 5, "timestamp": base + 295, "model": "gemini-4-carbon-high", "input_tokens": 35000, "cache_read_tokens": 0},
            # Same model, short gap, >= 32K, cr=0 -> abnormal_miss #1 & #2
            {"step_index": 6, "timestamp": base + 310, "model": "gemini-4-carbon-high", "input_tokens": 42000, "cache_read_tokens": 0},
            {"step_index": 7, "timestamp": base + 325, "model": "gemini-4-carbon-high", "input_tokens": 46000, "cache_read_tokens": 0},
        ]
        res = classify_steps(steps, idle_rewarm_sec=180)
        self.assertEqual(res[0].step_index, 0)
        self.assertEqual([r.reason for r in res], [
            "first_turn",
            "first_cacheable",
            "cached",
            "idle_rewarm",
            "model_switch",
            "compact_reset",
            "abnormal_zero",
            "abnormal_zero",
        ])
        self.assertEqual([r.classification for r in res[-2:]], ["abnormal_miss", "abnormal_miss"])

    def test_go_slash_dates_partial_lines_and_sliding_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_p = Path(td) / "language_server.log"
            now = time.time()
            # Two days ago at the EXACT same clock time (Go slash date format & glog MMDD format)
            two_days_ago = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now - 2 * 86400))
            two_days_mmdd = time.strftime("%m%d %H:%M:%S", time.localtime(now - 2 * 86400))
            old_today = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now - 3600))
            rec_go = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now - 60))
            rec_glog = time.strftime("%m%d %H:%M:%S", time.localtime(now - 45))
            log_p.write_text(
                f"{two_days_ago} ERROR 403 PERMISSION_DENIED TOS_VIOLATION old-account\n"
                f"ERROR: logging before google.Init: I{two_days_mmdd}.111111 22456 run.go:315] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available)\n"
                f"{old_today} ERROR status=503 No capacity available\n"
                f"{rec_go} WARN 127.0.0.1:55503 TLS handshake error\n"
                f"ERROR: logging before google.Init: W{rec_glog}.350309     607 extract_searchable_content.go:54] conversationsearch: loading trajectory\n"
                f"ERROR: logging before google.Init: E{rec_glog}.850324    8711 cascade_manager.go:2064] LoadTrajectory LoadUnsafe failed for demo01: not found\n"
                f"ERROR: logging before google.Init: I{rec_glog}.282503       1 server.go:3993] Wrote hub discovery file: demo_hub.json\n"
                f"ERROR: logging before google.Init: I{rec_glog}.745168    7650 runner.go:898] Monitor sir_flash_lite FINISHED: reason=\"debugging a 503 error\"\n"
                f"ERROR: logging before google.Init: I{rec_glog}.101751     189 server.go:5878] [RemoteControl] Staying disconnected: user setting is off\n"
                f"ERROR: logging before google.Init: W{rec_glog}.100000     100 code_assist_client.go:464] AICode FetchConfig failed: context deadline exceeded\n"
                f"ERROR: logging before google.Init: I{rec_glog}.200000   22456 run.go:315] Run: attempt 1 failed (context canceled)\n"
                f"ERROR: logging before google.Init: I{rec_glog}.222222   22456 run.go:315] Run: attempt 1 failed (unexpected EOF), retrying in 1.5s\n"
                f"ERROR: logging before google.Init: I{rec_glog}.333333   22456 run.go:315] Run: attempt 2 failed (UNAVAILABLE (code 503): The service is currently unavailable.), partial-without-newline",
                encoding="utf-8",
            )
            tailer = LanguageServerLogTailer(log_p)
            tailer.poll(now_ts=now)
            recent, today, notable = tailer.get_counts(now, window_min=30)
            self.assertEqual(recent["403"], 0)
            self.assertEqual(today["403"], 0)
            self.assertEqual(recent["503"], 0)   # Microsecond .350309/.850324/.282503 & sir_flash_lite & 1h-old 503 must NOT match!
            self.assertEqual(recent["timeout"], 0)
            self.assertEqual(recent["retry"], 1)  # Only genuine unexpected EOF retry; context canceled excluded!
            self.assertEqual(notable, [])

            # Complete the partial line with '\n' and confirm incremental pickup of real "(code 503)" format
            with log_p.open("a", encoding="utf-8") as fh:
                fh.write("\n")
            tailer.poll(now_ts=now + 2)
            recent_b, _, notable_b = tailer.get_counts(now + 2, window_min=30)
            self.assertEqual(recent_b["503"], 1)
            self.assertEqual(len(notable_b), 1)
            # Confirm real glog timestamp was extracted instead of stamping fallback now
            self.assertNotEqual(notable_b[0].timestamp, now + 2)

            # Append real glog 5m stream idle timeout + real 403 TOS_VIOLATION and verify exact classification
            with log_p.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"ERROR: logging before google.Init: I{rec_glog}.429371   22456 run.go:315] Run: attempt 1 failed (request failed: stream idle timeout (5m0s): context deadline exceeded), retrying in 5.4s\n"
                    f"ERROR: logging before google.Init: E{rec_glog}.555555   22456 client.go:200] onboardUser failed: 403 PERMISSION_DENIED (reason: TOS_VIOLATION)\n"
                )
            tailer.poll(now_ts=now + 3)
            recent_c, _, notable_c = tailer.get_counts(now + 3, window_min=30)
            self.assertEqual(recent_c["timeout"], 1)
            self.assertEqual(recent_c["403"], 1)
            self.assertEqual(recent_c["retry"], 1)  # 5m timeout classified as timeout, not double-counted as retry

            # Simulate client restart truncating log file
            log_p.write_text(f"{rec_go} ERROR stream idle timeout\n", encoding="utf-8")
            tailer.poll(now_ts=now + 5)
            recent2, _, _ = tailer.get_counts(now + 5, window_min=30)
            self.assertEqual(recent2["timeout"], 1)
            self.assertEqual(recent2["retry"], 0)
            self.assertEqual(recent2["503"], 0)

    def test_offline_client_enters_idle_and_silences_stale_log_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "idle1234-5678" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"

            now = time.time()
            # Pre-shutdown real errors from 4 minutes ago, while Antigravity is currently closed (no live hub)
            rec_glog = time.strftime("%m%d %H:%M:%S", time.localtime(now - 240))
            ls_log.write_text(
                f"ERROR: logging before google.Init: I{rec_glog}.429371   22456 run.go:315] Run: attempt 1 failed (request failed: stream idle timeout (5m0s): context deadline exceeded)\n"
                f"ERROR: logging before google.Init: I{rec_glog}.500000   22456 run.go:315] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available)\n"
                f"ERROR: logging before google.Init: I{rec_glog}.600000   22456 run.go:315] Run: attempt 2 failed (UNAVAILABLE (code 503): No capacity available)\n",
                encoding="utf-8",
            )
            t_old = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 300))
            (brain / "transcript.jsonl").write_text(
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t_old, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 98000, "output_tokens": 500}) + "\n",
                encoding="utf-8",
            )

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing_cockpit.sqlite",
                }
            )
            snap = engine.poll(now_ts=now)
            self.assertFalse(snap.account.online)
            self.assertEqual(snap.grade, "IDLE")
            self.assertEqual(snap.level, "green")
            self.assertEqual(snap.alerts, [])
            # Even on a subsequent poll with another trailing log line while still offline, stay IDLE with 0 alerts
            snap2 = engine.poll(now_ts=now + 5)
            self.assertEqual(snap2.grade, "IDLE")
            self.assertEqual(snap2.alerts, [])

    def test_account_ledger_splits_daily_steps_and_suppresses_switch_first_cold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()
            ledger_p = root / "account_ledger.json"

            now = time.time()
            t_acc1 = now - 3600
            t_acc2 = now - 600
            iso1 = time.strftime("%Y-%m-%dT%H:%M:%S.123456789+08:00", time.localtime(t_acc1))
            iso2 = time.strftime("%Y-%m-%dT%H:%M:%S.987654321+08:00", time.localtime(t_acc2))
            (ck_logs / "app.log.2026-11-20").write_text(
                f"{iso1}  INFO [Antigravity 2.0] 写入系统凭据: alpha123@example.com\n"
                f"{iso2}  INFO [Antigravity 2.0] 写入系统凭据: beta9876@example.com\n",
                encoding="utf-8",
            )

            ledger = AccountSwitchLedger(ledger_path=ledger_p, cockpit_logs_dir=ck_logs)
            ledger.observe_online("be***6", now)
            # Ledger on disk must contain ONLY masked labels, never raw emails
            if ledger_p.is_file():
                raw_txt = ledger_p.read_text(encoding="utf-8")
                self.assertNotIn("alpha123@example.com", raw_txt)
                self.assertNotIn("beta9876@example.com", raw_txt)

            tl = ledger.get_timeline(now - 7200)
            self.assertEqual([lbl for _, lbl in tl], ["al***3", "be***6"])
            # Attribution separates steps cleanly across the switch timestamp
            self.assertEqual(ledger.attribute_timestamp(t_acc1 + 300, tl), "al***3")
            self.assertEqual(ledger.attribute_timestamp(t_acc2 + 100, tl), "be***6")

            # Same conversation crossing an account switch: first cr=0 after switch is expected_cold ("account_switch")
            switches = ledger.get_switch_timestamps(now - 7200)
            raw_steps = [
                {"step_index": 1, "timestamp": t_acc2 - 30, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 120000},
                {"step_index": 2, "timestamp": t_acc2 + 20, "model": "gemini-4-neon-xhigh", "input_tokens": 124000, "cache_read_tokens": 0},
                {"step_index": 3, "timestamp": t_acc2 + 60, "model": "gemini-4-neon-xhigh", "input_tokens": 126000, "cache_read_tokens": 0},
            ]
            classified = classify_steps(raw_steps, idle_rewarm_sec=180, account_switches=switches)
            self.assertEqual(classified[1].classification, "expected_cold")
            self.assertEqual(classified[1].reason, "account_switch")
            self.assertEqual(classified[2].classification, "abnormal_miss")

    def test_recent_ttft_series_avg_and_max(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "ttft1234-5678" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")

            now = time.time()
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 30))
            t1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 10))
            rows = [
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t0, "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 600},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": t1, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000, "output_tokens": 800},
            ]
            (brain / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            conn = sqlite3.connect(convs / "ttft1234-5678.db")
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, _build_fake_gen_blob(50000, 0, 600, ttft_s=10, strm_s=4)))
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (2, _build_fake_gen_blob(2000, 50000, 800, ttft_s=30, strm_s=5)))
            conn.commit()
            conn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                }
            )
            snap = engine.poll(now_ts=now)
            self.assertEqual(len(snap.primary_recent_ttfts), 2)
            # _build_fake_gen_blob appends 0.5s nanos -> 10.5s & 30.5s (avg=20.5s, max=30.5s)
            self.assertAlmostEqual(snap.primary_ttft_avg or 0.0, 20.5, places=2)
            self.assertAlmostEqual(snap.primary_ttft_max or 0.0, 30.5, places=2)
            summary = format_text_summary(snap)
            self.assertIn("近10步均 20.5s / 峰 30.5s", summary)

    def test_intermittent_t1_cannot_wash_to_t0_on_single_hit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "wash1234-5678" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")

            now = time.time()
            # Step 0 warm seed + 10 steps: 4 misses, 1 lucky hit, 4 misses, 1 lucky hit (8/10 abnormal misses!)
            pattern = [True, False, False, False, False, True, False, False, False, False, True]
            rows = []
            for idx, is_hit in enumerate(pattern):
                ts_s = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - (len(pattern) - idx) * 20))
                rows.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": idx + 1,
                    "created_at": ts_s,
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 2000 if is_hit else 180000,
                    "cache_read_tokens": 180000 if is_hit else 0,
                    "output_tokens": 600,
                })
            tp = brain / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                }
            )
            snap = engine.poll(now_ts=now)
            # Even though latest step hit 98.9%, 8/10 abnormal misses MUST stay T1 red, never wash to T0 green!
            self.assertEqual(snap.grade, "T1")
            self.assertEqual(snap.level, "red")
            self.assertEqual(snap.max_abnormal_recent10, 8)
            self.assertIn("频繁掉缓存", snap.headline)

            # Now test moderate recovery window: 2 abnormal misses among 10 steps, latest 2 steps hit -> WARN (not T0)
            pattern2 = [True, True, True, True, True, True, True, False, False, True, True]
            rows2 = []
            for idx, is_hit in enumerate(pattern2):
                ts_s = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - (len(pattern2) - idx) * 20))
                rows2.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": idx + 1,
                    "created_at": ts_s,
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 2000 if is_hit else 120000,
                    "cache_read_tokens": 120000 if is_hit else 0,
                    "output_tokens": 600,
                })
            time.sleep(0.02)
            tp.write_text("\n".join(json.dumps(r) for r in rows2) + "\n", encoding="utf-8")
            snap2 = engine.poll(now_ts=now + 2)
            self.assertEqual(snap2.grade, "WARN")
            self.assertEqual(snap2.level, "orange")
            self.assertIn("观察恢复中", snap2.sub_headline)

    def test_switching_from_bad_account_to_healthy_does_not_inherit_t1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "swok1234-5678" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()

            now = time.time()
            t_bad_sw = now - 600
            t_good_sw = now - 60
            iso1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_bad_sw))
            iso2 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_good_sw))
            (ck_logs / "app.log.2026-11-20").write_text(
                f"{iso1}  INFO [Antigravity 2.0] 写入系统凭据: bad01@example.com\n"
                f"{iso2}  INFO [Antigravity 2.0] 写入系统凭据: good9@example.com\n",
                encoding="utf-8",
            )

            # 6 consecutive abnormal misses on bad01, then switch to good9 -> 1 switch cold + 1 warm hit
            rows = []
            for idx in range(7):
                ts_s = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_bad_sw + 60 + idx * 20))
                rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": idx + 1, "created_at": ts_s,
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 2000 if idx == 0 else 180000,
                    "cache_read_tokens": 180000 if idx == 0 else 0,
                    "output_tokens": 500,
                })
            rows.append({
                "type": "PLANNER_RESPONSE", "step_index": 8,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_good_sw + 10)),
                "model": "gemini-4-neon-xhigh", "input_tokens": 182000, "cache_read_tokens": 0, "output_tokens": 500,
            })
            rows.append({
                "type": "PLANNER_RESPONSE", "step_index": 9,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_good_sw + 35)),
                "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 182000, "output_tokens": 500,
            })
            (brain / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                }
            )
            # Simulate live online account good9
            from core.log_monitor import AccountQuotaInfo
            engine._account_cache = AccountQuotaInfo(email_masked="go***9@example.com", short_label="go***9", online=True, observed_at=now)
            engine._last_account_poll = now
            snap = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T0")
            self.assertEqual(snap.level, "green")
            self.assertEqual(snap.max_abnormal_recent10, 0)
            self.assertEqual(snap.wasted_cold_tokens_total, 0)
            self.assertEqual(snap.today_steps, 2)
            self.assertEqual(snap.total_today_steps, 9)

    def test_end_to_end_t1_invalid_freshness_dedup_and_no_offline_cockpit_leak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "demo1234-5678" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")

            # Fake Cockpit legacy binding + quota DB to verify offline state NEVER leaks unrelated account quota
            ck_dir = root / "cockpit"
            ck_dir.mkdir()
            (ck_dir / "antigravity_legacy_instances.json").write_text(
                json.dumps({"defaults": {"bindAccountId": "other_acct"}}), encoding="utf-8"
            )
            qdb = ck_dir / "platform-quota-history-v1.sqlite"
            qconn = sqlite3.connect(qdb)
            qconn.execute("CREATE TABLE quota_observations (account_id TEXT, metric TEXT, remaining REAL, observed_at REAL)")
            qconn.execute("INSERT INTO quota_observations VALUES ('other_acct', 'gemini-weekly', 88.0, ?)", (time.time(),))
            qconn.commit()
            qconn.close()

            now = time.time()
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 40))
            t1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 25))
            t2 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 10))
            rows = [
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t0, "model": "gemini-4-neon-xhigh", "input_tokens": 110000, "cache_read_tokens": 0, "output_tokens": 1000},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": t1, "model": "gemini-4-neon-xhigh", "input_tokens": 115000, "cache_read_tokens": 0, "output_tokens": 1000},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": t2, "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 1000},
            ]
            tp = brain / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            conn = sqlite3.connect(convs / "demo1234-5678.db")
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (1, _build_fake_gen_blob(120000, 0, 500, ttft_s=38, strm_s=20)))
            conn.commit()
            conn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": qdb,
                }
            )
            snap = engine.poll(now_ts=now)
            self.assertEqual(snap.grade, "T1")
            self.assertEqual(snap.level, "red")
            self.assertEqual(snap.max_consec_miss, 2)
            self.assertIsNone(snap.min_freshness_sec)
            self.assertIn("无热缓存", snap.min_freshness_invalid_reason)
            self.assertEqual(snap.primary_speed_status, "slow")
            self.assertIsNone(snap.account.weekly_pct)  # Offline must not borrow other_acct quota
            self.assertEqual(snap.alerts, [])  # First poll sets baseline

            # Append another cr=0 in the same streak -> must NOT spam another T1 alert for the same streak
            time.sleep(0.02)
            t3 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 2))
            rows.append({"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": t3, "model": "gemini-4-neon-xhigh", "input_tokens": 125000, "cache_read_tokens": 0, "output_tokens": 1000})
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            snap2 = engine.poll(now_ts=now + 2)
            self.assertEqual(snap2.max_consec_miss, 3)
            self.assertEqual(snap2.alerts, [])

            summary = format_text_summary(snap2)
            self.assertIn("T1", summary)
            self.assertIn("360.0K", summary)


class TestShortcutsAndIcons(unittest.TestCase):
    def test_startup_toggle_com_roundtrip_and_tray_icons(self) -> None:
        app_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as td:
            desk = Path(td) / "桌面 测试"
            startup = Path(td) / "Startup"
            lnk = install_desktop_shortcut(app_root, desktop_dir=desk)
            self.assertTrue(lnk.is_file())
            # Verify via PowerShell COM that Arguments preserve full Unicode path cleanly
            ps = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                f"$sc = (New-Object -ComObject WScript.Shell).CreateShortcut('{str(lnk)}'); "
                "Write-Output $sc.Arguments"
            )
            res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            self.assertEqual(res.returncode, 0)
            self.assertIn("启动反重力监控.vbs", res.stdout)

            self.assertFalse(is_startup_enabled(startup_dir=startup))
            self.assertTrue(set_startup_enabled(True, app_root, startup_dir=startup))
            self.assertTrue(is_startup_enabled(startup_dir=startup))
            self.assertFalse(set_startup_enabled(False, app_root, startup_dir=startup))
            self.assertFalse(is_startup_enabled(startup_dir=startup))

        base_png = app_root / "assets" / "app-icon.png"
        for lvl in ("green", "orange", "red"):
            im = build_tray_icon(base_png, level=lvl, size=64)
            self.assertEqual(im.size, (64, 64))
            self.assertEqual(im.mode, "RGBA")

    def test_steps_smooth_color_gradient_80_to_100_locked_red(self) -> None:
        g, r = "#34D399", "#F87171"
        self.assertEqual(compute_steps_color(0, 960, 1200, g, r), g)
        self.assertEqual(compute_steps_color(959, 960, 1200, g, r), g)
        # Exactly at 80% (960) switches cleanly to yellow
        self.assertEqual(compute_steps_color(960, 960, 1200, g, r), "#FACC15")
        # Midpoint 90% (1080) smoothly transitions through orange
        self.assertEqual(compute_steps_color(1080, 960, 1200, g, r), "#F59E0B")
        # 95% smoothly interpolates between orange and red
        mid_or = compute_steps_color(1140, 960, 1200, g, r)
        self.assertNotIn(mid_or, (g, "#FACC15", "#F59E0B", r))
        # At and above 100% locks permanently to red
        self.assertEqual(compute_steps_color(1200, 960, 1200, g, r), r)
        self.assertEqual(compute_steps_color(1800, 960, 1200, g, r), r)

    def test_subagent_classification_primary_isolation_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")
            now = time.time()

            # Main conversation (warm 200K, 30s ago -> remaining ~180s)
            b_main = root / "brain" / "main0001-1111" / ".system_generated" / "logs"
            b_main.mkdir(parents=True)
            t_main = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 30))
            (b_main / "transcript.jsonl").write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>hi</USER_REQUEST>"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": t_main, "model": "gemini-4-neon-xhigh", "input_tokens": 3000, "cache_read_tokens": 197000, "output_tokens": 500}) + "\n",
                encoding="utf-8",
            )

            # Newer retired subagent (220K, expired 260s ago, PLUS a brand-new 50K subagent 5s ago)
            b_sub1 = root / "brain" / "sub00001-2222" / ".system_generated" / "logs"
            b_sub1.mkdir(parents=True)
            t_sub1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 260))
            (b_sub1 / "transcript.jsonl").write_text(
                json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t_sub1, "model": "gemini-4-hydrogen-xhigh", "input_tokens": 5000, "cache_read_tokens": 215000, "output_tokens": 200}) + "\n",
                encoding="utf-8",
            )
            conn1 = sqlite3.connect(convs / "sub00001-2222.db")
            conn1.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn1.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
            conn1.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (b"swarm worker subagent " * 1000,))
            conn1.commit()
            conn1.close()

            b_sub2 = root / "brain" / "sub00002-3333" / ".system_generated" / "logs"
            b_sub2.mkdir(parents=True)
            t_sub2 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 5))
            (b_sub2 / "transcript.jsonl").write_text(
                json.dumps({"source": "SWARM", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t_sub2, "model": "gemini-4-hydrogen-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200}) + "\n",
                encoding="utf-8",
            )

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                }
            )
            snap = engine.poll(now_ts=now)
            # Primary model/hit stays locked on MAIN conversation (Neon XHigh), not hijacked by newer Hydrogen subagent!
            self.assertEqual(snap.active_model, "Neon XHigh")
            self.assertAlmostEqual(snap.primary_hit_rate or 0.0, 98.5, places=1)
            # Freshness tracks MAIN warm conversation (~180s), NOT expired subagent sub00001!
            self.assertEqual(snap.min_freshness_conv, "main00")
            self.assertIsNotNone(snap.min_freshness_sec)
            self.assertGreater(snap.min_freshness_sec or 0, 150)
            by_id = {c.short_id: c.is_subagent for c in snap.active_conversations}
            self.assertFalse(by_id["main00"])
            self.assertTrue(by_id["sub000"])

    def test_checkpoint_compact_overrides_idle_rewarm_and_tracks_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "cmp00001-aaaa" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")

            now = time.time()
            # Compaction took 220s (>180s idle_rewarm_sec) after CHECKPOINT -> MUST classify as compact_reset, not idle_rewarm or abnormal_zero!
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 260))
            t_chk = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 150))
            t1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 40))
            t2 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 15))
            lines = [
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}),
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 10, "created_at": t0, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 498000, "output_tokens": 500}),
                json.dumps({"type": "CHECKPOINT", "step_index": 11, "created_at": t_chk, "input_tokens": 12000, "cache_read_tokens": 500000}),
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 12, "created_at": t1, "model": "gemini-4-neon-xhigh", "input_tokens": 42000, "cache_read_tokens": 0, "output_tokens": 400}),
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 13, "created_at": t2, "model": "gemini-4-neon-xhigh", "input_tokens": 8000, "cache_read_tokens": 42000, "output_tokens": 500}),
            ]
            (brain / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=now)
            self.assertEqual(snap.grade, "T0")
            self.assertEqual(snap.pressure, "normal")
            self.assertEqual(compute_cli_exit_code(snap), 0)
            conv = snap.active_conversations[0]
            self.assertEqual(conv.recent_steps[1].reason, "compact_reset")
            self.assertEqual(conv.recent_steps[1].classification, "expected_cold")
            self.assertEqual(conv.recent_steps[1].context_delta, 42000 - 500000)
            self.assertEqual(conv.latest_context_delta, 8000)

    def test_literal_context_summary_text_without_shrink_never_triggers_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "lit00001-bbbb" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")

            now = time.time()
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 800))
            t1 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 20))
            # User mentions literal "<CONTEXT_SUMMARY>" after 13m idle; context grew 340K -> 343K -> MUST be idle_rewarm, NOT compact_reset!
            lines = [
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}),
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t0, "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 338000, "output_tokens": 300}),
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "mention <CONTEXT_SUMMARY> and CHECKPOINT in prose"}),
                json.dumps({"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": t1, "model": "gemini-4-neon-xhigh", "input_tokens": 343000, "cache_read_tokens": 0, "output_tokens": 300}),
            ]
            (brain / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=now)
            st = snap.active_conversations[0].recent_steps[-1]
            self.assertEqual(st.reason, "idle_rewarm")
            self.assertEqual(st.classification, "expected_cold")
            detail = engine.read_conversation_detail("lit00001", now_ts=now)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail.compact_count, 0)

    def test_json_contract_pressure_exit_codes_and_readonly_zero_write(self) -> None:
        self.assertEqual(compute_pressure("IDLE", 0, 960, 1200), "offline_unknown")
        self.assertEqual(compute_pressure("T0", 200, 960, 1200), "normal")
        self.assertEqual(compute_pressure("WARN", 980, 960, 1200), "warn")
        self.assertEqual(compute_pressure("WARN", 1250, 960, 1200), "red")
        self.assertEqual(compute_pressure("T1", 100, 960, 1200), "degraded")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_p = root / "account_ledger.json"
            ledger = AccountSwitchLedger(ledger_path=ledger_p, read_only=True)
            ledger.observe_online("de***o", time.time())
            # Strict read_only MUST NEVER create or touch account_ledger.json on disk!
            self.assertFalse(ledger_p.exists())

            # Verify JSON contract on offline IDLE snapshot returns exit_code=2 and masks any emails
            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "ledger_path": ledger_p,
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=time.time(), target_conversation_id="nonexistent")
            self.assertEqual(snap.pressure, "offline_unknown")
            self.assertEqual(compute_cli_exit_code(snap), 2)
            raw_json = format_json_snapshot(snap)
            parsed = json.loads(raw_json)
            self.assertEqual(parsed["schema_version"], "1.0")
            self.assertEqual(parsed["pressure"], "offline_unknown")
            self.assertEqual(parsed["exit_code"], 2)
            self.assertFalse(parsed["target_conversation_found"])
            self.assertIsNone(parsed["target_conversation"])
            for req_key in ("observed_at", "account", "grade", "level", "steps", "cache", "perf", "errors_30m", "errors_today", "alerts"):
                self.assertIn(req_key, parsed)
            self.assertFalse(ledger_p.exists())

    def test_multi_pool_quota_reset_time_and_cockpit_reset_at(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import os
        import threading

        payload = {
            "userStatus": {
                "email": "tester42@example.com",
                "userTier": {"id": "g1-pro-tier"},
                "planStatus": {"planInfo": {"planName": "Pro", "teamsTier": "TEAMS_TIER_PRO"}},
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {"label": "Gemini 4 Neon (XHigh)", "quotaInfo": {"remainingFraction": 0.96, "resetTime": "2026-11-20T12:00:00Z"}},
                        {"label": "Gemini 4 Carbon (High)", "quotaInfo": {"remainingFraction": 0.96, "resetTime": "2026-11-20T12:00:00Z"}},
                        {"label": "Claude Opus 5.5", "quotaInfo": {"remainingFraction": 0.24, "resetTime": "2026-11-25T08:00:00Z"}},
                    ]
                },
            }
        }

        class _MockHub(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt: str, *_args: object) -> None:
                return

        srv = HTTPServer(("127.0.0.1", 0), _MockHub)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                daemon = root / "daemon"
                daemon.mkdir()
                (daemon / "hub_live.json").write_text(
                    json.dumps({"pid": os.getpid(), "httpPort": port, "csrfToken": "tok_demo"}),
                    encoding="utf-8",
                )
                ck_dir = root / "cockpit"
                ck_dir.mkdir()
                (ck_dir / "accounts.json").write_text(
                    json.dumps({"accounts": [{"id": "acc_demo_1", "email": "tester42@example.com"}]}),
                    encoding="utf-8",
                )
                qdb = ck_dir / "platform-quota-history-v1.sqlite"
                now_ms = int(time.time() * 1000)
                reset_wk_ms = now_ms + 3 * 86400 * 1000
                qconn = sqlite3.connect(qdb)
                qconn.execute(
                    "CREATE TABLE quota_observations (account_id TEXT, metric TEXT, remaining REAL, observed_at REAL, reset_at REAL)"
                )
                qconn.execute("INSERT INTO quota_observations VALUES ('acc_demo_1', 'gemini-weekly', 71.0, ?, ?)", (now_ms, reset_wk_ms))
                qconn.execute("INSERT INTO quota_observations VALUES ('acc_demo_1', '3p-weekly', 40.0, ?, ?)", (now_ms, reset_wk_ms))
                qconn.commit()
                qconn.close()

                engine = AntigravityMonitorEngine(
                    paths={
                        "brain_dir": root / "brain",
                        "conversations_dir": root / "conversations",
                        "daemon_dir": daemon,
                        "ls_log_path": root / "language_server.log",
                        "cockpit_quota_db": qdb,
                    },
                    read_only=True,
                )
                snap = engine.poll(force_account=True)
                self.assertTrue(snap.account.online)
                self.assertEqual(snap.account.short_label, "te***2")
                self.assertIn("Pro", snap.account.plan_status)
                self.assertIn("gemini", snap.account.pools)
                self.assertIn("3p", snap.account.pools)
                self.assertAlmostEqual(snap.account.pools["gemini"].five_hour_pct or 0.0, 96.0, places=1)
                self.assertEqual(snap.account.pools["gemini"].five_hour_reset_at, "2026-11-20T12:00:00Z")
                self.assertAlmostEqual(snap.account.pools["gemini"].weekly_pct or 0.0, 71.0, places=1)
                self.assertIsNotNone(snap.account.pools["gemini"].weekly_reset_at)
                self.assertAlmostEqual(snap.account.pools["3p"].five_hour_pct or 0.0, 24.0, places=1)
                self.assertEqual(snap.account.pools["3p"].five_hour_reset_at, "2026-11-25T08:00:00Z")
                self.assertAlmostEqual(snap.account.pools["3p"].weekly_pct or 0.0, 40.0, places=1)

                js = format_json_snapshot(snap)
                self.assertNotIn("tester42@example.com", js)
                obj = json.loads(js)
                self.assertEqual(obj["account"]["pools"]["3p"]["model_count"], 1)
                self.assertEqual(obj["account"]["pools"]["gemini"]["model_count"], 2)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_swarm_burst_protects_main_t1_step_breakdown_and_detail_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ls_log.write_text("", encoding="utf-8")
            now = time.time()

            # 1 older main conversation with T1 consecutive misses (rank 29 by mtime!)
            main_id = "main9999-aaaa-bbbb"
            b_main = root / "brain" / main_id / ".system_generated" / "logs"
            b_main.mkdir(parents=True)
            main_rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "test"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 400)), "model": "gemini-4-neon-xhigh", "input_tokens": 110000, "cache_read_tokens": 0, "output_tokens": 400},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 380)), "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 400},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 360)), "model": "gemini-4-neon-xhigh", "input_tokens": 130000, "cache_read_tokens": 0, "output_tokens": 500},
            ]
            (b_main / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in main_rows) + "\n", encoding="utf-8")
            mconn = sqlite3.connect(convs / f"{main_id}.db")
            mconn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            mconn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (_build_fake_gen_blob(110000, 0, 400, ttft_s=12, strm_s=4),))
            mconn.execute("INSERT INTO gen_metadata VALUES (2, ?)", (_build_fake_gen_blob(120000, 0, 400, ttft_s=50, strm_s=5),))
            mconn.execute("INSERT INTO gen_metadata VALUES (3, ?)", (_build_fake_gen_blob(130000, 0, 500, ttft_s=95, strm_s=5),))
            mconn.commit()
            mconn.close()

            # 28 newer swarm subagents pushing main_id well beyond index 24!
            for idx in range(28):
                sid = f"sub{idx:02d}xxx-1111-2222"
                bs = root / "brain" / sid / ".system_generated" / "logs"
                bs.mkdir(parents=True)
                ts_s = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 30 + idx))
                (bs / "transcript.jsonl").write_text(
                    json.dumps({"source": "SWARM", "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": ts_s, "model": "gemini-4-hydrogen-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000, "output_tokens": 200}) + "\n",
                    encoding="utf-8",
                )

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=now, target_conversation_id="main99")
            # Main conversation MUST be protected from swarm eviction and surface its T1 drop!
            self.assertEqual(snap.grade, "T1")
            self.assertEqual(snap.pressure, "degraded")
            self.assertEqual(compute_cli_exit_code(snap), 1)
            self.assertEqual(snap.main_today_steps, 3)
            self.assertEqual(snap.subagent_today_steps, 28)
            self.assertEqual(snap.today_steps, 31)
            self.assertGreaterEqual(snap.last_1h_steps, 31)
            self.assertGreaterEqual(snap.peak_hour_steps_today, 31)
            self.assertIsNotNone(snap.target_conversation)
            assert snap.target_conversation is not None
            self.assertEqual(snap.target_conversation.conversation_id, main_id)
            self.assertEqual(snap.target_conversation.latest_context_delta, 10000)

            # On-demand full-history detail report
            detail = engine.read_conversation_detail("main99", now_ts=now)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail.total_steps, 3)
            self.assertEqual(detail.peak_context_tokens, 130000)
            self.assertAlmostEqual(detail.max_ttft_sec or 0.0, 95.5, places=1)
            self.assertEqual(len(detail.points), 3)
            self.assertAlmostEqual(detail.points[0].ttft_sec or 0.0, 12.5, places=1)
            txt = format_detail_text_report(detail)
            self.assertIn("main99", txt)
            self.assertIn("+10.0K", txt)

    def test_conversation_titles_from_hub_and_vscdb_and_display_truncation(self) -> None:
        import base64
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import os
        import threading

        self.assertEqual(truncate_display_width("ABC", 6), "ABC")
        self.assertEqual(truncate_display_width("中文长标题测试文本超长", 10), "中文长标…")

        # 1. Verify offline state.vscdb double-base64 protobuf fallback + MM-DD date in detail report
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "title001-1234-5678"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            now = time.time()
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 20))
            (b_dir / "transcript.jsonl").write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": t0, "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200}) + "\n",
                encoding="utf-8",
            )
            sub_pb = _pb_bytes_field(1, "离线测试中文对话名".encode("utf-8"))
            inner_pb = _pb_bytes_field(1, base64.b64encode(sub_pb))
            entry_pb = _pb_bytes_field(1, cid.encode("utf-8")) + _pb_bytes_field(2, inner_pb)
            top_pb = _pb_bytes_field(1, entry_pb)
            vscdb = root / "state.vscdb"
            vconn = sqlite3.connect(vscdb)
            vconn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            vconn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("antigravityUnifiedStateSync.trajectorySummaries", base64.b64encode(top_pb).decode("ascii")),
            )
            vconn.commit()
            vconn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "vscdb_path": vscdb,
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=now)
            self.assertEqual(snap.active_conversations[0].title, "离线测试中文对话名")
            detail = engine.read_conversation_detail(cid, now_ts=now)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail.title, "离线测试中文对话名")
            rep_txt = format_detail_text_report(detail)
            self.assertIn("离线测试中文对话名 (title0)", rep_txt)
            self.assertIn(time.strftime("%m-%d ", time.localtime(now - 20)), rep_txt)

        # 2. Verify live Hub GetAllCascadeTrajectories title override
        class _TitleHub(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if "GetAllCascadeTrajectories" in self.path:
                    body = json.dumps({"trajectorySummaries": {"hubconv1-aaaa": {"summary": "在线热聊标题"}}}).encode("utf-8")
                else:
                    body = json.dumps({"userStatus": {"email": "demo1@example.com", "cascadeModelConfigData": {"clientModelConfigs": []}}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt: str, *_args: object) -> None:
                return

        srv = HTTPServer(("127.0.0.1", 0), _TitleHub)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                daemon = root / "daemon"
                daemon.mkdir()
                (daemon / "hub_t.json").write_text(
                    json.dumps({"pid": os.getpid(), "httpPort": port, "csrfToken": "tok_t"}),
                    encoding="utf-8",
                )
                b_dir = root / "brain" / "hubconv1-aaaa" / ".system_generated" / "logs"
                b_dir.mkdir(parents=True)
                now = time.time()
                (b_dir / "transcript.jsonl").write_text(
                    json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}) + "\n"
                    + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 5)), "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200}) + "\n",
                    encoding="utf-8",
                )
                engine = AntigravityMonitorEngine(
                    paths={
                        "brain_dir": root / "brain",
                        "conversations_dir": root / "conversations",
                        "daemon_dir": daemon,
                        "ls_log_path": root / "language_server.log",
                        "cockpit_quota_db": root / "missing.sqlite",
                    },
                    read_only=True,
                )
                snap = engine.poll(now_ts=now, force_account=True)
                self.assertEqual(snap.active_conversations[0].title, "在线热聊标题")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_account_switch_isolates_503_logs_primary_and_blocks_offline_label(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "acctiso1-1111" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()
            ls_log = root / "language_server.log"
            ledger_p = root / "account_ledger.json"

            # Verify "离线" never pollutes ledger as "离***"
            from core.account_ledger import AccountSwitchLedger
            led = AccountSwitchLedger(ledger_path=ledger_p)
            led.observe_online("离线", time.time())
            led.observe_online("离***", time.time())
            self.assertFalse(ledger_p.exists())

            now = time.time()
            t_old_acct = now - 600
            t_new_acct = now - 60
            (ck_logs / "app.log.2026-11-21").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_old_acct))}  INFO [Antigravity 2.0] 写入系统凭据: old01@example.com\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_new_acct))}  INFO [Antigravity 2.0] 写入系统凭据: fresh9@example.com\n",
                encoding="utf-8",
            )
            # 4x 503 errors + 0% cache miss occurred on old01 BEFORE switching to fresh9
            glog_old = time.strftime("%m%d %H:%M:%S", time.localtime(t_old_acct + 120))
            ls_log.write_text(
                "".join(
                    f"ERROR: logging before google.Init: I{glog_old}.{100000 + i} 22456 run.go:315] Run: attempt {i+1} failed (UNAVAILABLE (code 503): No capacity available)\n"
                    for i in range(4)
                ),
                encoding="utf-8",
            )
            (brain / "transcript.jsonl").write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_old_acct + 100)), "model": "gemini-4-argon-high", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 300}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_old_acct + 130)), "model": "gemini-4-argon-high", "input_tokens": 125000, "cache_read_tokens": 0, "output_tokens": 300}) + "\n",
                encoding="utf-8",
            )

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": ledger_p,
                },
                read_only=True,
            )
            from core.log_monitor import AccountQuotaInfo
            engine._account_cache = AccountQuotaInfo(email_masked="fr***9@example.com", short_label="fr***9", online=True, observed_at=now)
            engine._last_account_poll = now
            snap = engine.poll(now_ts=now, force_account=False)
            # Switching to fresh9 MUST isolate old01's 4x 503 logs and 0% cache bar!
            self.assertEqual(snap.recent_503, 0)
            self.assertEqual(snap.today_503, 0)
            self.assertEqual(snap.machine_today_503, 4)
            self.assertEqual(snap.grade, "T0")
            self.assertEqual(snap.pressure, "normal")
            self.assertIsNone(snap.primary_hit_rate)
            self.assertIn("已切入 fr***9", snap.headline)

    def test_t2_dynamic_recovery_with_per_model_ttft_and_multimodel_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain" / "t2rec001-2222" / ".system_generated" / "logs"
            brain.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"

            now = time.time()
            t_err = now - 120  # 2 minutes ago: 4x 503 on Argon
            glog_err = time.strftime("%m%d %H:%M:%S", time.localtime(t_err))
            ls_log.write_text(
                "".join(
                    f"ERROR: logging before google.Init: I{glog_err}.{200000 + i} 22456 run.go:315] Run: attempt {i+1} failed (UNAVAILABLE (code 503): No capacity available)\n"
                    for i in range(4)
                ),
                encoding="utf-8",
            )
            # Phase 1: only 1 Argon step before the 503s -> MUST stay T2 red!
            tp = brain / "transcript.jsonl"
            rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_err - 20)), "model": "gemini-4-argon-high", "input_tokens": 60000, "cache_read_tokens": 0, "output_tokens": 300},
            ]
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            dbp = convs / "t2rec001-2222.db"
            conn = sqlite3.connect(dbp)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (_build_fake_gen_blob(60000, 0, 300, ttft_s=52, strm_s=4),))
            conn.commit()
            conn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            from core.log_monitor import AccountQuotaInfo
            engine._account_cache = AccountQuotaInfo(email_masked="de***2@example.com", short_label="de***2", online=True, observed_at=now)
            engine._last_account_poll = now
            snap1 = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap1.grade, "T2")
            self.assertEqual(snap1.level, "red")

            # Phase 2: user switches to Neon XHigh and runs 3 fast cached steps (TTFT ~6.5s) -> de-escalates from T2 red to WARN!
            time.sleep(0.02)
            for k in range(3):
                rows.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": 2 + k,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_err + 25 + k * 20)),
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 2000,
                    "cache_read_tokens": 65000 + k * 2000,
                    "output_tokens": 500,
                })
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            conn = sqlite3.connect(dbp)
            for k in range(3):
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (2 + k, _build_fake_gen_blob(2000, 65000 + k * 2000, 500, ttft_s=6, strm_s=3)))
            conn.commit()
            conn.close()

            snap2 = engine.poll(now_ts=now + 2, force_account=False)
            self.assertEqual(snap2.grade, "WARN")
            self.assertEqual(snap2.level, "orange")
            self.assertIn("已连跑 3 步正常", snap2.t2_recovery_note)
            model_names = {m.model for m in snap2.model_stats}
            self.assertIn("Neon XHigh", model_names)
            self.assertIn("Argon High", model_names)

            # Detail report must label each step's model clearly
            det = engine.read_conversation_detail("t2rec001", now_ts=now + 2)
            self.assertIsNotNone(det)
            assert det is not None
            rep_txt = format_detail_text_report(det)
            self.assertIn("[Argon High]", rep_txt)
            self.assertIn("[Neon XHigh]", rep_txt)
            self.assertIn("含 2 种模型", rep_txt)

            # Phase 3: 6+ normal steps and >10 minutes since last 503 -> smooth recovery all the way back to T0 green!
            time.sleep(0.02)
            t_later = t_err + 650
            for k in range(4):
                rows.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": 5 + k,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_err + 200 + k * 90)),
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 2000,
                    "cache_read_tokens": 75000 + k * 2000,
                    "output_tokens": 500,
                })
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            conn = sqlite3.connect(dbp)
            for k in range(4):
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (5 + k, _build_fake_gen_blob(2000, 75000 + k * 2000, 500, ttft_s=7, strm_s=3)))
            conn.commit()
            conn.close()
            engine._last_account_poll = t_later
            snap3 = engine.poll(now_ts=t_later, force_account=False)
            self.assertEqual(snap3.grade, "T0")
            self.assertEqual(snap3.level, "green")
            self.assertIn("已平复", snap3.sub_headline)


if __name__ == "__main__":
    unittest.main()
