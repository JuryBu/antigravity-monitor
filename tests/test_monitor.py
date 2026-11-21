"""Comprehensive unit tests for Antigravity Monitor core engine, SQLite reader, config, and shortcuts."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import List

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
            # Pin to fixed 12:30 local so 400s offsets never straddle a top-of-hour boundary!
            now = datetime.datetime.now().replace(hour=12, minute=30, second=0, microsecond=0).timestamp()

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
            # Pin to fixed 12:30 local so 400s offsets never straddle a top-of-hour boundary!
            now = datetime.datetime.now().replace(hour=12, minute=30, second=0, microsecond=0).timestamp()

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
            # Switching to fresh9 MUST isolate old01's 4x 503 logs and 0% cache bar,
            # and stay objectively in WAIT (not blindly claim T0 healthy with zero samples).
            self.assertEqual(snap.recent_503, 0)
            self.assertEqual(snap.today_503, 0)
            self.assertEqual(snap.machine_today_503, 4)
            self.assertEqual(snap.grade, "WAIT")
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

    def test_annotations_pbtxt_title_resolution_and_escapes(self) -> None:
        from core.log_monitor import parse_pbtxt_title

        # Direct UTF-8, quoted escapes, and Go prototext octal UTF-8 escapes
        self.assertEqual(
            parse_pbtxt_title('title:"矢量图渲染测试示例" last_user_view_time:{seconds:1795197574}'.encode("utf-8")),
            "矢量图渲染测试示例",
        )
        octal_raw = b'title:"\\346\\265\\213\\350\\257\\225 \\"A\\\\B\\"" last_user_view_time:{seconds:1}'
        self.assertEqual(parse_pbtxt_title(octal_raw), '测试 "A\\B"')
        self.assertEqual(parse_pbtxt_title(b"last_user_view_time:{seconds:123}"), "")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "demo0529-0030-459c-aafa-000000000001"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            ann_dir = root / "annotations"
            ann_dir.mkdir(parents=True)
            now = time.time()
            t0 = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now - 15))
            (b_dir / "transcript.jsonl").write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "svg"}) + "\n"
                + json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "created_at": t0,
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 48000,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                }) + "\n",
                encoding="utf-8",
            )
            pbtxt = ann_dir / f"{cid}.pbtxt"
            pbtxt.write_text('title:"矢量图渲染测试示例" last_user_view_time:{seconds:1795197574 nanos:0}\n', encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "annotations_dir": ann_dir,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            snap = engine.poll(now_ts=now)
            self.assertEqual(snap.active_conversations[0].title, "矢量图渲染测试示例")
            detail = engine.read_conversation_detail(cid, now_ts=now)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail.title, "矢量图渲染测试示例")

            # Live rename in annotations/*.pbtxt must invalidate cache and update title immediately
            time.sleep(0.02)
            pbtxt.write_text('title:"矢量图重命名新标题" last_user_view_time:{seconds:1795197600}\n', encoding="utf-8")
            snap2 = engine.poll(now_ts=now + 2)
            self.assertEqual(snap2.active_conversations[0].title, "矢量图重命名新标题")

    def test_detail_window_locked_width_never_jitters_on_hover_or_switch(self) -> None:
        import tkinter as tk
        from core.log_monitor import ConversationDetailPoint, ConversationDetailReport
        from ui.detail_window import ConversationDetailWindow
        from ui.themes import THEMES

        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")

        try:
            now = time.time()
            pt_short = ConversationDetailPoint(
                step_index=3,
                timestamp=now - 120,
                total_tokens=18000,
                input_tokens=18000,
                cache_read_tokens=0,
                output_tokens=120,
                hit_rate=0.0,
                classification="expected_cold",
                reason="first_turn",
                context_delta=0,
                ttft_sec=4.2,
                tps=91.0,
                model="Neon XHigh",
            )
            pt_long = ConversationDetailPoint(
                step_index=1309,
                timestamp=now - 10,
                total_tokens=482100,
                input_tokens=482100,
                cache_read_tokens=0,
                output_tokens=640,
                hit_rate=0.0,
                classification="abnormal_miss",
                reason="abnormal_zero",
                context_delta=-245600,
                ttft_sec=134.8,
                tps=218.0,
                model="Hydrogen XH",
            )
            rep_a = ConversationDetailReport(
                conversation_id="demo0529-0030-459c-aafa-000000000001",
                short_id="demo05",
                is_subagent=False,
                model="Neon XHigh",
                model_raw="gemini-4-neon-xhigh",
                total_steps=38,
                today_steps=38,
                latest_context_tokens=217300,
                latest_context_delta=2000,
                peak_context_tokens=217300,
                max_context_tokens=512000,
                compact_count=0,
                cache_hit_steps=13,
                abnormal_miss_steps=22,
                expected_cold_steps=3,
                overall_cacheable_hit_ratio=0.371,
                avg_ttft_sec=24.7,
                p95_ttft_sec=72.3,
                max_ttft_sec=103.1,
                avg_tps=177.0,
                latest_tps=117.0,
                latest_ttft_sec=33.4,
                freshness_remaining_sec=None,
                freshness_invalid_reason="",
                freshness_window_sec=210,
                points=[pt_short, pt_long],
                title="矢量图渲染测试示例",
            )
            rep_b = ConversationDetailReport(
                conversation_id="long0001-0000-0000-0000-000000000001",
                short_id="long00",
                is_subagent=False,
                model="Carbon High",
                model_raw="gemini-4-carbon-high",
                total_steps=985,
                today_steps=120,
                latest_context_tokens=482100,
                latest_context_delta=-245600,
                peak_context_tokens=512000,
                max_context_tokens=512000,
                compact_count=4,
                cache_hit_steps=820,
                abnormal_miss_steps=2,
                expected_cold_steps=20,
                overall_cacheable_hit_ratio=0.997,
                avg_ttft_sec=18.2,
                p95_ttft_sec=44.5,
                max_ttft_sec=134.8,
                avg_tps=205.0,
                latest_tps=218.0,
                latest_ttft_sec=134.8,
                freshness_remaining_sec=120,
                freshness_invalid_reason="",
                freshness_window_sec=210,
                points=[pt_short, pt_long],
                title="这是一个非常非常长的中文标题测试用于验证不会撑宽单对话弹窗",
            )
            reps = {rep_a.conversation_id: rep_a, rep_b.conversation_id: rep_b}

            dw = ConversationDetailWindow(
                parent=root,
                theme=THEMES["midnight_blue"],
                load_report=lambda cid: reps.get(cid),
                on_toggle_pin=lambda _cid: None,
                is_pinned_fn=lambda _cid: False,
            )
            dw.show_conversation(rep_a.conversation_id, place_near_parent=True)
            root.update()
            base_w = dw.win.winfo_width()
            base_h = dw.win.winfo_height()
            self.assertGreaterEqual(base_w, 700)

            # Hover short step, hover ultra-long step, toggle mode, switch to ultra-long title conversation
            dw._update_readout_for_point(pt_short, prefix="选定步")
            root.update()
            self.assertEqual((dw.win.winfo_width(), dw.win.winfo_height()), (base_w, base_h))

            dw._update_readout_for_point(pt_long, prefix="选定步")
            root.update()
            self.assertEqual((dw.win.winfo_width(), dw.win.winfo_height()), (base_w, base_h))

            dw._set_window_mode("all")
            root.update()
            self.assertEqual((dw.win.winfo_width(), dw.win.winfo_height()), (base_w, base_h))

            dw.show_conversation(rep_b.conversation_id, place_near_parent=False)
            root.update()
            self.assertEqual((dw.win.winfo_width(), dw.win.winfo_height()), (base_w, base_h))

            # Right-hand close button must stay strictly inside locked window width
            close_right = dw._btn_close.winfo_x() + dw._btn_close.winfo_width()
            self.assertLessEqual(close_right, dw._hdr.winfo_width() + 2)
            dw.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_switch_back_to_historical_account_inherits_own_rating_and_view_filter(self) -> None:
        import datetime
        import os
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain_bad = root / "brain" / "badconv1-1111" / ".system_generated" / "logs"
            brain_good = root / "brain" / "goodconv-2222" / ".system_generated" / "logs"
            brain_bad.mkdir(parents=True)
            brain_good.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()

            # Pin to fixed midday (12:00 local) so test is 100% deterministic at any wall-clock time including 00:00-00:30
            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            t_bad = now - 5400.0
            t_good = now - 2400.0
            t_back_bad = now - 20.0

            (ck_logs / "app.log.2026-11-21").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_bad))}  INFO [Antigravity 2.0] 写入系统凭据: bad01@example.com\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_good))}  INFO [Antigravity 2.0] 写入系统凭据: good9@example.com\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_back_bad))}  INFO [Antigravity 2.0] 写入系统凭据: bad01@example.com\n",
                encoding="utf-8",
            )
            glog_bad = time.strftime("%m%d %H:%M:%S", time.localtime(t_bad + 130))
            ls_log.write_text(
                "".join(
                    f"ERROR: logging before google.Init: I{glog_bad}.{100000 + i} 22456 run.go:315] Run: attempt {i+1} failed (UNAVAILABLE (code 503): No capacity available)\n"
                    for i in range(3)
                ),
                encoding="utf-8",
            )

            # bad01 had 4 consecutive cr=0 steps earlier
            bad_rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "bad"}]
            for k in range(5):
                bad_rows.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": k + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_bad + 30 + k * 20)),
                    "model": "gemini-4-argon-high",
                    "input_tokens": 120000 + k * 1000,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                })
            tp_bad = brain_bad / "transcript.jsonl"
            tp_bad.write_text("\n".join(json.dumps(r) for r in bad_rows) + "\n", encoding="utf-8")
            os.utime(tp_bad, (t_bad + 120, t_bad + 120))

            # good9 had healthy cached steps later
            good_rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "good"}]
            for k in range(3):
                good_rows.append({
                    "type": "PLANNER_RESPONSE",
                    "step_index": k + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_good + 30 + k * 20)),
                    "model": "gemini-4-neon-xhigh",
                    "input_tokens": 60000 if k == 0 else 2000,
                    "cache_read_tokens": 0 if k == 0 else 60000,
                    "output_tokens": 300,
                })
            tp_good = brain_good / "transcript.jsonl"
            tp_good.write_text("\n".join(json.dumps(r) for r in good_rows) + "\n", encoding="utf-8")
            os.utime(tp_good, (t_good + 80, t_good + 80))

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            # Case 1: Just switched back to bad01 with ZERO new requests yet -> MUST inherit bad01's own historical T2 rating & 3x 503!
            engine._account_cache = AccountQuotaInfo(email_masked="ba***1@example.com", short_label="ba***1", online=True, observed_at=now)
            engine._last_account_poll = now
            snap_bad = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap_bad.grade, "T2")
            self.assertEqual(snap_bad.level, "red")
            self.assertTrue(snap_bad.is_historical_rating)
            self.assertEqual(snap_bad.recent_503, 3)
            self.assertEqual(len(snap_bad.active_conversations), 1)
            self.assertEqual(snap_bad.active_conversations[0].short_id, "badcon")
            self.assertIn("ba***1", snap_bad.available_accounts)
            self.assertIn("go***9", snap_bad.available_accounts)

            # Case 2: Manually switch dashboard view to historical good9 while bad01 is logged in -> sees good9's own T0 & 0x 503!
            snap_view_good = engine.poll(now_ts=now, force_account=False, view_account="go***9", history_range="today")
            self.assertEqual(snap_view_good.grade, "T0")
            self.assertEqual(snap_view_good.level, "green")
            self.assertTrue(snap_view_good.is_custom_account_view)
            self.assertEqual(snap_view_good.recent_503, 0)
            self.assertEqual(len(snap_view_good.active_conversations), 1)
            self.assertEqual(snap_view_good.active_conversations[0].short_id, "goodco")

    def test_undo_revert_not_misclassified_as_compact_or_abnormal_and_aligns_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "revt0001-3333-4444"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()

            now = time.time()
            def iso(offset: float) -> str:
                return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now + offset))

            # Simulate real Antigravity Undo to this point:
            # si=10 -> si=12 (200K) -> Undo back to si=8 -> new si=10 (110K, cr=0) + stale background GENERIC earlier ts
            lines = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 7, "created_at": iso(-300), "content": "start"},
                {"type": "PLANNER_RESPONSE", "step_index": 10, "created_at": iso(-290), "model": "gemini-4-neon-xhigh", "input_tokens": 110000, "cache_read_tokens": 0, "output_tokens": 300},
                {"type": "PLANNER_RESPONSE", "step_index": 12, "created_at": iso(-260), "model": "gemini-4-neon-xhigh", "input_tokens": 90000, "cache_read_tokens": 110000, "output_tokens": 400},
                # Discarded old branch step si=14 (will be overwritten/removed from SQLite on Undo)
                {"type": "PLANNER_RESPONSE", "step_index": 14, "created_at": iso(-230), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 200000, "output_tokens": 400},
                # User clicks "Undo to this point" back to step 8 (forward timestamp, regressed step_index!)
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 8, "created_at": iso(-200), "content": "try another path"},
                {"type": "PLANNER_RESPONSE", "step_index": 10, "created_at": iso(-195), "model": "gemini-4-neon-xhigh", "input_tokens": 112000, "cache_read_tokens": 0, "output_tokens": 350},
                # Delayed background GENERIC from 4 hours earlier (must NOT falsely trigger revert!)
                {"type": "GENERIC", "step_index": 2, "created_at": iso(-14400), "content": "old bg"},
                {"type": "PLANNER_RESPONSE", "step_index": 12, "created_at": iso(-170), "model": "gemini-4-neon-xhigh", "input_tokens": 3000, "cache_read_tokens": 112000, "output_tokens": 350},
            ]
            (b_dir / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")

            # SQLite gen_metadata drops the undone si=14 (202K), keeping only 4 live branch records
            dbp = convs / f"{cid}.db"
            conn = sqlite3.connect(dbp)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (_build_fake_gen_blob(110000, 0, 300, ttft_s=11, strm_s=3),))
            conn.execute("INSERT INTO gen_metadata VALUES (2, ?)", (_build_fake_gen_blob(90000, 110000, 400, ttft_s=12, strm_s=3),))
            conn.execute("INSERT INTO gen_metadata VALUES (3, ?)", (_build_fake_gen_blob(112000, 0, 350, ttft_s=22, strm_s=3),))
            conn.execute("INSERT INTO gen_metadata VALUES (4, ?)", (_build_fake_gen_blob(3000, 112000, 350, ttft_s=8, strm_s=3),))
            conn.commit()
            conn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            det = engine.read_conversation_detail(cid, now_ts=now)
            self.assertIsNotNone(det)
            assert det is not None
            self.assertEqual(det.revert_count, 1)
            self.assertEqual(det.compact_count, 0)
            self.assertEqual(det.abnormal_miss_steps, 0)
            # Point[3] is the post-Undo si=10 step -> classified as revert_reset (expected_cold), aligned to ttft=22s!
            self.assertEqual(det.points[3].reason, "revert_reset")
            self.assertEqual(det.points[3].classification, "expected_cold")
            self.assertTrue(det.points[3].is_revert)
            self.assertAlmostEqual(det.points[3].ttft_sec or 0.0, 22.0, delta=0.5)
            # Point[2] was the discarded old branch (202K) -> must stay None, NOT mis-aligned to previous step!
            self.assertIsNone(det.points[2].ttft_sec)
            # Point[4] after delayed old GENERIC -> still normal cache hit, NOT revert, aligned to ttft=8s
            self.assertFalse(det.points[4].is_revert)
            self.assertEqual(det.points[4].classification, "hit")
            self.assertAlmostEqual(det.points[4].ttft_sec or 0.0, 8.0, delta=0.5)

    def test_detail_window_range_60_vs_all_kpis_and_ttft_4tier_colors(self) -> None:
        import tkinter as tk
        from core.log_monitor import ConversationDetailPoint, ConversationDetailReport
        from ui.detail_window import ConversationDetailWindow, summarize_detail_points, ttft_metric_color
        from ui.themes import THEMES

        theme = THEMES["midnight_blue"]
        self.assertEqual(ttft_metric_color(10.0, theme), theme["green"])
        self.assertEqual(ttft_metric_color(32.3, theme), theme["cyan"])
        self.assertEqual(ttft_metric_color(60.0, theme), theme["orange"])
        self.assertEqual(ttft_metric_color(120.0, theme), theme["red"])

        now = time.time()
        pts = []
        # First 15 steps: abnormal misses, slow TTFT 120s, slow TPS 25
        for i in range(15):
            pts.append(
                ConversationDetailPoint(
                    step_index=i + 1,
                    timestamp=now - (75 - i) * 30,
                    model="Neon XHigh",
                    total_tokens=100000 + i * 1000,
                    context_delta=1000,
                    input_tokens=100000 + i * 1000,
                    cache_read_tokens=0,
                    output_tokens=200,
                    hit_rate=0.0,
                    classification="abnormal_miss",
                    reason="abnormal_zero",
                    ttft_sec=120.0,
                    tps=25.0,
                )
            )
        # Last 60 steps: 100% cache hit, avg TTFT ~32.0s with 1 deep-thinking peak 152.3s, fast TPS 180
        for j in range(60):
            pts.append(
                ConversationDetailPoint(
                    step_index=16 + j,
                    timestamp=now - (60 - j) * 20,
                    model="Neon XHigh",
                    total_tokens=120000 + j * 1500,
                    context_delta=1500,
                    input_tokens=1500,
                    cache_read_tokens=118500 + j * 1500,
                    output_tokens=300,
                    hit_rate=98.8,
                    classification="hit",
                    reason="cached",
                    ttft_sec=152.3 if j == 50 else 30.0,
                    tps=180.0,
                )
            )
        st60 = summarize_detail_points(pts[-60:])
        st_all = summarize_detail_points(pts)
        self.assertAlmostEqual(st60["cacheable_hit_ratio"] or 0.0, 1.0, places=3)
        self.assertAlmostEqual(st_all["cacheable_hit_ratio"] or 0.0, 0.8, places=3)

        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")

        try:
            rep = ConversationDetailReport(
                conversation_id="kpi0001-1111",
                short_id="kpi000",
                is_subagent=False,
                model="Neon XHigh",
                model_raw="gemini-4-neon-xhigh",
                total_steps=75,
                today_steps=75,
                peak_context_tokens=pts[-1].total_tokens,
                latest_context_tokens=pts[-1].total_tokens,
                latest_context_delta=1500,
                max_context_tokens=512000,
                compact_count=0,
                cache_hit_steps=60,
                abnormal_miss_steps=15,
                expected_cold_steps=0,
                overall_cacheable_hit_ratio=0.8,
                avg_ttft_sec=st_all["avg_ttft_sec"],
                p95_ttft_sec=st_all["p95_ttft_sec"],
                max_ttft_sec=st_all["max_ttft_sec"],
                avg_tps=st_all["avg_tps"],
                latest_tps=180.0,
                latest_ttft_sec=30.0,
                freshness_remaining_sec=120,
                freshness_invalid_reason="",
                freshness_window_sec=210,
                points=pts,
                title="长对话KPI联动测试",
            )
            dw = ConversationDetailWindow(
                parent=root,
                theme=theme,
                load_report=lambda _cid: rep,
                on_toggle_pin=lambda _cid: None,
                is_pinned_fn=lambda _cid: False,
            )
            dw.show_conversation(rep.conversation_id, place_near_parent=True)
            root.update()
            # In "60" mode: cache must be 100.0%, TTFT avg ~32.0s must be CYAN (NOT red despite 152.3s peak!)
            self.assertEqual(dw._kpi_cards[1]["val"].cget("text"), "100.0%")
            self.assertEqual(dw._kpi_cards[2]["val"].cget("fg"), theme["cyan"])
            self.assertEqual(dw._kpi_cards[2]["sub"].cget("fg"), theme["orange"])
            self.assertEqual(dw._kpi_cards[3]["val"].cget("text"), "均 180 t/s")

            # Switch to "all" mode: all KPI cards MUST update to full 75-step metrics!
            dw._set_window_mode("all")
            root.update()
            self.assertEqual(dw._kpi_cards[1]["val"].cget("text"), "80.0%")
            self.assertEqual(dw._kpi_cards[2]["val"].cget("fg"), theme["orange"])
            self.assertEqual(dw._kpi_cards[3]["val"].cget("text"), "均 149 t/s")
            dw.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_same_account_switch_back_isolates_yesterday_t1_from_today_live_session(self) -> None:
        import datetime
        import os
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "retn0001-1111-2222"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()
            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            t_yest = now - 26 * 3600.0
            t_other = now - 10 * 3600.0
            t_back = now - 300.0

            def iso(ts_v: float) -> str:
                return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(ts_v))

            (ck_logs / "app.log.2026-11-21").write_text(
                f"{iso(t_yest - 60)}  INFO [Antigravity 2.0] 写入系统凭据: alpha1@example.com\n"
                f"{iso(t_other)}  INFO [Antigravity 2.0] 写入系统凭据: beta22@example.com\n"
                f"{iso(t_back)}  INFO [Antigravity 2.0] 写入系统凭据: alpha1@example.com\n",
                encoding="utf-8",
            )
            # Yesterday alpha1 had 4 consecutive abnormal cr=0 misses; today after switching back alpha1 runs 3 healthy cached steps
            rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(t_yest), "content": "yest"},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": iso(t_yest + 10), "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": iso(t_yest + 30), "model": "gemini-4-neon-xhigh", "input_tokens": 122000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": iso(t_yest + 50), "model": "gemini-4-neon-xhigh", "input_tokens": 124000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 5, "created_at": iso(t_yest + 70), "model": "gemini-4-neon-xhigh", "input_tokens": 126000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 6, "created_at": iso(t_back + 30), "model": "gemini-4-neon-xhigh", "input_tokens": 128000, "cache_read_tokens": 0, "output_tokens": 300},
                {"type": "PLANNER_RESPONSE", "step_index": 7, "created_at": iso(t_back + 50), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 128000, "output_tokens": 300},
                {"type": "PLANNER_RESPONSE", "step_index": 8, "created_at": iso(t_back + 70), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 130000, "output_tokens": 300},
            ]
            tp = b_dir / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(tp, (t_back + 70, t_back + 70))

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            engine._account_cache = AccountQuotaInfo(email_masked="al***1@example.com", short_label="al***1", online=True, observed_at=now)
            engine._last_account_poll = now
            snap = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T0")
            self.assertEqual(snap.level, "green")
            self.assertEqual(snap.max_consec_miss, 0)
            self.assertEqual(snap.max_abnormal_recent10, 0)

    def test_older_52h_account_auto_expands_7d_and_single_cold_step_preserves_prior_t1_warn(self) -> None:
        import datetime
        import os
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid_old = "old52h01-aaaa-bbbb"
            b_old = root / "brain" / cid_old / ".system_generated" / "logs"
            b_old.mkdir(parents=True)
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()
            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            t_52h = now - 52 * 3600.0
            t_other = now - 40 * 3600.0
            t_relogin = now - 30.0

            def iso(ts_v: float) -> str:
                return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(ts_v))

            (ck_logs / "app.log.2026-11-21").write_text(
                f"{iso(t_52h - 60)}  INFO [Antigravity 2.0] 写入系统凭据: rest52@example.com\n"
                f"{iso(t_other)}  INFO [Antigravity 2.0] 写入系统凭据: main88@example.com\n"
                f"{iso(t_relogin)}  INFO [Antigravity 2.0] 写入系统凭据: rest52@example.com\n",
                encoding="utf-8",
            )
            old_rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(t_52h), "content": "probe"},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": iso(t_52h + 20), "model": "gemini-4-neon-xhigh", "input_tokens": 110000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": iso(t_52h + 40), "model": "gemini-4-neon-xhigh", "input_tokens": 115000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": iso(t_52h + 60), "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 200},
            ]
            tp_old = b_old / "transcript.jsonl"
            tp_old.write_text("\n".join(json.dumps(r) for r in old_rows) + "\n", encoding="utf-8")
            os.utime(tp_old, (t_52h + 60, t_52h + 60))

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            engine._account_cache = AccountQuotaInfo(email_masked="re***2@example.com", short_label="re***2", online=True, observed_at=now)
            engine._last_account_poll = now

            # Phase 1: 0 new steps after switching back to 52h-old T1 account -> MUST expand >48h to 7d and inherit T1 red!
            snap1 = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap1.grade, "T1")
            self.assertEqual(snap1.level, "red")
            self.assertTrue(snap1.is_historical_rating)

            # Phase 2: 1 pure expected_cold first-turn step (0 cacheable steps yet) -> MUST warn (NOT wash away prior T1 into T0!)
            cid_new = "newcold1-cccc-dddd"
            b_new = root / "brain" / cid_new / ".system_generated" / "logs"
            b_new.mkdir(parents=True)
            tp_new = b_new / "transcript.jsonl"
            tp_new.write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(t_relogin + 5), "content": "hi"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": iso(t_relogin + 10), "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200}) + "\n",
                encoding="utf-8",
            )
            os.utime(tp_new, (t_relogin + 10, t_relogin + 10))
            snap2 = engine.poll(now_ts=now + 2, force_account=False)
            self.assertEqual(snap2.grade, "WARN")
            self.assertEqual(snap2.level, "orange")
            self.assertIn("前序历史曾连续", snap2.sub_headline)

    def test_global_sqlite_alignment_prevents_cross_account_compaction_mismatch_and_caches_full_history(self) -> None:
        import datetime
        import os
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "xacct001-5555-6666"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()

            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            t1 = now - 3600.0
            t2 = now - 1200.0

            def iso(ts_v: float) -> str:
                return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(ts_v))

            (ck_logs / "app.log.2026-11-21").write_text(
                f"{iso(t1 - 60)}  INFO [Antigravity 2.0] 写入系统凭据: acc001@example.com\n"
                f"{iso(t2)}  INFO [Antigravity 2.0] 写入系统凭据: acc002@example.com\n",
                encoding="utf-8",
            )
            # acc001 ran at 180K -> 220K (TTFT 42s/48s); later acc002 compacted back to 180K (TTFT 5s)!
            # Greedy sliced alignment would wrongly bind acc001's 180K step to acc002's 5s row; global alignment MUST bind 42s/48s!
            rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(t1), "content": "go"},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": iso(t1 + 10), "model": "gemini-4-neon-xhigh", "input_tokens": 180000, "cache_read_tokens": 0, "output_tokens": 300},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": iso(t1 + 40), "model": "gemini-4-neon-xhigh", "input_tokens": 40000, "cache_read_tokens": 180000, "output_tokens": 400},
                {"type": "CHECKPOINT", "step_index": 4, "created_at": iso(t2 + 5)},
                {"type": "PLANNER_RESPONSE", "step_index": 5, "created_at": iso(t2 + 15), "model": "gemini-4-neon-xhigh", "input_tokens": 180000, "cache_read_tokens": 0, "output_tokens": 600},
                {"type": "PLANNER_RESPONSE", "step_index": 6, "created_at": iso(t2 + 40), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 180000, "output_tokens": 250},
            ]
            tp = b_dir / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(tp, (t2 + 40, t2 + 40))

            dbp = convs / f"{cid}.db"
            conn = sqlite3.connect(dbp)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (_build_fake_gen_blob(180000, 0, 300, ttft_s=42, strm_s=4),))
            conn.execute("INSERT INTO gen_metadata VALUES (2, ?)", (_build_fake_gen_blob(40000, 180000, 400, ttft_s=48, strm_s=4),))
            conn.execute("INSERT INTO gen_metadata VALUES (3, ?)", (_build_fake_gen_blob(180000, 0, 600, ttft_s=5, strm_s=4),))
            conn.execute("INSERT INTO gen_metadata VALUES (4, ?)", (_build_fake_gen_blob(2000, 180000, 250, ttft_s=6, strm_s=4),))
            conn.commit()
            conn.close()

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            engine._account_cache = AccountQuotaInfo(email_masked="ac***2@example.com", short_label="ac***2", online=True, observed_at=now, five_hour_pct=88.0, weekly_pct=60.0)
            engine._last_account_poll = now

            snap = engine.poll(now_ts=now, force_account=False, view_account="ac***1", history_range="7d")
            self.assertEqual(len(snap.active_conversations), 1)
            c1 = snap.active_conversations[0]
            self.assertEqual(c1.total_step_count, 2)
            self.assertEqual(c1.full_step_count, 4)
            self.assertAlmostEqual(c1.ttft_sec or 0.0, 48.5, delta=0.6)
            self.assertAlmostEqual(c1.ttft_avg_sec or 0.0, 45.5, delta=0.6)
            # Custom account text summary must NOT leak online acc002's 5h/weekly quota!
            txt = format_text_summary(snap)
            self.assertIn("观察视角: ac***1", txt)
            self.assertNotIn("88.0%", txt)
            # Full-history SQLite cache should now be populated and hit on second call
            self.assertIn(cid, engine._db_reader._full_cache)
            det_scoped = engine.read_conversation_detail(cid, now_ts=now, view_account="ac***1", history_range="7d")
            self.assertIsNotNone(det_scoped)
            assert det_scoped is not None
            self.assertEqual(det_scoped.total_steps, 2)
            self.assertIn("ac***1", det_scoped.scope_label)

    def test_first_turn_503_resend_not_revert_and_cached_revert_and_cached_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "edge0001-7777-8888"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            now = time.time()

            def iso(off: float) -> str:
                return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now + off))

            rows = [
                # Duplicate USER_INPUT before any PLANNER_RESPONSE (503 resend) -> MUST NOT trigger revert!
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(-500), "content": "try 1"},
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": iso(-490), "content": "try 1 resend"},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": iso(-480), "model": "gemini-4-neon-xhigh", "input_tokens": 450000, "cache_read_tokens": 0, "output_tokens": 300},
                # True compaction with partial prefix cache hit (cr > 0, 450K -> 180K) -> is_compact=True, compact_count=1
                {"type": "CHECKPOINT", "step_index": 3, "created_at": iso(-450)},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": iso(-440), "model": "gemini-4-neon-xhigh", "input_tokens": 155000, "cache_read_tokens": 25000, "output_tokens": 300},
                # True Undo revert that still hits earlier prefix cache (cr > 0) -> is_revert=True, reason="cached", [回溯·命中]
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 2, "created_at": iso(-400), "content": "undo and retry"},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": iso(-395), "model": "gemini-4-neon-xhigh", "input_tokens": 20000, "cache_read_tokens": 100000, "output_tokens": 250},
            ]
            (b_dir / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                },
                read_only=True,
            )
            det = engine.read_conversation_detail(cid, now_ts=now)
            self.assertIsNotNone(det)
            assert det is not None
            self.assertFalse(det.points[0].is_revert)
            self.assertTrue(det.points[1].is_compact)
            self.assertFalse(det.points[1].is_revert)
            self.assertEqual(det.compact_count, 1)
            self.assertTrue(det.points[2].is_revert)
            self.assertEqual(det.points[2].reason, "cached")
            self.assertEqual(det.revert_count, 1)
            rep_txt = format_detail_text_report(det)
            self.assertIn("[回溯·命中]", rep_txt)
            self.assertIn("[压缩·命中]", rep_txt)

    def test_detail_window_sync_idempotency_under_filtered_view_and_cli_args(self) -> None:
        import tkinter as tk
        from core.log_monitor import AccountQuotaInfo, ConversationDetailPoint, ConversationDetailReport, ConversationSummary, MonitorSnapshot
        from main import _parse_args
        from ui.detail_window import ConversationDetailWindow
        from ui.themes import THEMES

        # 1. Verify CLI --account / --range parser
        ns = _parse_args(["--json", "--readonly", "--account", "al***1", "--range", "7d"])
        self.assertTrue(ns.json)
        self.assertTrue(ns.readonly)
        self.assertEqual(ns.account, "al***1")
        self.assertEqual(ns.history_range, "7d")

        # 2. Verify ConversationDetailWindow.sync_from_snapshot makes 0 redundant load_report calls under filtered view
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")

        try:
            now = time.time()
            pt = ConversationDetailPoint(
                step_index=10, timestamp=now - 50, model="Neon XHigh",
                total_tokens=150000, context_delta=2000, input_tokens=2000, cache_read_tokens=148000,
                output_tokens=300, hit_rate=98.7, classification="hit", reason="cached", ttft_sec=12.0, tps=160.0,
            )
            rep = ConversationDetailReport(
                conversation_id="idem0001-9999", short_id="idem00", is_subagent=False,
                model="Neon XHigh", model_raw="gemini-4-neon-xhigh", total_steps=50, today_steps=10,
                peak_context_tokens=150000, latest_context_tokens=150000, latest_context_delta=2000,
                max_context_tokens=512000, compact_count=0, cache_hit_steps=50, abnormal_miss_steps=0,
                expected_cold_steps=0, overall_cacheable_hit_ratio=1.0, avg_ttft_sec=12.0, p95_ttft_sec=12.0,
                max_ttft_sec=12.0, avg_tps=160.0, latest_tps=160.0, latest_ttft_sec=12.0,
                freshness_remaining_sec=None, freshness_invalid_reason="", freshness_window_sec=210,
                points=[pt], title="防抖测试",
            )
            calls = {"n": 0}

            def _loader(_cid: str) -> ConversationDetailReport:
                calls["n"] += 1
                return rep

            conv_summary = ConversationSummary(
                conversation_id="idem0001-9999", short_id="idem00", model="Neon XHigh",
                model_raw="gemini-4-neon-xhigh", context_tokens=150000, max_context_tokens=512000,
                latest_hit_rate=98.7, latest_cached_in=148000, latest_uncached_in=2000,
                latest_timestamp=now - 50, recent_steps=[], consec_miss=0, consec_miss_start_step=0,
                single_miss=False, wasted_cold_tokens=0, freshness_remaining_sec=None,
                total_step_count=12, full_step_count=50,
                full_latest_timestamp=now - 50, ttft_sec=12.0, tps=160.0, title="防抖测试",
            )
            snap_filtered = MonitorSnapshot(
                generated_at=now, level="green", grade="T0", headline="T0", sub_headline="ok",
                active_model="Neon XHigh", primary_hit_rate=98.7, primary_hit_classification="hit",
                primary_tps=160.0, primary_ttft=12.0, primary_speed_status="ok",
                min_freshness_sec=None, min_freshness_conv="", min_freshness_ctx=0,
                min_freshness_invalid_reason="", today_steps=12, daily_steps_warn=960, daily_steps_alert=1200,
                active_conversations=[conv_summary], large_freshness_list=[],
                recent_503=0, today_503=0, recent_timeout=0, today_timeout=0,
                recent_403=0, today_403=0, recent_retry=0, today_retry=0,
                recent_events=[], wasted_cold_tokens_total=0, max_consec_miss=0,
                account=AccountQuotaInfo(short_label="al***1", online=True),
                sliding_window_min=30, freshness_window_sec=210,
                view_account="al***1", resolved_account="al***1", is_custom_account_view=True,
                history_range="7d", history_range_label="近 7 天",
            )
            dw = ConversationDetailWindow(
                parent=root, theme=THEMES["midnight_blue"], load_report=_loader,
                on_toggle_pin=lambda _c: None, is_pinned_fn=lambda _c: False, slow_tps_threshold=40.0,
            )
            dw.show_conversation("idem0001-9999", place_near_parent=True, initial_snap=snap_filtered)
            self.assertEqual(calls["n"], 1)
            for _ in range(5):
                dw.sync_from_snapshot(snap_filtered)
            # Zero redundant refreshes even though conv.total_step_count (12) != rep.total_steps (50)!
            self.assertEqual(calls["n"], 1)
            # v7 policy: single-conversation detail window always represents full live conversation, never fragmented by left account filter
            self.assertIn("实时", dw._lbl_live_tag.cget("text"))
            dw.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_v7_single_stream_timeout_warns_with_dynamic_duration_and_culprit_and_escalates_on_second(self) -> None:
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "demo7a12-1111-2222-3333-444455556666"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ann_dir = root / "annotations"
            ann_dir.mkdir()
            (ann_dir / f"{cid}.pbtxt").write_text('title: "多模态构图排版示例"\n', encoding="utf-8")
            ls_log = root / "language_server.log"

            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            err_ts = now - 60.0
            glog_t1 = time.strftime("%m%d %H:%M:%S", time.localtime(err_ts))
            # Include a 500ms local RPC timeout line to prove short non-LLM timeouts are ignored!
            ls_log.write_text(
                f"ERROR: logging before google.Init: I{glog_t1}.050000 1234 rpc.go:99] local probe timed out after 500ms\n"
                f"ERROR: logging before google.Init: I{glog_t1}.100000 1234 run.go:315] "
                f"Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s\n",
                encoding="utf-8",
            )
            tr_lines = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts - 30)), "content": "hi"},
                # PLANNER_RESPONSE mentioning literal "ERROR_MESSAGE" in content MUST NOT be swallowed!
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts - 15)), "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 300, "content": 'checking "ERROR_MESSAGE" handler'},
                {"type": "ERROR_MESSAGE", "step_index": 3, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts + 1.0)), "content": "stream idle timeout"},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts + 10)), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 120000, "output_tokens": 350},
            ]
            tp = b_dir / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in tr_lines) + "\n", encoding="utf-8")
            os.utime(tp, (err_ts + 10, err_ts + 10))

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "summaries_db_path": root / "summaries.db",
                    "annotations_dir": ann_dir,
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            engine._account_cache = AccountQuotaInfo(email_masked="al***1@example.com", short_label="al***1", online=True, observed_at=now)
            engine._last_account_poll = now

            snap1 = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap1.grade, "WARN")
            self.assertEqual(snap1.level, "orange")
            self.assertEqual(snap1.recent_timeout, 1)
            # Step 2 with literal "ERROR_MESSAGE" in content MUST be preserved (total 2 planner steps!)
            self.assertEqual(snap1.active_conversations[0].total_step_count, 2)
            self.assertTrue(any("流超时 (10m)" in ev.summary and "demo7a" in ev.summary and "多模态构图排版示例" in ev.summary for ev in snap1.recent_events))
            self.assertIn("demo7a", snap1.sub_headline)
            txt1 = format_text_summary(snap1)
            self.assertIn("流超时=1/1", txt1)
            self.assertNotIn("5m超时=", txt1)

            # Second timeout (or 503) in window escalates to red T2
            glog_t2 = time.strftime("%m%d %H:%M:%S", time.localtime(now - 20.0))
            with ls_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"ERROR: logging before google.Init: I{glog_t2}.200000 1234 run.go:315] "
                    f"Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available)\n"
                )
            snap2 = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(snap2.grade, "T2")
            self.assertEqual(snap2.level, "red")

    def test_v7_t2_e2e_no_light_subagent_whitewash_and_multimodal_spike_anti_flap(self) -> None:
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            convs = root / "conversations"
            convs.mkdir()
            ls_log = root / "language_server.log"
            now = datetime.datetime.now().replace(hour=12, minute=30, second=0, microsecond=0).timestamp()
            err_ts = now - 300.0
            g1 = time.strftime("%m%d %H:%M:%S", time.localtime(err_ts - 5))
            g2 = time.strftime("%m%d %H:%M:%S", time.localtime(err_ts))
            ls_log.write_text(
                f"ERROR: logging before google.Init: I{g1}.100000 1234 run.go:315] Run: attempt 1 failed (UNAVAILABLE (code 503): No capacity available)\n"
                f"ERROR: logging before google.Init: I{g2}.200000 1234 run.go:315] Run: attempt 2 failed (UNAVAILABLE (code 503): No capacity available)\n",
                encoding="utf-8",
            )

            # Case 1 (Negative test): Main Carbon High conversation is STILL degraded (TTFT 115s, 130s),
            # while a Hydrogen XHigh subagent ran 6 fast 4s steps after T2 -> MUST STAY RED T2 (NO whitewash!)
            main_cid = "mainc001-0000-0000-0000-000000000001"
            sub_cid = "subh0001-0000-0000-0000-000000000002"
            (root / "brain" / main_cid / ".system_generated" / "logs").mkdir(parents=True)
            (root / "brain" / sub_cid / ".system_generated" / "logs").mkdir(parents=True)

            main_rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts - 60)), "content": "main"}]
            mconn = sqlite3.connect(convs / f"{main_cid}.db")
            mconn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            for i, ttft in enumerate((115, 130), start=1):
                in_t = 90000 if i == 1 else 2000
                cr_t = 0 if i == 1 else 90000
                main_rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": i + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts + 30 * i)),
                    "model": "gemini-4-carbon-high", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 300 + i,
                })
                mconn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (i, _build_fake_gen_blob(in_t, cr_t, 300 + i, ttft_s=ttft, strm_s=2)))
            mconn.commit()
            mconn.close()
            tp_m = root / "brain" / main_cid / ".system_generated" / "logs" / "transcript.jsonl"
            tp_m.write_text("\n".join(json.dumps(r) for r in main_rows) + "\n", encoding="utf-8")
            os.utime(tp_m, (now - 10, now - 10))

            sub_rows = []
            sconn = sqlite3.connect(convs / f"{sub_cid}.db")
            sconn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            for i in range(1, 7):
                in_t = 40000 if i == 1 else 1500
                cr_t = 0 if i == 1 else 40000 + i * 1000
                sub_rows.append({
                    "source": "SWARM", "type": "PLANNER_RESPONSE", "step_index": i,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts + 20 * i)),
                    "model": "gemini-4-hydrogen-xhigh", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 200 + i,
                })
                sconn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (i, _build_fake_gen_blob(in_t, cr_t, 200 + i, ttft_s=4, strm_s=1)))
            sconn.commit()
            sconn.close()
            tp_s = root / "brain" / sub_cid / ".system_generated" / "logs" / "transcript.jsonl"
            tp_s.write_text("\n".join(json.dumps(r) for r in sub_rows) + "\n", encoding="utf-8")
            os.utime(tp_s, (now - 5, now - 5))

            eng = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "summaries_db_path": root / "summaries.db",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "missing.sqlite",
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            eng._account_cache = AccountQuotaInfo(email_masked="al***1@example.com", short_label="al***1", online=True, observed_at=now)
            eng._last_account_poll = now
            snap_still_t2 = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap_still_t2.grade, "T2")
            self.assertEqual(snap_still_t2.level, "red")

            # Case 2 (Positive anti-flap): Main Neon XHigh runs 4 fast steps (12s, 14s, 16s, 18s) + 1 slow 95s multimodal step
            # (mean=31s, median=16s < 30s, latest=95s < 150s) -> stays stable orange WARN without flapping back to T2!
            neon_cid = "mainn002-0000-0000-0000-000000000003"
            (root / "brain" / neon_cid / ".system_generated" / "logs").mkdir(parents=True)
            n_rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts - 60)), "content": "neon"}]
            nconn = sqlite3.connect(convs / f"{neon_cid}.db")
            nconn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            for i, ttft in enumerate((12, 14, 16, 18, 95), start=1):
                in_t = 80000 if i == 1 else 2500
                cr_t = 0 if i == 1 else 80000 + i * 2000
                n_rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": i + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(err_ts + 25 * i)),
                    "model": "gemini-4-neon-xhigh", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 350 + i,
                })
                nconn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (i, _build_fake_gen_blob(in_t, cr_t, 350 + i, ttft_s=ttft, strm_s=2)))
            nconn.commit()
            nconn.close()
            tp_n = root / "brain" / neon_cid / ".system_generated" / "logs" / "transcript.jsonl"
            tp_n.write_text("\n".join(json.dumps(r) for r in n_rows) + "\n", encoding="utf-8")
            os.utime(tp_n, (now - 1, now - 1))

            snap_warn = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap_warn.grade, "WARN")
            self.assertEqual(snap_warn.level, "orange")
            self.assertIn("TTFT 中位 16.", snap_warn.t2_recovery_note or snap_warn.sub_headline)

    def test_v7_cross_account_internal_gap_recovers_earlier_ttft_bars(self) -> None:
        import datetime
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cid = "gap12345-0000-1111-2222-333344445555"
            b_dir = root / "brain" / cid / ".system_generated" / "logs"
            b_dir.mkdir(parents=True)
            convs = root / "conversations"
            convs.mkdir()
            ck_logs = root / "cockpit_logs"
            ck_logs.mkdir()

            now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
            t_a1 = now - 3600.0
            t_b = now - 1800.0
            t_a2 = now - 120.0

            (ck_logs / "app.log.2026-11-21").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_a1 - 60))}  INFO [Antigravity 2.0] 写入系统凭据: alpha1@example.com\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_b - 60))}  INFO [Antigravity 2.0] 写入系统凭据: beta22@example.com\n"
                f"{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(t_a2 - 60))}  INFO [Antigravity 2.0] 写入系统凭据: alpha1@example.com\n",
                encoding="utf-8",
            )

            tr_rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_a1 - 10)), "content": "start"}]
            dbp = convs / f"{cid}.db"
            conn = sqlite3.connect(dbp)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            gen_idx = 0
            # 4 early steps by alpha1
            for i in range(4):
                gen_idx += 1
                in_t = 50000 if i == 0 else 2000
                cr_t = 0 if i == 0 else 50000 + i * 2000
                tr_rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": gen_idx + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_a1 + i * 15)),
                    "model": "gemini-4-neon-xhigh", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 300 + i,
                })
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (gen_idx, _build_fake_gen_blob(in_t, cr_t, 300 + i, ttft_s=11 + i, strm_s=2)))
            # 22 intervening steps by beta22 (> 16 tail window!)
            for j in range(22):
                gen_idx += 1
                in_t = 2500
                cr_t = 70000 + j * 1500
                tr_rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": gen_idx + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_b + j * 15)),
                    "model": "gemini-4-neon-xhigh", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 400 + j,
                })
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (gen_idx, _build_fake_gen_blob(in_t, cr_t, 400 + j, ttft_s=30, strm_s=2)))
            # 2 latest steps back on alpha1
            for k in range(2):
                gen_idx += 1
                in_t = 3000
                cr_t = 120000 + k * 3000
                tr_rows.append({
                    "type": "PLANNER_RESPONSE", "step_index": gen_idx + 1,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(t_a2 + k * 20)),
                    "model": "gemini-4-neon-xhigh", "input_tokens": in_t, "cache_read_tokens": cr_t, "output_tokens": 500 + k,
                })
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (gen_idx, _build_fake_gen_blob(in_t, cr_t, 500 + k, ttft_s=9 + k, strm_s=2)))
            conn.commit()
            conn.close()

            tp = b_dir / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in tr_rows) + "\n", encoding="utf-8")
            os.utime(tp, (t_a2 + 20, t_a2 + 20))

            engine = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": convs,
                    "summaries_db_path": root / "summaries.db",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "cockpit_logs_dir": ck_logs,
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            engine._account_cache = AccountQuotaInfo(email_masked="al***1@example.com", short_label="al***1", online=True, observed_at=now)
            engine._last_account_poll = now
            snap = engine.poll(now_ts=now, force_account=False)
            self.assertEqual(len(snap.active_conversations), 1)
            conv = snap.active_conversations[0]
            # All 6 alpha1 steps across the 22-step beta22 gap MUST have recovered non-None TTFT bars!
            self.assertEqual(len(conv.recent_ttfts), 6)
            self.assertTrue(all(v is not None for v in conv.recent_ttfts), f"Expected all non-None TTFTs, got {conv.recent_ttfts}")
            self.assertAlmostEqual(conv.recent_ttfts[0] or 0.0, 11.0, delta=0.5)
            self.assertAlmostEqual(conv.recent_ttfts[-1] or 0.0, 10.0, delta=0.5)

    def _create_slot_env(self, root: Path, now: float):
        convs = root / "conversations"
        brain = root / "brain"
        convs.mkdir(parents=True, exist_ok=True)
        brain.mkdir(parents=True, exist_ok=True)
        sum_db = root / "conversation_summaries.db"
        conn = sqlite3.connect(sum_db)
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT, step_count INTEGER, "
            "last_modified_time TEXT, status TEXT, agent_name TEXT, "
            "parent_conversation_id TEXT, nesting_depth INTEGER, not_fully_idle INTEGER)"
        )
        conn.commit()
        return convs, brain, sum_db, conn

    def _add_slot_conv(
        self,
        convs: Path,
        brain: Path,
        conn: sqlite3.Connection,
        cid: str,
        age_sec: float,
        now: float,
        *,
        in_summary: bool = True,
        title: str = "",
        agent_name: str = "",
        parent_id: str = "",
        nesting_depth: int = 0,
        status: str = "CASCADE_RUN_STATUS_IDLE",
        not_fully_idle: int = 0,
        touch_shm_fresh: bool = False,
        db_bytes: int = 4096,
    ) -> None:
        ts = now - age_sec
        dbp = convs / f"{cid}.db"
        dbp.write_bytes(b"x" * db_bytes)
        os.utime(dbp, (ts, ts))
        wal = convs / f"{cid}.db-wal"
        wal.write_bytes(b"")
        os.utime(wal, (ts, ts))
        shm = convs / f"{cid}.db-shm"
        shm.write_bytes(b"s" * 64)
        os.utime(shm, (now, now) if touch_shm_fresh else (ts, ts))
        bdir = brain / cid / ".system_generated" / "logs"
        bdir.mkdir(parents=True, exist_ok=True)
        tp = bdir / "transcript.jsonl"
        tp.write_text('{"type":"GENERIC"}\n', encoding="utf-8")
        os.utime(tp, (ts, ts))
        if in_summary:
            iso_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, title, 10, iso_utc, status, agent_name, parent_id, nesting_depth, not_fully_idle),
            )
            conn.commit()

    def test_v7_slot_scanner_ignores_db_shm_mtime_and_detects_main_at_risk_floor(self) -> None:
        from core.slot_manager import SlotInventoryScanner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = time.time()
            convs, brain, sum_db, conn = self._create_slot_env(root, now)
            try:
                # 1 main conversation (old, must NEVER be cleanable)
                self._add_slot_conv(convs, brain, conn, "main0001-0000", 86400, now, title="重要主对话")
                # 1 old completed subagent whose .db-shm was touched 0s ago (must IGNORE .db-shm mtime!)
                self._add_slot_conv(
                    convs, brain, conn, "subold01-0000", 3 * 3600, now,
                    agent_name="research", parent_id="main0001-0000", nesting_depth=1, touch_shm_fresh=True,
                )
                # 1 completed subagent at 45m (protected at 120m, cleanable at 30m floor)
                self._add_slot_conv(
                    convs, brain, conn, "submid01-0000", 45 * 60, now,
                    agent_name="worker", parent_id="main0001-0000", nesting_depth=1,
                )
                # 1 recent subagent at 10m (strictly inside 30m hard floor)
                self._add_slot_conv(
                    convs, brain, conn, "subnew01-0000", 10 * 60, now,
                    agent_name="worker", parent_id="main0001-0000", nesting_depth=1,
                )
                # 1 running subagent + 1 titled subagent + 1 not_fully_idle=1 + 1 unknown unregistered .db
                self._add_slot_conv(
                    convs, brain, conn, "subrun01-0000", 5 * 3600, now,
                    agent_name="worker", status="CASCADE_RUN_STATUS_RUNNING",
                )
                self._add_slot_conv(
                    convs, brain, conn, "subtit01-0000", 5 * 3600, now,
                    agent_name="worker", title="有标题子会话",
                )
                self._add_slot_conv(
                    convs, brain, conn, "subnfi01-0000", 5 * 3600, now,
                    agent_name="worker", not_fully_idle=1,
                )
                self._add_slot_conv(convs, brain, conn, "unknow01-0000", 5 * 3600, now, in_summary=False)
            finally:
                conn.close()

            scanner = SlotInventoryScanner(convs, brain, sum_db)
            inv = scanner.scan(now_ts=now, protect_age_min=120, include_items=True, force=True)
            self.assertEqual(inv.total_db, 8)
            self.assertEqual(inv.main_count, 1)
            self.assertEqual(inv.cleanable_sub_count, 1)
            self.assertEqual(inv.cleanable_30m_count, 2)
            self.assertEqual(inv.protected_sub_count, 5)
            self.assertEqual(inv.unknown_count, 1)
            self.assertFalse(inv.main_at_risk)
            cleanable_ids = [it.conversation_id for it in inv.items if it.category == "cleanable_sub"]
            self.assertEqual(cleanable_ids, ["subold01-0000"])

            # Simulate hitting 30m hard floor at cap=50 (add 42 extra protected main convs -> total=50, clean30=2, pinned_floor=48 >= cap-30!)
            conn_extra = sqlite3.connect(sum_db)
            try:
                for idx_m in range(42):
                    self._add_slot_conv(convs, brain, conn_extra, f"mainex{idx_m:02d}-0000", 86400, now, title=f"主对话{idx_m}")
            finally:
                conn_extra.close()
            inv_risk = scanner.scan(now_ts=now, slot_cap=50, warn_threshold=38, alert_threshold=45, protect_age_min=120, force=True)
            self.assertTrue(inv_risk.main_at_risk)
            self.assertEqual(inv_risk.status, "main_at_risk")
            self.assertIn("30m", inv_risk.risk_message)

    def test_v7_slot_cleaner_enforces_6_guards_30m_floor_locked_skip_and_archive_sha256(self) -> None:
        from core.slot_manager import MIN_PROTECT_AGE_MIN, SlotInventoryScanner, SlotSubagentCleaner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = time.time()
            convs, brain, sum_db, conn = self._create_slot_env(root, now)
            try:
                self._add_slot_conv(convs, brain, conn, "main-keep-01", 86400, now, title="主对话")
                self._add_slot_conv(convs, brain, conn, "sub-clean-01", 3 * 3600, now, agent_name="w1", nesting_depth=1)
                self._add_slot_conv(convs, brain, conn, "sub-lock-02", 3 * 3600, now, agent_name="w2", nesting_depth=1)
                self._add_slot_conv(convs, brain, conn, "sub-fresh-03", 15 * 60, now, agent_name="w3", nesting_depth=1)
                self._add_slot_conv(convs, brain, conn, "sub-arch-04", 4 * 3600, now, agent_name="w4", nesting_depth=1)
            finally:
                conn.close()

            scanner = SlotInventoryScanner(convs, brain, sum_db)
            recycled_paths: List[str] = []

            def fake_recycle(p: Path):
                if p.name == "sub-lock-02.db":
                    return False, "sharing_violation"
                recycled_paths.append(str(p))
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                return True, ""

            journal = root / "logs" / "slot_cleanup_journal.jsonl"
            cleaner = SlotSubagentCleaner(scanner, journal_path=journal, recycle_fn=fake_recycle)

            # Requesting protect_age_min=5 MUST clamp to 30m hard floor (sub-fresh-03 at 15m MUST survive!)
            res_rec = cleaner.execute_cleanup(protect_age_min=5, mode="recycle", trigger="manual", dry_run=False, now_ts=now)
            self.assertEqual(res_rec.protect_age_min, MIN_PROTECT_AGE_MIN)
            self.assertEqual(res_rec.removed_count, 2)  # sub-clean-01 + sub-arch-04
            self.assertEqual(res_rec.skipped_locked_count, 1)  # sub-lock-02
            # Locked subagent MUST keep its .db, .db-wal, and brain dir untouched!
            self.assertTrue((convs / "sub-lock-02.db").exists())
            self.assertTrue((convs / "sub-lock-02.db-wal").exists())
            self.assertTrue((brain / "sub-lock-02").is_dir())
            # Main conversation and 15m subagent MUST remain untouched!
            self.assertTrue((convs / "main-keep-01.db").exists())
            self.assertTrue((convs / "sub-fresh-03.db").exists())
            self.assertTrue(journal.is_file())

            # Archive mode: missing target drive aborts safely with 0 deletions (dynamically pick an unmounted drive letter)
            conn2 = sqlite3.connect(sum_db)
            self._add_slot_conv(convs, brain, conn2, "sub-tar-05", 4 * 3600, now, agent_name="w5", nesting_depth=1)
            conn2.close()
            missing_drive = next((f"{ch}:" for ch in "QWVYXUPONMLKJIHGF" if not Path(f"{ch}:\\").exists()), "Q:")
            res_bad_arch = cleaner.execute_cleanup(
                protect_age_min=120, mode="archive", archive_dir=f"{missing_drive}/nonexistent_drive_xyz/arch", trigger="manual", dry_run=False, now_ts=now
            )
            self.assertTrue(bool(res_bad_arch.error))
            self.assertIn("archive_drive_missing", res_bad_arch.error)
            self.assertTrue((convs / "sub-tar-05.db").exists())

            # Archive mode with valid local dir: writes PAX tar + SHA-256 verification before removing source
            # (both sub-tar-05 and previously-locked sub-lock-02 are now cleanly archived & removed!)
            arch_dir = root / "archives"
            res_arch = cleaner.execute_cleanup(
                protect_age_min=120, mode="archive", archive_dir=str(arch_dir), trigger="manual", dry_run=False, now_ts=now
            )
            self.assertEqual(res_arch.error, "")
            self.assertEqual(res_arch.removed_count, 2)
            self.assertEqual(set(res_arch.removed_cids), {"sub-lock-02", "sub-tar-05"})
            self.assertFalse((convs / "sub-tar-05.db").exists())
            self.assertTrue(Path(res_arch.archive_path).is_file())

    def test_v7_auto_clean_offline_idle_and_high_water_respects_readonly_and_toggle(self) -> None:
        from core.log_monitor import AccountQuotaInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = time.time()
            convs, brain, sum_db, conn = self._create_slot_env(root, now)
            try:
                self._add_slot_conv(convs, brain, conn, "main-01", 86400, now, title="主对话")
                self._add_slot_conv(convs, brain, conn, "sub-old-01", 3 * 3600, now, agent_name="w1", nesting_depth=1)
            finally:
                conn.close()

            def fake_recycle(p: Path):
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                return True, ""

            base_paths = {
                "brain_dir": brain,
                "conversations_dir": convs,
                "summaries_db_path": sum_db,
                "daemon_dir": root / "daemon",
                "ls_log_path": root / "language_server.log",
                "cockpit_quota_db": root / "missing.sqlite",
                "ledger_path": root / "account_ledger.json",
                "slot_journal_path": root / "slot_journal.jsonl",
            }
            # 1. read_only=True MUST never delete anything
            ro_eng = AntigravityMonitorEngine(paths=base_paths, read_only=True)
            ro_eng.slot_cleaner._recycle_fn = fake_recycle
            ro_eng._account_cache = AccountQuotaInfo(short_label="离线", online=False, observed_at=now)
            ro_eng._last_account_poll = now
            snap_ro = ro_eng.poll(now_ts=now, force_account=False)
            self.assertIsNone(ro_eng.maybe_auto_clean_slots(snap_ro, now_ts=now))
            self.assertIsNone(ro_eng.maybe_auto_clean_slots(snap_ro, now_ts=now + 30.0))
            self.assertTrue((convs / "sub-old-01.db").exists())

            # 2. Writable engine with auto_clean disabled MUST not delete
            eng = AntigravityMonitorEngine(paths=base_paths, config={"slot_auto_clean_enabled": False}, read_only=False)
            eng.slot_cleaner._recycle_fn = fake_recycle
            eng._account_cache = AccountQuotaInfo(short_label="离线", online=False, observed_at=now)
            eng._last_account_poll = now
            snap_off = eng.poll(now_ts=now, force_account=False)
            self.assertIsNone(eng.maybe_auto_clean_slots(snap_off, now_ts=now + 30.0))
            self.assertTrue((convs / "sub-old-01.db").exists())

            # 3. Enable auto_clean -> first offline poll arms timer (<15s no-op), >=15s offline triggers auto_offline!
            eng.update_config({"slot_auto_clean_enabled": True})
            self.assertIsNone(eng.maybe_auto_clean_slots(snap_off, now_ts=now))
            self.assertTrue((convs / "sub-old-01.db").exists())
            res = eng.maybe_auto_clean_slots(snap_off, now_ts=now + 20.0)
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res.trigger, "auto_offline")
            self.assertEqual(res.removed_count, 1)
            self.assertFalse((convs / "sub-old-01.db").exists())
            self.assertTrue((convs / "main-01.db").exists())

    def test_v7_cli_clean_subagents_json_and_slots_window_smoke(self) -> None:
        import tkinter as tk
        from core.slot_manager import SlotConversationItem, SlotInventorySnapshot
        from main import _parse_args
        from ui.slots_window import ConversationSlotsWindow
        from ui.themes import THEMES

        ns = _parse_args(["--clean-subagents", "--dry-run", "--min-age", "15", "--json"])
        self.assertTrue(ns.clean_subagents)
        self.assertTrue(ns.dry_run)
        self.assertEqual(ns.min_age, 15)
        self.assertTrue(ns.json)

        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")

        try:
            now = time.time()
            fake_inv = SlotInventorySnapshot(
                observed_at=now,
                cap=500,
                total_db=488,
                remaining_slots=12,
                main_count=320,
                protected_sub_count=152,
                cleanable_sub_count=16,
                cleanable_bytes=25 * 1024 * 1024,
                cleanable_30m_count=18,
                cleanable_30m_bytes=28 * 1024 * 1024,
                unknown_count=0,
                pinned_floor_count=470,
                protect_age_min=120,
                status="main_at_risk",
                main_at_risk=True,
                risk_message="已撞 30m 保护下限！请先备份主对话再重启",
                summary_db_ok=True,
                total_bytes=420 * 1024 * 1024,
                items=[
                    SlotConversationItem(
                        conversation_id="sub11111-aaaa", short_id="sub111", category="cleanable_sub",
                        title="", agent_name="research", parent_conversation_id="main1111-aaaa",
                        step_count=12, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0,
                        disk_bytes=1500000, last_active_ts=now - 7500, age_min=125.0,
                        cleanable_at_30m=True, protect_reason="已结束闲置 · 125分钟前",
                    ),
                    SlotConversationItem(
                        conversation_id="main1111-aaaa", short_id="main11", category="main",
                        title="主力工作对话", agent_name="", parent_conversation_id="",
                        step_count=88, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0,
                        disk_bytes=8500000, last_active_ts=now - 600, age_min=10.0,
                        cleanable_at_30m=False, protect_reason="主对话（永久保护）",
                    ),
                ],
            )
            sw = ConversationSlotsWindow(
                parent=root,
                theme=THEMES["midnight_blue"],
                load_inventory=lambda: fake_inv,
                on_clean_now=lambda _cb: None,
                is_auto_clean_enabled=lambda: True,
                on_toggle_auto_clean=lambda: None,
                get_protect_age_min=lambda: 120,
                get_clean_mode=lambda: "recycle",
            )
            sw.show(place_near_parent=True)
            root.update()
            self.assertTrue(bool(sw._risk_frame.winfo_manager()))
            self.assertIn("30m", sw._lbl_risk.cget("text"))
            sw._set_tab("main")
            sw.apply_theme(THEMES["obsidian_dark"])
            root.update()
            sw.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_v7_dual_review_hardening_regressions(self) -> None:
        from core.log_monitor import AccountQuotaInfo
        from core.slot_manager import _parse_summary_timestamp_conservative

        # 1. RFC3339Nano 7/8/9 fractional digits + half-hour tz + date-only & bool rejection
        now_utc = datetime.datetime(2026, 11, 21, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        ts_8frac = _parse_summary_timestamp_conservative("2026-11-20T19:00:00.12345678-05:00", now_utc)
        self.assertIsNotNone(ts_8frac)
        self.assertAlmostEqual(ts_8frac or 0.0, now_utc, delta=1.0)
        ts_half_tz = _parse_summary_timestamp_conservative("2026-11-21T05:30:00.123456789+05:30", now_utc)
        self.assertIsNotNone(ts_half_tz)
        self.assertAlmostEqual(ts_half_tz or 0.0, now_utc, delta=1.0)
        self.assertIsNone(_parse_summary_timestamp_conservative("2026-11-21", now_utc))
        self.assertIsNone(_parse_summary_timestamp_conservative(True, now_utc))

        # 2. read_only=True MUST reject clean_old_subagents(dry_run=False)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = time.time()
            convs, brain, sum_db, conn = self._create_slot_env(root, now)
            try:
                self._add_slot_conv(convs, brain, conn, "sub-ro-01", 4 * 3600, now, agent_name="w1", nesting_depth=1)
                self._add_slot_conv(convs, brain, conn, "sub-hw-lock", 4 * 3600, now, agent_name="w2", nesting_depth=1)
                self._add_slot_conv(convs, brain, conn, "sub-hw-45m", 45 * 60, now, agent_name="w3", nesting_depth=1)
                # Fill up to cap=50 so auto_high_water hits alert_th=45
                for i in range(47):
                    self._add_slot_conv(convs, brain, conn, f"main-hw-{i:02d}", 86400, now, title=f"主对话{i}")
            finally:
                conn.close()

            paths = {
                "brain_dir": brain, "conversations_dir": convs, "summaries_db_path": sum_db,
                "daemon_dir": root / "daemon", "ls_log_path": root / "language_server.log",
                "cockpit_quota_db": root / "missing.sqlite", "ledger_path": root / "account_ledger.json",
                "slot_journal_path": root / "slot_journal.jsonl",
            }
            ro_eng = AntigravityMonitorEngine(paths=paths, read_only=True)
            res_ro = ro_eng.clean_old_subagents(dry_run=False, now_ts=now)
            self.assertEqual(res_ro.error, "read_only_mode_forbids_cleanup")
            self.assertTrue((convs / "sub-ro-01.db").exists())

            # 3. Online auto_high_water with 1 locked >2h subagent MUST still step down to 30m and clean unlocked 45m subagent!
            def partial_lock_recycle(p: Path):
                if p.name == "sub-hw-lock.db":
                    return False, "locked_by_ls"
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                return True, ""

            cfg = {"slot_cap": 100, "slot_warn_threshold": 40, "slot_alert_threshold": 46, "slot_auto_trigger_threshold": 45, "slot_auto_clean_enabled": True}
            # Direct config values on engine with cap=50 test override
            eng = AntigravityMonitorEngine(paths=paths, config=cfg, read_only=False)
            eng.config["slot_cap"] = 50
            eng.config["slot_warn_threshold"] = 38
            eng.config["slot_auto_trigger_threshold"] = 42
            eng.config["slot_alert_threshold"] = 45
            eng.slot_cleaner._recycle_fn = partial_lock_recycle
            eng._account_cache = AccountQuotaInfo(short_label="al***1", online=True, observed_at=now)
            eng._last_account_poll = now
            snap_hw = eng.poll(now_ts=now, force_account=False)
            # First-poll main_at_risk / JSON slots contract check
            js = json.loads(format_json_snapshot(snap_hw))
            self.assertIn("remaining_slots", js["slots"])
            self.assertIn("risk_message", js["slots"])
            res_hw = eng.maybe_auto_clean_slots(snap_hw, now_ts=now)
            self.assertIsNotNone(res_hw)
            assert res_hw is not None
            self.assertEqual(res_hw.trigger, "auto_high_water")
            # Both sub-ro-01 (>2h) AND sub-hw-45m (45m, stepped pass!) removed while locked sub-hw-lock safely skipped!
            self.assertFalse((convs / "sub-ro-01.db").exists())
            self.assertFalse((convs / "sub-hw-45m.db").exists())
            self.assertTrue((convs / "sub-hw-lock.db").exists())

    def test_v71_telemetry_503_filtered_and_errorreport_dedup(self) -> None:
        """v7.1: telemetry recordTrajectoryAnalytics 503 must count 0; same-goroutine echoes dedup to 1; labels distinct."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "language_server.log"
            now_dt = datetime.datetime.now()
            mmdd = now_dt.strftime("%m%d")
            hms1 = (now_dt - datetime.timedelta(seconds=20)).strftime("%H:%M:%S")
            hms2 = (now_dt - datetime.timedelta(seconds=10)).strftime("%H:%M:%S")
            hms3 = (now_dt - datetime.timedelta(seconds=5)).strftime("%H:%M:%S")
            hms4 = (now_dt - datetime.timedelta(seconds=2)).strftime("%H:%M:%S")

            lines = [
                # 1. Exact replay of real 10:20:27 telemetry 503 + same-goroutine errorreport echo -> MUST count 0!
                f'ERROR: logging before google.Init: I{mmdd} {hms1}.560447  865341 http_helpers.go:876] Failed to make code assist backend request (https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics): {{',
                '  "error": {',
                '    "code": 503,',
                '    "message": "The service is currently unavailable.",',
                '    "status": "UNAVAILABLE"',
                '  }',
                '}',
                f"ERROR: logging before google.Init: E{mmdd} {hms1}.565732  865341 telemetry.go:141] error recording trajectory segment analytics: UNAVAILABLE (code 503): The service is currently unavailable.",
                f"ERROR: logging before google.Init: E{mmdd} {hms1}.565732  865341 errorreport.go:307] UNAVAILABLE (code 503): The service is currently unavailable.",
                # 2. Real model 503 No capacity echoed by errorreport.go on SAME goroutine -> MUST dedup to 1!
                f"ERROR: logging before google.Init: E{mmdd} {hms2}.100000  910001 planner_generator.go:512] UNAVAILABLE (code 503): No capacity available for model gemini-4-neon-xhigh",
                f"ERROR: logging before google.Init: E{mmdd} {hms2}.100120  910001 errorreport.go:307] UNAVAILABLE (code 503): No capacity available for model gemini-4-neon-xhigh",
                # 3. Genuine concurrent 503 Unavailable on a DIFFERENT goroutine -> MUST count +1 with Unavailable label!
                f"ERROR: logging before google.Init: E{mmdd} {hms3}.200000  910002 stream_client.go:220] UNAVAILABLE (code 503): The service is currently unavailable.",
                # 4. Standalone errorreport.go 503 with no prior ignored telemetry goroutine -> MUST NOT be lost (+1)!
                f"ERROR: logging before google.Init: E{mmdd} {hms4}.300000  910099 errorreport.go:307] UNAVAILABLE (code 503): No capacity available",
            ]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            tailer = LanguageServerLogTailer(log_path)
            now_ts = now_dt.timestamp()
            tailer.poll(now_ts=now_ts)
            recent, today, events = tailer.get_counts(now_ts=now_ts, window_min=30)
            self.assertEqual(recent["503"], 3)
            self.assertEqual(today["503"], 3)
            summaries = [ev.summary for ev in events if ev.category == "503"]
            self.assertEqual(len(summaries), 3)
            self.assertIn("服务端 503 No capacity", summaries[0])
            self.assertIn("服务端 503 Unavailable", summaries[1])
            self.assertIn("服务端 503 No capacity", summaries[2])

    def _build_test_release_zip(self, version: str, extra_files: dict[str, bytes] | None = None, wrap_dir: str = "antigravity-monitor-main") -> bytes:
        import io
        import zipfile

        buf = io.BytesIO()
        base_files = {
            "main.py": f'# main v{version}\n'.encode("utf-8"),
            "version.json": json.dumps({"version": version, "summary": f"release {version}", "repo": "JuryBu/antigravity-monitor", "branch": "main"}).encode("utf-8"),
            "core/log_monitor.py": f'# log_monitor v{version}\n'.encode("utf-8"),
            "core/config_store.py": f'# config_store v{version}\n'.encode("utf-8"),
            "ui/capsule_app.py": f'# capsule_app v{version}\n'.encode("utf-8"),
            "assets/app-icon.ico": b"ICON-UNCHANGED-BYTES",
            "requirements.txt": b"pystray>=0.19.5\nPillow>=10.0.0\n",
        }
        if extra_files:
            base_files.update(extra_files)
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel, data in base_files.items():
                arc = f"{wrap_dir}/{rel}" if wrap_dir else rel
                zf.writestr(arc, data)
        return buf.getvalue()

    def test_v71_updater_version_compare_and_zip_apply_preserves_user_files(self) -> None:
        """v7.1: ZIP/non-Git users get atomic update while user config.json & account_ledger.json remain untouched."""
        from core.updater import UpdateManager
        from core.version import is_newer_version, parse_version_tuple

        self.assertEqual(parse_version_tuple("v7.1.0"), (7, 1, 0))
        self.assertTrue(is_newer_version("7.2.0", "7.1.0"))
        self.assertTrue(is_newer_version("7.1.1", "7.1.0"))
        self.assertFalse(is_newer_version("7.1.0", "7.1.0"))
        self.assertFalse(is_newer_version("7.0.9", "7.1.0"))
        self.assertFalse(is_newer_version("invalid", "7.1.0"))

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "monitor_zip_user"
            (app_dir / "core").mkdir(parents=True)
            (app_dir / "ui").mkdir(parents=True)
            (app_dir / "assets").mkdir(parents=True)
            (app_dir / "version.json").write_text(json.dumps({"version": "7.1.0", "repo": "JuryBu/antigravity-monitor", "branch": "main"}), encoding="utf-8")
            (app_dir / "main.py").write_text("# old main\n", encoding="utf-8")
            (app_dir / "core" / "log_monitor.py").write_text("# old log\n", encoding="utf-8")
            (app_dir / "core" / "config_store.py").write_text("# old cfg\n", encoding="utf-8")
            (app_dir / "ui" / "capsule_app.py").write_text("# old ui\n", encoding="utf-8")
            (app_dir / "assets" / "app-icon.ico").write_bytes(b"ICON-UNCHANGED-BYTES")
            (app_dir / "requirements.txt").write_text("pystray>=0.19.5\nPillow>=10.0.0\n", encoding="utf-8")
            # User private runtime files MUST survive untouched even if archive tries to ship them
            (app_dir / "config.json").write_text('{"theme":"obsidian_dark","auto_update_restart":true}\n', encoding="utf-8")
            (app_dir / "account_ledger.json").write_text('{"accounts":["al***1"]}\n', encoding="utf-8")

            zip_payload = self._build_test_release_zip(
                "7.2.0",
                extra_files={
                    "config.json": b'{"theme":"SHOULD_NEVER_OVERWRITE"}\n',
                    "account_ledger.json": b'{"accounts":["POISON"]}\n',
                    "core/new_module.py": b"# added in 7.2.0\n",
                },
            )
            manifest_payload = json.dumps({
                "version": "7.2.0",
                "released_at": "2026-11-22",
                "summary": "新增特性测试",
                "repo": "JuryBu/antigravity-monitor",
                "branch": "main",
            }).encode("utf-8")

            def fake_http(url: str, timeout: float, max_bytes: int) -> bytes:
                if "version.json" in url:
                    return manifest_payload
                if "/zip/refs/heads/" in url:
                    return zip_payload
                raise AssertionError(f"Unexpected URL: {url}")

            mgr = UpdateManager(app_root=app_dir, http_get_fn=fake_http)
            self.assertEqual(mgr.detect_install_mode(), "zip")
            chk = mgr.check_for_update(is_manual=True)
            self.assertEqual(chk.error, "")
            self.assertTrue(chk.has_update)
            self.assertEqual(chk.remote_version, "7.2.0")

            res = mgr.apply_update(expected_remote_version="7.2.0")
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.method, "zip")
            self.assertTrue(res.needs_restart)
            self.assertEqual(res.to_version, "7.2.0")
            self.assertIn("# main v7.2.0", (app_dir / "main.py").read_text(encoding="utf-8"))
            self.assertTrue((app_dir / "core" / "new_module.py").is_file())
            # Unchanged icon skipped, user private files untouched, backup cleaned up
            self.assertIn("obsidian_dark", (app_dir / "config.json").read_text(encoding="utf-8"))
            self.assertIn("al***1", (app_dir / "account_ledger.json").read_text(encoding="utf-8"))
            self.assertFalse((app_dir / ".update_backup").exists())

    def test_v71_updater_zip_security_and_rollback(self) -> None:
        """v7.1: updater blocks untrusted hosts, Zip-Slip paths, malformed archives, and rolls back on write errors."""
        import io
        import zipfile
        from unittest import mock
        from core.updater import UpdateManager, _validate_https_url

        with self.assertRaises(ValueError):
            _validate_https_url("http://raw.githubusercontent.com/JuryBu/antigravity-monitor/main/version.json")
        with self.assertRaises(ValueError):
            _validate_https_url("https://evil.example.com/payload.zip")

        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "app"
            (app_dir / "core").mkdir(parents=True)
            (app_dir / "ui").mkdir(parents=True)
            (app_dir / "version.json").write_text('{"version":"7.1.0"}', encoding="utf-8")
            (app_dir / "main.py").write_text("# original v7.1.0\n", encoding="utf-8")
            (app_dir / "core" / "log_monitor.py").write_text("# orig log\n", encoding="utf-8")
            (app_dir / "core" / "config_store.py").write_text("# orig cfg\n", encoding="utf-8")
            (app_dir / "ui" / "capsule_app.py").write_text("# orig ui\n", encoding="utf-8")

            # 1. Zip Slip archive MUST be rejected cleanly without writing outside app_dir
            slip_buf = io.BytesIO()
            with zipfile.ZipFile(slip_buf, "w") as zf:
                zf.writestr("../escaped.txt", b"pwn")
                zf.writestr("main.py", b"# bad")
            slip_bytes = slip_buf.getvalue()
            mgr_slip = UpdateManager(app_root=app_dir, http_get_fn=lambda u, t, m: slip_bytes)
            res_slip = mgr_slip.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_slip.ok)
            self.assertIn("非法路径", res_slip.error)
            self.assertFalse((Path(td) / "escaped.txt").exists())

            # 2. Mid-write OSError MUST roll back already replaced files to original 7.1.0 state
            good_zip = self._build_test_release_zip("7.2.0", extra_files={"core/brand_new.py": b"# new\n"})
            mgr_rb = UpdateManager(app_root=app_dir, http_get_fn=lambda u, t, m: good_zip)
            real_replace = os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst):
                calls["n"] += 1
                if calls["n"] >= 3:
                    raise PermissionError("simulated file lock")
                return real_replace(src, dst)

            with mock.patch("core.updater.os.replace", side_effect=flaky_replace):
                res_rb = mgr_rb.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_rb.ok)
            self.assertIn("回滚", res_rb.error)
            self.assertEqual((app_dir / "main.py").read_text(encoding="utf-8"), "# original v7.1.0\n")
            self.assertEqual(json.loads((app_dir / "version.json").read_text(encoding="utf-8"))["version"], "7.1.0")
            self.assertFalse((app_dir / "core" / "brand_new.py").exists())

    def test_v71_updater_git_dirty_guard_and_ff_update(self) -> None:
        """v7.1: Git mode protects uncommitted edits and cleanly fast-forwards clean checkouts."""
        import subprocess
        from core.updater import UpdateManager

        if shutil.which("git") is None:
            self.skipTest("git not installed")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            remote = root / "remote.git"
            work_up = root / "up"
            local = root / "local"

            def git(cwd: Path, *args: str) -> None:
                subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

            remote.mkdir()
            git(remote, "init", "--bare", "-b", "main")
            work_up.mkdir()
            git(work_up, "init", "-b", "main")
            git(work_up, "config", "user.email", "test@example.com")
            git(work_up, "config", "user.name", "Test")
            (work_up / "version.json").write_text('{"version":"7.1.0","repo":"JuryBu/antigravity-monitor","branch":"main"}\n', encoding="utf-8")
            (work_up / "main.py").write_text("# v7.1.0\n", encoding="utf-8")
            git(work_up, "add", ".")
            git(work_up, "commit", "-m", "v7.1.0")
            git(work_up, "remote", "add", "origin", str(remote))
            git(work_up, "push", "-u", "origin", "main")

            subprocess.run(["git", "clone", "-b", "main", str(remote), str(local)], check=True, capture_output=True)

            # Push v7.2.0 to remote
            (work_up / "version.json").write_text('{"version":"7.2.0","repo":"JuryBu/antigravity-monitor","branch":"main"}\n', encoding="utf-8")
            (work_up / "main.py").write_text("# v7.2.0\n", encoding="utf-8")
            git(work_up, "add", ".")
            git(work_up, "commit", "-m", "v7.2.0")
            git(work_up, "push", "origin", "main")

            mgr = UpdateManager(app_root=local)
            self.assertEqual(mgr.detect_install_mode(), "git")

            # 1. Dirty local working tree MUST be protected
            (local / "main.py").write_text("# local unsaved edit\n", encoding="utf-8")
            res_dirty = mgr.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_dirty.ok)
            self.assertTrue(res_dirty.dirty_blocked)
            self.assertEqual((local / "main.py").read_text(encoding="utf-8"), "# local unsaved edit\n")

            # 2. Clean checkout MUST fast-forward cleanly to 7.2.0
            git(local, "checkout", "--", "main.py")
            res_ff = mgr.apply_update(expected_remote_version="7.2.0")
            self.assertTrue(res_ff.ok, res_ff.error)
            self.assertEqual(res_ff.method, "git")
            self.assertTrue(res_ff.needs_restart)
            self.assertEqual(res_ff.to_version, "7.2.0")
            self.assertEqual((local / "main.py").read_text(encoding="utf-8"), "# v7.2.0\n")

    def test_v71_config_update_options_and_auto_loop_guard(self) -> None:
        """v7.1: update config options clamp safely and failed auto-update versions are suppressed from retry loops."""
        from core.config_store import sanitize_config
        from core.updater import UpdateManager

        cfg = sanitize_config({
            "update_check_enabled": True,
            "auto_update_restart": True,
            "update_check_interval_min": 2,
            "update_ignored_version": " 7.2.0 ",
        })
        self.assertTrue(cfg["update_check_enabled"])
        self.assertTrue(cfg["auto_update_restart"])
        self.assertEqual(cfg["update_check_interval_min"], 10)  # clamped >= 10
        self.assertEqual(cfg["update_ignored_version"], "7.2.0")

        with tempfile.TemporaryDirectory() as td:
            mgr = UpdateManager(app_root=Path(td))
            self.assertFalse(mgr.has_failed_auto_version("7.2.0"))
            # Transient failures retry up to 3 times before stopping; permanent locks immediately
            mgr.mark_auto_version_failed("7.2.0", permanent=False)
            mgr.mark_auto_version_failed("7.2.0", permanent=False)
            self.assertFalse(mgr.has_failed_auto_version("7.2.0"))
            mgr.mark_auto_version_failed("7.2.0", permanent=False)
            self.assertTrue(mgr.has_failed_auto_version("7.2.0"))
            mgr.clear_failed_auto_version("7.2.0")
            self.assertFalse(mgr.has_failed_auto_version("7.2.0"))
            mgr.mark_auto_version_failed("7.2.0", permanent=True)
            self.assertTrue(mgr.has_failed_auto_version("7.2.0"))

    def test_v71_dual_review_hardening_regressions(self) -> None:
        """v7.1 dual-review regressions: multiline model 503 dedup, INFO telemetry non-muting,
        per-hop redirect allowlist, ZIP bombs/drive-colon/version-last, Git untracked/branch/lag, SemVer prerelease."""
        import io
        import subprocess
        import urllib.request
        import zipfile
        from core.config_store import sanitize_config
        from core.updater import UpdateManager, _SafeRedirectHandler, _validate_https_url
        from core.version import is_newer_version

        # 1. SemVer prerelease & config coupling
        self.assertTrue(is_newer_version("7.2.0", "7.2.0-beta.1"))
        self.assertFalse(is_newer_version("7.2.0-beta.1", "7.2.0"))
        cfg_off = sanitize_config({"update_check_enabled": False, "auto_update_restart": True})
        self.assertFalse(cfg_off["update_check_enabled"])
        self.assertFalse(cfg_off["auto_update_restart"])

        # 2. Per-hop redirect validation & port/userinfo blocking
        with self.assertRaises(ValueError):
            _validate_https_url("https://user:pass@raw.githubusercontent.com/JuryBu/antigravity-monitor/main/version.json")
        with self.assertRaises(ValueError):
            _validate_https_url("https://raw.githubusercontent.com:8443/JuryBu/antigravity-monitor/main/version.json")
        handler = _SafeRedirectHandler(max_hops=2)
        req = urllib.request.Request("https://github.com/JuryBu/antigravity-monitor/archive/refs/heads/main.zip")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example.com/bad.zip")

        # 3. Multiline real model 503 JSON + caller + errorreport MUST count strictly 1 (never 2->T2),
        #    and normal INFO log on ignored source MUST NOT mute a real 403/timeout on the same goroutine!
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "language_server.log"
            now_dt = datetime.datetime.now()
            mmdd = now_dt.strftime("%m%d")
            hms1 = (now_dt - datetime.timedelta(seconds=20)).strftime("%H:%M:%S")
            hms2 = (now_dt - datetime.timedelta(seconds=10)).strftime("%H:%M:%S")
            lines = [
                # Real streamGenerateContent 503 with multi-line JSON + caller + errorreport on same goroutine 77001 -> strictly 1!
                f'ERROR: logging before google.Init: I{mmdd} {hms1}.100000  77001 http_helpers.go:876] Failed to make code assist backend request (https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse): {{',
                '  "error": {',
                '    "code": 503,',
                '    "message": "No capacity available for model gemini-4-neon-xhigh",',
                '    "status": "RESOURCE_EXHAUSTED"',
                '  }',
                '}',
                f"ERROR: logging before google.Init: E{mmdd} {hms1}.105000  77001 planner_generator.go:512] UNAVAILABLE (code 503): No capacity available",
                f"ERROR: logging before google.Init: E{mmdd} {hms1}.105100  77001 errorreport.go:307] UNAVAILABLE (code 503): No capacity available",
                # Normal INFO line from telemetry.go on goroutine 77002 followed by real 403 -> 403 MUST NOT be muted!
                f"ERROR: logging before google.Init: I{mmdd} {hms2}.100000  77002 telemetry.go:88] flushed metrics batch ok",
                f"ERROR: logging before google.Init: E{mmdd} {hms2}.200000  77002 errorreport.go:307] PERMISSION_DENIED (code 403): TOS_VIOLATION",
                # Background loadCodeAssist 503 + errorreport echo on goroutine 77003 -> strictly 0!
                f'ERROR: logging before google.Init: I{mmdd} {hms2}.500000  77003 http_helpers.go:876] Failed to make code assist backend request (https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist): {{',
                '  "error": {"code": 503, "message": "No capacity"}',
                '}',
                f"ERROR: logging before google.Init: E{mmdd} {hms2}.505000  77003 errorreport.go:307] UNAVAILABLE (code 503): The service is currently unavailable.",
            ]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tailer = LanguageServerLogTailer(log_path)
            tailer.poll(now_ts=now_dt.timestamp())
            recent, _today, _evs = tailer.get_counts(now_ts=now_dt.timestamp(), window_min=30)
            self.assertEqual(recent["503"], 1)
            self.assertEqual(recent["403"], 1)

        # 4. ZIP entry bomb & nested drive-colon rejection & corrupted version.json rejection
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "app"
            (app_dir / "core").mkdir(parents=True)
            (app_dir / "ui").mkdir(parents=True)
            (app_dir / "version.json").write_text('{"version":"7.1.0"}', encoding="utf-8")
            (app_dir / "main.py").write_text("# v7.1.0\n", encoding="utf-8")
            (app_dir / "core" / "log_monitor.py").write_text("# v7.1.0\n", encoding="utf-8")
            (app_dir / "core" / "config_store.py").write_text("# v7.1.0\n", encoding="utf-8")
            (app_dir / "ui" / "capsule_app.py").write_text("# v7.1.0\n", encoding="utf-8")

            # Nested drive colon in second segment
            colon_buf = io.BytesIO()
            with zipfile.ZipFile(colon_buf, "w") as zf:
                zf.writestr("wrap/C:evil.txt", b"x")
                zf.writestr("wrap/main.py", b"x")
            mgr_c = UpdateManager(app_root=app_dir, http_get_fn=lambda u, t, m: colon_buf.getvalue())
            res_c = mgr_c.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_c.ok)
            self.assertIn("非法路径", res_c.error)

            # Corrupt version.json inside archive must report error (NOT "already latest")
            bad_ver_zip = self._build_test_release_zip("7.2.0", extra_files={"version.json": b"{corrupt json"})
            mgr_bv = UpdateManager(app_root=app_dir, http_get_fn=lambda u, t, m: bad_ver_zip)
            res_bv = mgr_bv.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_bv.ok)
            self.assertIn("version.json", res_bv.error)

            # Entry count bomb (>500 files)
            bomb_buf = io.BytesIO()
            with zipfile.ZipFile(bomb_buf, "w") as zf:
                for idx in range(505):
                    zf.writestr(f"wrap/f_{idx}.txt", b"a")
            mgr_b = UpdateManager(app_root=app_dir, http_get_fn=lambda u, t, m: bomb_buf.getvalue())
            res_b = mgr_b.apply_update(expected_remote_version="7.2.0")
            self.assertFalse(res_b.ok)
            self.assertIn("条目", res_b.error)

        # 5. Git mode: untracked unignored file & origin lagging behind target version MUST block cleanly
        if shutil.which("git") is not None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                remote = root / "remote.git"
                up = root / "up"
                loc = root / "loc"
                remote.mkdir()
                up.mkdir()
                subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=str(remote), check=True, capture_output=True)
                subprocess.run(["git", "init", "-b", "main"], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "T"], cwd=str(up), check=True, capture_output=True)
                (up / "version.json").write_text('{"version":"7.1.0","repo":"JuryBu/antigravity-monitor","branch":"main"}\n', encoding="utf-8")
                (up / "main.py").write_text("# v7.1.0\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(up), check=True, capture_output=True)
                subprocess.run(["git", "clone", "-b", "main", str(remote), str(loc)], check=True, capture_output=True)

                mgr_g = UpdateManager(app_root=loc)
                # Untracked file blocks safely
                (loc / "untracked_conflict.py").write_text("# local\n", encoding="utf-8")
                r_un = mgr_g.apply_update(expected_remote_version="7.2.0")
                self.assertFalse(r_un.ok)
                self.assertTrue(r_un.dirty_blocked)
                (loc / "untracked_conflict.py").unlink()
                # Remote origin still at 7.1.0 while target is 7.2.0 -> returns ok=False (no silent loop)
                r_lag = mgr_g.apply_update(expected_remote_version="7.2.0")
                self.assertFalse(r_lag.ok)
                self.assertTrue(r_lag.dirty_blocked)

    def test_v711_daily_steps_alert_reminds_without_overriding_t0_grade(self) -> None:
        """v7.1.1: exceeding daily step warn/alert thresholds must remind independently without overwriting real T0/T1/T2 grade."""
        from core.log_monitor import ConversationSummary, compute_pressure

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eng = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": root / "language_server.log",
                    "cockpit_quota_db": root / "missing.sqlite",
                    "ledger_path": root / "account_ledger.json",
                },
                read_only=True,
            )
            now = time.time()
            healthy_conv = ConversationSummary(
                conversation_id="step-ok-01",
                short_id="step-o",
                model="Neon XHigh",
                model_raw="gemini-4-neon-xhigh",
                context_tokens=150000,
                max_context_tokens=512000,
                latest_hit_rate=0.99,
                latest_cached_in=148500,
                latest_uncached_in=1500,
                latest_timestamp=now - 10,
                recent_steps=[],
                consec_miss=0,
                consec_miss_start_step=0,
                single_miss=False,
                wasted_cold_tokens=0,
                freshness_remaining_sec=180,
                ttft_sec=12.0,
                tps=140.0,
                speed_status="ok",
                title="正常对话",
                is_subagent=False,
            )
            zero_log = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
            level, grade, headline, sub, _ = eng._evaluate_health(
                active_model="Neon XHigh",
                recent_log=zero_log,
                today_log=zero_log,
                max_consec_miss=0,
                any_single_miss=False,
                any_slow=False,
                today_steps=1280,
                primary=healthy_conv,
                is_online=True,
                now_ts=now,
            )
            self.assertEqual(grade, "T0")
            self.assertEqual(level, "green")
            self.assertIn("T0 健康", headline)
            self.assertIn("1280/1200", sub)
            # CLI pressure still flags "red" for automation throttling
            from core.log_monitor import compute_pressure
            self.assertEqual(compute_pressure(grade, 1280, 960, 1200), "red")


if __name__ == "__main__":
    unittest.main()
