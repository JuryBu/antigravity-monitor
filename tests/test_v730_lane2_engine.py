"""Unit tests for v7.3.0 Lane 2 core diagnostic engine (core/log_monitor.py)."""

from __future__ import annotations

import datetime
import http.server
import json
import os
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.account_ledger import AccountSwitchLedger
from core.log_monitor import (
    FRESHNESS_RESCUE_WINDOW_SEC,
    AccountQuotaInfo,
    AntigravityMonitorEngine,
    ConversationSummary,
    LogEvent,
    ModelPerfStats,
    ModelSpeedThresholds,
    QuotaPoolInfo,
    StepRecord,
    classify_speed_model_family,
    classify_steps,
    compute_quota_pool_velocity,
    format_countdown,
    format_freshness_status,
    format_json_snapshot,
    format_model_name,
    normalize_model_compare_key,
    reconcile_model_switches_from_gens,
    resolve_model_freshness_window_sec,
    resolve_model_speed_thresholds,
    resolve_tier_display,
)
from core.slot_manager import SlotCleanupResult, SlotInventorySnapshot
from core.sqlite_reader import GenTelemetry


def _iso_utc(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _varint(val: int) -> bytes:
    out = bytearray()
    v = int(val)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_varint(fn: int, val: int) -> bytes:
    return _varint((fn << 3) | 0) + _varint(val)


def _pb_bytes(fn: int, data: bytes) -> bytes:
    return _varint((fn << 3) | 2) + _varint(len(data)) + data


def _build_gen_blob(
    uncached_in: int,
    cached_in: int,
    out_tok: int,
    *,
    ttft_s: float = 5.0,
    strm_s: float = 4.0,
    model_label: str = "",
    max_ctx: int = 512000,
) -> bytes:
    usage = _pb_varint(2, uncached_in) + _pb_varint(3, out_tok) + _pb_varint(5, cached_in)
    ttft_msg = _pb_varint(1, int(ttft_s)) + _pb_varint(2, int(round((ttft_s - int(ttft_s)) * 1e9)))
    strm_msg = _pb_varint(1, int(strm_s)) + _pb_varint(2, int(round((strm_s - int(strm_s)) * 1e9)))
    m10 = _pb_varint(4, max_ctx)
    m9 = _pb_bytes(10, m10)
    m1 = _pb_bytes(4, usage) + _pb_bytes(9, m9) + _pb_bytes(11, ttft_msg) + _pb_bytes(12, strm_msg)
    if model_label:
        m1 += _pb_bytes(21, model_label.encode("utf-8"))
    return _pb_bytes(1, m1)


def _make_gen(
    idx: int,
    total_in: int,
    cached_in: int,
    out_tok: int = 600,
    *,
    ttft_sec: Optional[float] = 10.0,
    streaming_sec: Optional[float] = 5.0,
    tps: Optional[float] = None,
    model_label: str = "",
) -> GenTelemetry:
    eff_tps = tps if tps is not None else ((out_tok / streaming_sec) if (streaming_sec and streaming_sec >= 0.15) else None)
    return GenTelemetry(
        idx=idx,
        uncached_in=max(0, total_in - cached_in),
        cached_in=cached_in,
        total_in=total_in,
        output_tokens=out_tok,
        thinking_tokens=0,
        reply_tokens=out_tok,
        ttft_sec=ttft_sec,
        streaming_sec=streaming_sec,
        tps=eff_tps,
        max_context_tokens=512000,
        response_id=f"resp_{idx:012d}",
        shard_tag="abcd",
        retry_error="",
        model_label=model_label,
    )


def _make_engine(root: Path, *, read_only: bool = True, config: Optional[Dict[str, Any]] = None) -> AntigravityMonitorEngine:
    (root / "brain").mkdir(parents=True, exist_ok=True)
    (root / "conversations").mkdir(parents=True, exist_ok=True)
    (root / "daemon").mkdir(parents=True, exist_ok=True)
    ls_log = root / "language_server.log"
    if not ls_log.exists():
        ls_log.write_text("", encoding="utf-8")
    return AntigravityMonitorEngine(
        config=config,
        paths={
            "brain_dir": root / "brain",
            "conversations_dir": root / "conversations",
            "daemon_dir": root / "daemon",
            "ls_log_path": ls_log,
            "cockpit_quota_db": root / "missing_cockpit.sqlite",
            "ledger_path": root / "account_ledger.json",
        },
        read_only=read_only,
    )


class TestV730CacheCorrections(unittest.TestCase):
    def test_model_enum_fallbacks_and_compare_key(self) -> None:
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M400"), "Opus 5.5")
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M401"), "Opus 5.5")
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M402"), "Opus 5.5")
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M403"), "Sonnet 5.5")
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M404"), "Sonnet 5.5")
        self.assertEqual(format_model_name("MODEL_PLACEHOLDER_M405"), "Sonnet 5.5")

        k1 = normalize_model_compare_key("MODEL_PLACEHOLDER_M535")
        k2 = normalize_model_compare_key("Gemini 4 Neon (XHigh)")
        k3 = normalize_model_compare_key("gemini-4-neon-xhigh")
        self.assertEqual(k1, "gemini-4-neon-xhigh")
        self.assertEqual(k1, k2)
        self.assertEqual(k2, k3)
        self.assertEqual(
            normalize_model_compare_key("gemini-4-hydrogen-tiered-xhigh"),
            normalize_model_compare_key("Gemini 4 Hydrogen (XHigh)"),
        )
        self.assertEqual(normalize_model_compare_key(""), "")

    def test_classify_steps_empty_model_cursor_and_adaptive_idle(self) -> None:
        base_ts = 1795400000.0
        raw = [
            {"step_index": 1, "timestamp": base_ts, "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
            {"step_index": 2, "timestamp": base_ts + 20, "model": "", "input_tokens": 2000, "cache_read_tokens": 100000, "output_tokens": 200},
            {"step_index": 3, "timestamp": base_ts + 40, "model": "gemini-4-carbon-high", "input_tokens": 105000, "cache_read_tokens": 0, "output_tokens": 200},
        ]
        res = classify_steps(raw, idle_rewarm_sec=180)
        # Empty model on step 2 MUST NOT wipe prev_model -> step 3 detects real model_switch!
        self.assertEqual(res[2].classification, "expected_cold")
        self.assertEqual(res[2].reason, "model_switch")
        self.assertEqual(res[2].wasted_cold_tokens, 0)

        # Default use_model_adaptive_idle=False honors literal idle_rewarm_sec=180
        raw_idle = [
            {"step_index": 1, "timestamp": base_ts, "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
            {"step_index": 2, "timestamp": base_ts + 200, "model": "gemini-4-neon-xhigh", "input_tokens": 105000, "cache_read_tokens": 0, "output_tokens": 200},
        ]
        res_strict = classify_steps(raw_idle, idle_rewarm_sec=180)
        self.assertEqual(res_strict[1].reason, "idle_rewarm")
        # With use_model_adaptive_idle=True, Neon base=210 -> 240s, so 200s gap is abnormal_zero while Carbon (180s) is idle_rewarm
        res_adapt_neon = classify_steps(raw_idle, idle_rewarm_sec=210, use_model_adaptive_idle=True)
        self.assertEqual(res_adapt_neon[1].reason, "abnormal_zero")
        raw_carbon = [
            {"step_index": 1, "timestamp": base_ts, "model": "gemini-4-carbon-high", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
            {"step_index": 2, "timestamp": base_ts + 185, "model": "gemini-4-carbon-high", "input_tokens": 105000, "cache_read_tokens": 0, "output_tokens": 200},
        ]
        res_adapt_carbon = classify_steps(raw_carbon, idle_rewarm_sec=210, use_model_adaptive_idle=True)
        self.assertEqual(res_adapt_carbon[1].reason, "idle_rewarm")

    def test_partial_miss_blind_spots_and_wasted_cold_formula(self) -> None:
        base_ts = 1795400000.0
        raw = [
            # Step 1: 40K first_turn
            {"step_index": 1, "timestamp": base_ts, "model": "gemini-4-neon-xhigh", "input_tokens": 40000, "cache_read_tokens": 0, "output_tokens": 200},
            # Step 2: 32K~50K partial miss (prev=40K, cr=10K -> loss=30K >= 8K, hit=22.2% < 50%)
            {"step_index": 2, "timestamp": base_ts + 10, "model": "gemini-4-neon-xhigh", "input_tokens": 35000, "cache_read_tokens": 10000, "output_tokens": 200},
            # Step 3: build up to 100K hit
            {"step_index": 3, "timestamp": base_ts + 20, "model": "gemini-4-neon-xhigh", "input_tokens": 60000, "cache_read_tokens": 40000, "output_tokens": 200},
            # Step 4: 50%~75% blind spot (prev=100K, cr=60K -> loss=40K >= 16K, cr/prev=0.60 < 0.75, hit=57.1% < 75%)
            {"step_index": 4, "timestamp": base_ts + 30, "model": "gemini-4-neon-xhigh", "input_tokens": 45000, "cache_read_tokens": 60000, "output_tokens": 200},
            # Step 5: cr=0 abnormal_zero (prev=105K, total=110K -> wasted = min(105K, 110K) - 0 = 105K, NOT 110K!)
            {"step_index": 5, "timestamp": base_ts + 40, "model": "gemini-4-neon-xhigh", "input_tokens": 110000, "cache_read_tokens": 0, "output_tokens": 200},
            # Step 6: cached_diluted (prev=110K, cr=100K >= 0.85*110K, total=200K -> hit=50% < 70%) -> MUST remain hit!
            {"step_index": 6, "timestamp": base_ts + 50, "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 100000, "output_tokens": 200},
        ]
        res = classify_steps(raw, idle_rewarm_sec=180)
        self.assertEqual(res[1].reason, "partial_miss")
        self.assertEqual(res[1].wasted_cold_tokens, 30000)
        self.assertEqual(res[3].reason, "partial_miss")
        self.assertEqual(res[3].wasted_cold_tokens, 40000)
        self.assertEqual(res[4].reason, "abnormal_zero")
        self.assertEqual(res[4].wasted_cold_tokens, 105000)
        self.assertEqual(res[5].classification, "hit")
        self.assertEqual(res[5].reason, "cached_diluted")
        self.assertEqual(res[5].wasted_cold_tokens, 0)

    def test_reconcile_model_switches_tail_aligned_and_anti_whitewash(self) -> None:
        base_ts = 1795400000.0
        # 20 steps: steps 0..17 Neon, step 18 empty model cr=0 (SQLite says Carbon -> switch), step 19 empty model cr=0 (SQLite empty -> stays abnormal_zero!)
        raw = [{"step_index": 0, "timestamp": base_ts, "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200}]
        for i in range(1, 18):
            raw.append({"step_index": i, "timestamp": base_ts + i * 10, "model": "gemini-4-neon-xhigh", "input_tokens": 1000, "cache_read_tokens": 100000 + (i - 1) * 1000, "output_tokens": 200})
        raw.append({"step_index": 18, "timestamp": base_ts + 180, "model": "", "input_tokens": 119000, "cache_read_tokens": 0, "output_tokens": 200})
        raw.append({"step_index": 19, "timestamp": base_ts + 190, "model": "", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 200})
        steps = classify_steps(raw, idle_rewarm_sec=180)
        self.assertEqual(steps[18].reason, "abnormal_zero")
        self.assertEqual(steps[19].reason, "abnormal_zero")

        # Fast-path aligned_gens of length 10 (tail-aligned to steps[10:20])
        aligned_tail: List[Optional[Any]] = [
            _make_gen(i, steps[i].total_tokens, steps[i].cache_read_tokens, model_label="Gemini 4 Neon (XHigh)")
            for i in range(10, 18)
        ]
        aligned_tail.append(_make_gen(18, 119000, 0, model_label="Gemini 4 Carbon (High)"))
        aligned_tail.append(_make_gen(19, 120000, 0, model_label=""))  # Missing real model -> MUST NOT whitewash!

        reconciled, changed = reconcile_model_switches_from_gens(steps, aligned_tail)
        self.assertTrue(changed)
        self.assertEqual(reconciled[18].classification, "expected_cold")
        self.assertEqual(reconciled[18].reason, "model_switch")
        self.assertEqual(reconciled[18].wasted_cold_tokens, 0)
        # Step 19 had no real SQLite model_label -> MUST stay abnormal_zero
        self.assertEqual(reconciled[19].classification, "abnormal_miss")
        self.assertEqual(reconciled[19].reason, "abnormal_zero")
        # Original list untouched (immutable copy)
        self.assertEqual(steps[18].reason, "abnormal_zero")

    def test_end_to_end_reconcile_clears_freshness_invalid_and_updates_enum_cache_sig(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = 1795401000.0
            cid = "rec00001-1111-2222-3333-444444444444"
            tdir = root / "brain" / cid / ".system_generated" / "logs"
            tdir.mkdir(parents=True)
            rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(now - 60), "content": "hi"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(now - 50), "model": "gemini-4-neon-xhigh", "input_tokens": 120000, "cache_read_tokens": 0, "output_tokens": 600},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(now - 35), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 120000, "output_tokens": 600},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": _iso_utc(now - 20), "model": "", "input_tokens": 125000, "cache_read_tokens": 0, "output_tokens": 600},
            ]
            tp = tdir / "transcript.jsonl"
            tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(tp, (now - 20, now - 20))

            db_path = root / "conversations" / f"{cid}.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (_build_gen_blob(120000, 0, 600, model_label="Gemini 4 Neon (XHigh)"),))
            conn.execute("INSERT INTO gen_metadata VALUES (2, ?)", (_build_gen_blob(2000, 120000, 600, model_label="Gemini 4 Neon (XHigh)"),))
            conn.execute("INSERT INTO gen_metadata VALUES (3, ?)", (_build_gen_blob(125000, 0, 600, model_label="Gemini 4 Carbon (High)"),))
            conn.commit()
            conn.close()

            eng = _make_engine(root, read_only=True)
            eng._account_cache = AccountQuotaInfo(short_label="de***1", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T0")
            self.assertEqual(snap.max_consec_miss, 0)
            c = snap.active_conversations[0]
            self.assertEqual(c.latest_cache_reason, "model_switch")
            self.assertEqual(c.freshness_invalid_reason, "")
            self.assertIsNotNone(c.freshness_remaining_sec)
            self.assertEqual(c.freshness_zone, "safe")
            self.assertEqual(c.freshness_window_sec, 180)  # Carbon window

            # Detail report must also reconcile consistently
            rep = eng.read_conversation_detail(cid, now_ts=now)
            self.assertIsNotNone(rep)
            assert rep is not None
            self.assertEqual(rep.abnormal_miss_steps, 0)
            self.assertEqual(rep.points[-1].reason, "model_switch")
            self.assertEqual(rep.freshness_window_sec, 180)

            # Verify _classify_steps_cached invalidates when _model_enum_map changes
            raw_enum = [
                {"step_index": 1, "timestamp": now - 50, "model": "MODEL_CUSTOM_X", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
                {"step_index": 2, "timestamp": now - 30, "model": "gemini-4-neon-xhigh", "input_tokens": 105000, "cache_read_tokens": 0, "output_tokens": 200},
            ]
            s_before = eng._classify_steps_cached("enum_test", raw_enum, [])
            self.assertEqual(s_before[1].reason, "model_switch")
            eng._model_enum_map["MODEL_CUSTOM_X"] = "Gemini 4 Neon (XHigh)"
            eng._model_enum_map_version += 1
            s_after = eng._classify_steps_cached("enum_test", raw_enum, [])
            self.assertEqual(s_after[1].reason, "abnormal_zero")

            # Verify _hist_daily_profiles_cache_key strictly remains an 8-tuple!
            self.assertEqual(len(eng._hist_daily_profiles_cache_key), 8)

    def test_subagent_denoise_no_historical_veto_and_light_swarm_buffer(self) -> None:
        now = 1795405000.0
        # Main conv had 1 single miss 20 minutes ago, followed by 5 hits inside last 10m -> healthy!
        main_steps = [
            StepRecord(1, now - 1200, "gemini-4-neon-xhigh", 100000, 0, 200, 100000, 0.0, "abnormal_miss", "abnormal_zero", 0),
            StepRecord(2, now - 300, "gemini-4-neon-xhigh", 2000, 100000, 200, 102000, 98.0, "hit", "cached", 2000),
            StepRecord(3, now - 200, "gemini-4-neon-xhigh", 2000, 102000, 200, 104000, 98.1, "hit", "cached", 2000),
        ]
        main_c = ConversationSummary(
            conversation_id="main1", short_id="main1", model="Neon XHigh", model_raw="gemini-4-neon-xhigh",
            context_tokens=104000, max_context_tokens=512000, latest_hit_rate=98.1, latest_cached_in=102000,
            latest_uncached_in=2000, latest_timestamp=now - 200, recent_steps=main_steps, consec_miss=0,
            consec_miss_start_step=3, single_miss=False, wasted_cold_tokens=100000, freshness_remaining_sec=40,
            abnormal_in_recent10=1, cacheable_in_recent10=3, recent10_hit_ratio=2 / 3,
            tail_consec_hits_after_abnormal=2, last_abnormal_ts=now - 1200, is_subagent=False,
            _scoped_steps_all=main_steps,
        )

        def _make_sub(idx: int, model_raw: str, single_miss: bool) -> ConversationSummary:
            st_list = [
                StepRecord(1, now - 100, model_raw, 80000, 0, 200, 80000, 0.0, "expected_cold", "first_turn"),
                StepRecord(2, now - 50, model_raw, 82000 if single_miss else 1000, 0 if single_miss else 80000, 200, 82000, 0.0 if single_miss else 98.0,
                           "abnormal_miss" if single_miss else "hit", "abnormal_zero" if single_miss else "cached"),
            ]
            return ConversationSummary(
                conversation_id=f"sub{idx}", short_id=f"sub{idx:03d}", model=format_model_name(model_raw), model_raw=model_raw,
                context_tokens=82000, max_context_tokens=512000, latest_hit_rate=0.0 if single_miss else 98.0,
                latest_cached_in=0 if single_miss else 80000, latest_uncached_in=82000 if single_miss else 1000,
                latest_timestamp=now - 50, recent_steps=st_list, consec_miss=1 if single_miss else 0,
                consec_miss_start_step=2, single_miss=single_miss, wasted_cold_tokens=80000 if single_miss else 0,
                freshness_remaining_sec=None, abnormal_in_recent10=1 if single_miss else 0,
                cacheable_in_recent10=1, recent10_hit_ratio=0.0 if single_miss else 1.0,
                last_abnormal_ts=now - 50 if single_miss else 0.0, is_subagent=True,
                _scoped_steps_all=st_list,
            )

        # 1) 8 Hydrogen subagents, 2 single_miss (<3) -> buffered/denoised even though main had 1 old miss 20m ago!
        subs_8_2 = [_make_sub(i, "gemini-4-hydrogen-xhigh", i < 2) for i in range(8)]
        _, any_sm_2, _, _ = AntigravityMonitorEngine._select_cache_health_signals(
            [main_c] + subs_8_2, now_ts=now, eval_cutoff_ts=now - 1800, eval_end_ts=now,
            is_historical_rating=False, hr_key="30m", is_custom_account_view=False,
        )
        self.assertFalse(any_sm_2)

        # 2) 8 Hydrogen subagents, 3 single_miss (3/8 = 37.5% >= 25%) -> raises any_single_miss=True!
        subs_8_3 = [_make_sub(i, "gemini-4-hydrogen-xhigh", i < 3) for i in range(8)]
        _, any_sm_3, _, _ = AntigravityMonitorEngine._select_cache_health_signals(
            [main_c] + subs_8_3, now_ts=now, eval_cutoff_ts=now - 1800, eval_end_ts=now,
            is_historical_rating=False, hr_key="30m", is_custom_account_view=False,
        )
        self.assertTrue(any_sm_3)

        # 3) 2 flagship Neon subagents -> NEVER buffered
        subs_flag = [_make_sub(i, "gemini-4-neon-xhigh", True) for i in range(2)]
        _, any_sm_flag, _, _ = AntigravityMonitorEngine._select_cache_health_signals(
            [main_c] + subs_flag, now_ts=now, eval_cutoff_ts=now - 1800, eval_end_ts=now,
            is_historical_rating=False, hr_key="30m", is_custom_account_view=False,
        )
        self.assertTrue(any_sm_flag)


class TestV730ModelSpeedAndT2(unittest.TestCase):
    def test_model_speed_thresholds_and_strict_t2_ordering(self) -> None:
        self.assertEqual(classify_speed_model_family("Gemini 4 Carbon (High)"), "carbon")
        self.assertEqual(classify_speed_model_family("Gemini 4 Neon (XHigh)"), "neon")
        self.assertEqual(classify_speed_model_family("Gemini 4 Hydrogen (XHigh)"), "light")
        self.assertEqual(classify_speed_model_family("Claude Opus 5.5 (High)"), "3p")
        self.assertEqual(classify_speed_model_family(""), "default")

        th_c = resolve_model_speed_thresholds("Gemini 4 Carbon (High)", 40.0, 45.0)
        self.assertEqual((th_c.slow_tps, th_c.slow_ttft, th_c.full_speed_tps), (35.0, 60.0, 70.0))
        th_h = resolve_model_speed_thresholds("Gemini 4 Hydrogen (XHigh)", 40.0, 45.0)
        self.assertEqual((th_h.slow_tps, th_h.slow_ttft, th_h.full_speed_tps), (70.0, 35.0, 126.0))
        th_3p = resolve_model_speed_thresholds("Claude Opus 5.5 (High)", 40.0, 45.0)
        self.assertEqual((th_3p.slow_tps, th_3p.slow_ttft, th_3p.full_speed_tps), (25.0, 45.0, 55.0))

        # Extreme cfg_slow_ttft scaling must strictly preserve ok_med < deg_med < spike < extreme
        for extreme_ttft in (10.0, 15.0, 45.0, 180.0, 300.0, 600.0):
            for fam_m in ("Carbon High", "Neon XHigh", "Hydrogen XH", "Opus 5.5"):
                th = resolve_model_speed_thresholds(fam_m, 40.0, extreme_ttft)
                self.assertLess(th.t2_healthy_median, th.t2_deg_median)
                self.assertLess(th.t2_deg_median, th.t2_latest_spike)
                self.assertLess(th.t2_latest_spike, th.t2_extreme_spike)

    def test_classify_speed_ex_no_220k_cliff_and_short_burst_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = _make_engine(Path(td), read_only=True)
            # 1) >=220K genuine slow (260K, ttft=90s, out=300) MUST be slow (old <220K cliff eliminated!)
            st1, ex1 = eng._classify_speed_ex(
                output_tokens=300, tps=50.0, ttft_sec=90.0, context_tokens=260000, streaming_sec=6.0, model="Neon XHigh",
            )
            self.assertEqual((st1, ex1), ("slow", False))

            # 2) Short burst fake ultra-high TPS (out=262, strm=0.18s, tps=1410, ttft=90s) MUST NOT trigger full-speed exemption!
            st2, _ = eng._classify_speed_ex(
                output_tokens=262, tps=1410.0, ttft_sec=90.0, context_tokens=150000, streaming_sec=0.18, model="Neon XHigh",
            )
            self.assertEqual(st2, "slow")

            # 3) Carbon normal 48s TTFT -> ok (threshold 60s)
            st3, _ = eng._classify_speed_ex(
                output_tokens=800, tps=60.0, ttft_sec=48.0, context_tokens=150000, streaming_sec=13.0, model="Gemini 4 Carbon (High)",
            )
            self.assertEqual(st3, "ok")

            # 4) Hydrogen throttled to 60 t/s (<70) -> slow
            st4, _ = eng._classify_speed_ex(
                output_tokens=600, tps=60.0, ttft_sec=12.0, context_tokens=100000, streaming_sec=10.0, model="Gemini 4 Hydrogen (XHigh)",
            )
            self.assertEqual(st4, "slow")

    def test_sustained_slow_and_t2_same_model_recovery_no_switch_whitewash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eng = _make_engine(root, read_only=True)
            now = 1795410000.0
            t_err = now - 700.0

            # Build LogEvent annotation model backfill test
            cid_c = "carb0001-1111-2222-3333-444444444444"
            st_c = [StepRecord(1, t_err - 5.0, "gemini-4-carbon-high", 100000, 0, 500, 100000, 0.0, "expected_cold", "first_turn")]
            conv_c = ConversationSummary(
                conversation_id=cid_c, short_id="carb00", model="Carbon High", model_raw="gemini-4-carbon-high",
                context_tokens=100000, max_context_tokens=512000, latest_hit_rate=0.0, latest_cached_in=0,
                latest_uncached_in=100000, latest_timestamp=t_err - 5.0, recent_steps=st_c, consec_miss=0,
                consec_miss_start_step=1, single_miss=False, wasted_cold_tokens=0, freshness_remaining_sec=None,
                _scoped_steps_all=st_c,
            )
            eng._transcript_error_ts[cid_c] = [(t_err, "503 no capacity", 0, 0, 100000)]
            raw_evs = [LogEvent(timestamp=t_err, category="503", summary="[01:00:00] 503", event_key="k1")]
            ann = eng._annotate_log_events_with_conversations(raw_evs, [conv_c])
            self.assertEqual(ann[0].model, "Carbon High")

            # T2 recovery: Carbon High was affected; user switches to Neon XHigh and runs 10 healthy steps -> WARN only, NEVER T0!
            ms_neon = ModelPerfStats(
                model="Neon XHigh", step_count=10, hit_count=10, abnormal_miss_count=0,
                ttft_avg_sec=12.0, ttft_max_sec=18.0, latest_ttft_sec=11.0, tps_avg=140.0, latest_timestamp=now - 10,
                steps_after_last_t2=10, ttft_after_last_t2_avg=12.0, ttft_after_last_t2_latest=11.0,
                ttft_after_last_t2_median=12.0, main_steps_after_last_t2=10,
                main_ttft_after_last_t2_median=12.0, main_ttft_after_last_t2_latest=11.0,
                main_ttft_after_last_t2_recent2_max=13.0, main_adj_ttft_after_last_t2_median=12.0,
                main_adj_ttft_after_last_t2_latest=11.0, main_adj_ttft_after_last_t2_recent2_max=13.0,
                main_unexempt_ttft_steps_after_last_t2=10,
            )
            lvl_w, grd_w, _, _, _ = eng._evaluate_health(
                active_model="Neon XHigh",
                recent_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                today_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                max_consec_miss=0, any_single_miss=False, any_slow=False, today_steps=10,
                primary=conv_c, model_stats=[ms_neon], last_t2_error_ts=t_err, now_ts=now,
                t2_affected_model="Carbon High",
            )
            self.assertEqual((lvl_w, grd_w), ("orange", "WARN"))

            # Switching to Hydrogen (light immune) must NOT soften Carbon T2 -> stays red T2!
            ms_hydro = replace(ms_neon, model="Hydrogen XH")
            lvl_r, grd_r, _, _, _ = eng._evaluate_health(
                active_model="Hydrogen XH",
                recent_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                today_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                max_consec_miss=0, any_single_miss=False, any_slow=False, today_steps=10,
                primary=conv_c, model_stats=[ms_hydro], last_t2_error_ts=t_err, now_ts=now,
                t2_affected_model="Carbon High",
            )
            self.assertEqual((lvl_r, grd_r), ("red", "T2"))

            # Once Carbon High itself recovers (>=6 steps, >=600s, median 48s < Carbon 60s) -> green T0!
            ms_carb_ok = replace(
                ms_neon, model="Carbon High",
                main_ttft_after_last_t2_median=48.0, main_adj_ttft_after_last_t2_median=48.0,
                main_ttft_after_last_t2_latest=46.0, main_adj_ttft_after_last_t2_latest=46.0,
                main_ttft_after_last_t2_recent2_max=50.0, main_adj_ttft_after_last_t2_recent2_max=50.0,
            )
            lvl_ok, grd_ok, _, _, _ = eng._evaluate_health(
                active_model="Carbon High",
                recent_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                today_log={"503": 2, "timeout": 0, "403": 0, "retry": 0},
                max_consec_miss=0, any_single_miss=False, any_slow=False, today_steps=10,
                primary=conv_c, model_stats=[ms_carb_ok], last_t2_error_ts=t_err, now_ts=now,
                t2_affected_model="Carbon High",
            )
            self.assertEqual((lvl_ok, grd_ok), ("green", "T0"))

            # sustained_slow single jitter vs consecutive 2 slow steps
            c_spd = replace(conv_c, model="Neon XHigh", model_raw="gemini-4-neon-xhigh")
            g_ok = _make_gen(1, 100000, 95000, 600, ttft_sec=12.0, streaming_sec=4.0, tps=150.0)
            g_mild_slow = _make_gen(2, 102000, 95000, 300, ttft_sec=52.0, streaming_sec=2.0, tps=60.0)
            eng._evaluate_conv_speed_window(c_spd, [(None, g_ok), (None, g_mild_slow)])
            self.assertEqual(c_spd.speed_status, "slow")
            self.assertFalse(c_spd.sustained_slow)  # Single mild jitter suppressed
            eng._evaluate_conv_speed_window(c_spd, [(None, g_mild_slow), (None, g_mild_slow)])
            self.assertTrue(c_spd.sustained_slow)   # Consecutive 2 slow confirmed


class TestV730FreshnessAndQuotaVelocity(unittest.TestCase):
    def test_freshness_windows_rescue_sort_and_formatters(self) -> None:
        self.assertEqual(resolve_model_freshness_window_sec("Gemini 4 Carbon (High)", 210), 180)
        self.assertEqual(resolve_model_freshness_window_sec("Gemini 4 Neon (XHigh)", 210), 240)
        self.assertEqual(resolve_model_freshness_window_sec("Claude Opus 5.5 (High)", 210), 240)
        self.assertEqual(resolve_model_freshness_window_sec("Gemini 4 Hydrogen (XHigh)", 210), 210)
        self.assertEqual(resolve_model_freshness_window_sec("", 210), 210)

        # Legacy format_countdown preserved!
        self.assertEqual(format_countdown(0), "已过期")
        self.assertEqual(format_countdown(None), "闲置")

        # New format_freshness_status
        self.assertEqual(format_freshness_status(120), ("剩 2:00", "green"))
        self.assertEqual(format_freshness_status(45, compact=True), ("快冷 0:45", "orange"))
        self.assertEqual(format_freshness_status(-15, compact=True), ("抢救 1:15", "red"))
        self.assertEqual(format_freshness_status(-15, compact=False), ("临界抢救 剩 1:15", "red"))
        self.assertEqual(format_freshness_status(-95), ("已冷却", "orange"))
        self.assertEqual(format_freshness_status(None, "无热缓存 (连掉 3 步)", compact=True), ("已失效", "red"))

        # Tier display strictly parses userTier.id
        self.assertEqual(resolve_tier_display("g1-ultra-tier"), ("Ultra", "[U]"))
        self.assertEqual(resolve_tier_display("g1-pro-tier"), ("Pro", "[P]"))
        self.assertEqual(resolve_tier_display("g1-free-tier"), ("Free", "[F]"))
        self.assertEqual(resolve_tier_display(""), ("", ""))

        # 490K conv at -15s (rescue) ranks ahead of 100K conv at +180s (safe)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = 1795420000.0
            for cid, tok, age in (("big49000-1111", 490000, 255.0), ("sml10000-2222", 100000, 60.0)):
                d = root / "brain" / cid / ".system_generated" / "logs"
                d.mkdir(parents=True)
                rows = [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(now - age - 20), "content": "hi"},
                    {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(now - age), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": tok - 2000, "output_tokens": 300},
                ]
                tp = d / "transcript.jsonl"
                tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
                os.utime(tp, (now - age, now - age))
            eng = _make_engine(root, read_only=True)
            eng._account_cache = AccountQuotaInfo(short_label="de***2", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.min_freshness_conv, "big490")
            self.assertEqual(snap.min_freshness_zone, "rescue")
            self.assertEqual(snap.min_freshness_sec, -15)  # 240 - 255
            self.assertEqual(snap.expiring_soon_count, 1)

    def test_quota_velocity_reset_jump_zero_burn_and_five_hour_reset_sync(self) -> None:
        now = 1795430000.0
        rst_5h = _iso_utc(now + 3600.0)
        rst_wk = _iso_utc(now + 5 * 86400.0)

        # 1) Normal linear burn: 60% -> 40% in 1800s => 40%/h, eta = 3600s; with reset in 7200s -> will_exhaust=True
        pool = QuotaPoolInfo(
            name="gemini", five_hour_pct=40.0, weekly_pct=80.0,
            five_hour_fraction=0.400000, weekly_fraction=0.800000,
            five_hour_reset_at=_iso_utc(now + 7200.0), weekly_reset_at=rst_wk,
        )
        samples = [
            (now - 1800.0, 60.0, 82.0, pool.five_hour_reset_at or "", rst_wk),
            (now - 900.0, 50.0, 81.0, pool.five_hour_reset_at or "", rst_wk),
            (now, 40.0, 80.0, pool.five_hour_reset_at or "", rst_wk),
        ]
        compute_quota_pool_velocity(pool, samples, now)
        self.assertAlmostEqual(pool.burn_pct_per_hour or 0.0, 40.0, delta=0.2)
        self.assertEqual(pool.eta_exhaust_sec, 3600)
        self.assertTrue(pool.will_exhaust_before_reset)
        self.assertEqual(pool.five_hour_reset_in_sec, 7200)
        self.assertIsNotNone(pool.weekly_pace_ratio)
        self.assertAlmostEqual(pool.weekly_pace_ratio or 0.0, 0.7, delta=0.05)

        # 2) Reset jump guard (old cycle 10% -> new cycle 100% -> 95% in 1800s => burn 10%/h, NOT negative!)
        pool_jump = QuotaPoolInfo(
            name="gemini", five_hour_pct=95.0, five_hour_fraction=0.95,
            five_hour_reset_at=rst_5h,
        )
        jump_samples = [
            (now - 3600.0, 10.0, None, _iso_utc(now - 2000.0), ""),
            (now - 1800.0, 100.0, None, rst_5h, ""),
            (now, 95.0, None, rst_5h, ""),
        ]
        compute_quota_pool_velocity(pool_jump, jump_samples, now)
        self.assertAlmostEqual(pool_jump.burn_pct_per_hour or 0.0, 10.0, delta=0.2)
        self.assertFalse(pool_jump.will_exhaust_before_reset)

        # 3) Zero burn must not divide by zero
        pool_zero = QuotaPoolInfo(name="gemini", five_hour_pct=80.0, five_hour_fraction=0.80, five_hour_reset_at=rst_5h)
        zero_samples = [
            (now - 1200.0, 80.0, None, rst_5h, ""),
            (now, 80.0, None, rst_5h, ""),
        ]
        compute_quota_pool_velocity(pool_zero, zero_samples, now)
        self.assertEqual(pool_zero.burn_pct_per_hour, 0.0)
        self.assertIsNone(pool_zero.eta_exhaust_sec)
        self.assertFalse(pool_zero.will_exhaust_before_reset)

        # 4) five_hour_reset_at syncs with min(remainingFraction) in GetUserStatus + Cockpit preheat ms/s
        payload_status = {
            "userStatus": {
                "email": "tester@example.com",
                "userTier": {"id": "g1-ultra-tier"},
                "planStatus": {"planInfo": {"planName": "Pro", "teamsTier": "TEAMS_TIER_PRO"}},
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Gemini 4 Hydrogen (XHigh)",
                            "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M528"},
                            "quotaInfo": {"remainingFraction": 0.99, "resetTime": _iso_utc(now + 14400.0)},
                        },
                        {
                            "label": "Gemini 4 Neon (XHigh)",
                            "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M535"},
                            "quotaInfo": {"remainingFraction": 0.41234567, "resetTime": _iso_utc(now + 1800.0)},
                        },
                    ]
                },
            }
        }

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if "GetUserStatus" in self.path:
                    body = json.dumps(payload_status).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        th_srv = threading.Thread(target=srv.serve_forever, daemon=True)
        th_srv.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ck_dir = root / "cockpit"
                ck_dir.mkdir()
                (ck_dir / "accounts.json").write_text(
                    json.dumps({"accounts": [{"id": "acc_1", "email": "tester@example.com"}]}), encoding="utf-8",
                )
                qdb = ck_dir / "platform-quota-history-v1.sqlite"
                qc = sqlite3.connect(qdb)
                qc.execute("CREATE TABLE quota_observations (account_id TEXT, metric TEXT, remaining REAL, observed_at REAL, reset_at TEXT)")
                # Mix ms timestamp and s timestamp in Cockpit preheat
                qc.execute("INSERT INTO quota_observations VALUES ('acc_1', 'gemini-5h', 61.23, ?, ?)", ((now - 1800.0) * 1000.0, _iso_utc(now + 1800.0)))
                qc.execute("INSERT INTO quota_observations VALUES ('acc_1', 'gemini-5h', 51.23, ?, ?)", (now - 900.0, _iso_utc(now + 1800.0)))
                qc.execute("INSERT INTO quota_observations VALUES ('acc_1', 'gemini-weekly', 88.0, ?, ?)", (now - 60.0, rst_wk))
                qc.commit()
                qc.close()

                daemon_dir = root / "daemon"
                daemon_dir.mkdir()
                (daemon_dir / "hub_test.json").write_text(
                    json.dumps({"pid": os.getpid(), "httpPort": port, "csrfToken": "tok123"}), encoding="utf-8",
                )
                eng = AntigravityMonitorEngine(
                    paths={
                        "brain_dir": root / "brain",
                        "conversations_dir": root / "conversations",
                        "daemon_dir": daemon_dir,
                        "ls_log_path": root / "ls.log",
                        "cockpit_quota_db": qdb,
                        "ledger_path": root / "ledger.json",
                    },
                    read_only=False,
                )
                snap = eng.poll(now_ts=now, force_account=True)
                self.assertTrue(snap.account.online)
                # Tier strictly from userTier.id (Ultra), NOT plan_status (Pro)!
                self.assertEqual(snap.account.tier_label, "Ultra")
                self.assertEqual(snap.account.tier_short, "[U]")
                self.assertEqual(snap.account_tier_norm, "ultra")
                self.assertEqual(eng._ledger.get_account_tier(snap.account.short_label), "ultra")
                # five_hour_reset_at synced with min(remainingFraction)=0.41234567!
                gem_pool = snap.account.pools["gemini"]
                self.assertAlmostEqual(gem_pool.five_hour_fraction or 0.0, 0.41234567, places=6)
                self.assertEqual(gem_pool.five_hour_reset_at, _iso_utc(now + 1800.0))
                # Velocity computed via Cockpit preheat ring
                self.assertIsNotNone(gem_pool.burn_pct_per_hour)
                self.assertGreater(gem_pool.burn_pct_per_hour or 0.0, 10.0)
        finally:
            srv.shutdown()
            srv.server_close()


class TestV730AdaptiveStepsAndJsonIntegration(unittest.TestCase):
    def test_t1_single_causal_window_and_json_v10_extended_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = 1795440000.0
            # Conv A (culprit): 3 consecutive abnormal_zero ending at now - 30
            cid_a = "t1culp01-1111-2222-3333-444444444444"
            da = root / "brain" / cid_a / ".system_generated" / "logs"
            da.mkdir(parents=True)
            rows_a = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(now - 90), "content": "a"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(now - 80), "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(now - 60), "model": "gemini-4-neon-xhigh", "input_tokens": 102000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": _iso_utc(now - 45), "model": "gemini-4-neon-xhigh", "input_tokens": 104000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": _iso_utc(now - 30), "model": "gemini-4-neon-xhigh", "input_tokens": 106000, "cache_read_tokens": 0, "output_tokens": 200},
            ]
            tpa = da / "transcript.jsonl"
            tpa.write_text("\n".join(json.dumps(r) for r in rows_a) + "\n", encoding="utf-8")
            os.utime(tpa, (now - 30, now - 30))

            # Conv B (other main conv): healthy hits INCLUDING at now - 10 (>= t1_ts = now - 30) -> proves isolated t1_single!
            cid_b = "health02-1111-2222-3333-444444444444"
            db_dir = root / "brain" / cid_b / ".system_generated" / "logs"
            db_dir.mkdir(parents=True)
            rows_b = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(now - 100), "content": "b"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(now - 50), "model": "claude-opus-5-5", "input_tokens": 80000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(now - 20), "model": "claude-opus-5-5", "input_tokens": 2000, "cache_read_tokens": 80000, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": _iso_utc(now - 10), "model": "claude-opus-5-5", "input_tokens": 2000, "cache_read_tokens": 82000, "output_tokens": 200},
            ]
            tpb = db_dir / "transcript.jsonl"
            tpb.write_text("\n".join(json.dumps(r) for r in rows_b) + "\n", encoding="utf-8")
            os.utime(tpb, (now - 10, now - 10))

            # Add a retry line in ls.log to verify recent_events_by_cat["retry"]
            dt_l = datetime.datetime.fromtimestamp(now - 15)
            ls_p = root / "language_server.log"
            ls_p.write_text(
                f"W{dt_l.strftime('%m%d %H:%M:%S')}.000000 100 run.go:88] attempt 1 failed: transient network reset\n",
                encoding="utf-8",
            )

            eng = _make_engine(root, read_only=False)
            eng._account_cache = AccountQuotaInfo(
                short_label="de***9",
                tier="g1-ultra-tier",
                online=True,
                observed_at=now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=80.0), "3p": QuotaPoolInfo(name="3p", five_hour_pct=90.0)},
            )
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T1")

            # Persisted hourly bucket MUST be d=False, dk="t1_single" with multi-dim metadata (m/p3/uc)!
            hk = int((now - 30) // 3600)
            hb = eng._ledger.get_account_hourly_buckets("de***9")
            self.assertIn(hk, hb)
            self.assertFalse(hb[hk]["degraded"])
            self.assertEqual(hb[hk]["dk"], "t1_single")
            self.assertEqual(hb[hk]["m"], 7)
            self.assertEqual(hb[hk]["p3"], 3)
            self.assertGreater(hb[hk]["uc"], 400)

            # JSON snapshot v1.0 contract check (all Plan §5.1 + diag_ui §3.4 fields)
            jobj = json.loads(format_json_snapshot(snap))
            self.assertEqual(jobj["schema_version"], "1.0")
            self.assertEqual(jobj["steps"]["account_tier"], "ultra")
            self.assertEqual(jobj["steps"]["gemini_24h_steps"], 4)
            self.assertEqual(jobj["steps"]["third_party_24h_steps"], 3)
            for k in (
                "pool_family", "pool_24h_steps", "effective_weighted_steps",
                "peer_prior_peak", "peer_weight_pct", "quota_headroom_steps",
                "soft_over_limit", "low_quota_tightened", "detail_lines",
            ):
                self.assertIn(k, jobj["steps"])
            for k in ("tier_label", "tier_short", "five_hour_reset_in_sec", "weekly_reset_in_sec", "burn_pct_per_hour", "eta_exhaust_sec", "will_exhaust_before_reset", "weekly_pace_ratio"):
                self.assertIn(k, jobj["account"])
            for k in ("min_freshness_window_sec", "min_freshness_zone", "expiring_soon_count", "large_freshness_list"):
                self.assertIn(k, jobj["cache"])
            for k in ("sustained_slow", "effective_slow_tps_th", "effective_slow_ttft_th", "media_ttft_exempted", "large_ctx_ttft_allowance_sec"):
                self.assertIn(k, jobj["perf"])
            for k in ("last_t2_error_ts", "last_t2_error_age_sec", "recent_events"):
                self.assertIn(k, jobj["errors_30m"])
            for k in ("total_matched_conversations", "total_matched_main", "total_matched_sub"):
                self.assertIn(k, jobj["counts"])
            self.assertEqual(len(snap.recent_events_by_cat["retry"]), 1)

    def test_t1_causal_window_requires_hit_after_t1_and_history_scan_multidim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Yesterday settled hour: Conv A has T1 at t_t1, Conv B only had hits BEFORE t_t1 (none >= t_t1) -> MUST be t1_multi (d=True)!
            now = datetime.datetime(2026, 11, 24, 10, 0, 0).timestamp()
            t_t1 = datetime.datetime(2026, 11, 22, 14, 30, 0).timestamp()
            hk_y = int(t_t1 // 3600)

            da = root / "brain" / "histt101-aaaa" / ".system_generated" / "logs"
            da.mkdir(parents=True)
            rows_a = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(t_t1 - 90), "content": "a"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(t_t1 - 60), "model": "gemini-4-neon-xhigh", "input_tokens": 100000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(t_t1 - 40), "model": "gemini-4-neon-xhigh", "input_tokens": 102000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": _iso_utc(t_t1 - 20), "model": "gemini-4-neon-xhigh", "input_tokens": 104000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 4, "created_at": _iso_utc(t_t1), "model": "gemini-4-neon-xhigh", "input_tokens": 106000, "cache_read_tokens": 0, "output_tokens": 200},
            ]
            tpa = da / "transcript.jsonl"
            tpa.write_text("\n".join(json.dumps(r) for r in rows_a) + "\n", encoding="utf-8")
            os.utime(tpa, (t_t1, t_t1))

            db = root / "brain" / "histok02-bbbb" / ".system_generated" / "logs"
            db.mkdir(parents=True)
            rows_b = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(t_t1 - 110), "content": "b"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(t_t1 - 80), "model": "gemini-4-hydrogen-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(t_t1 - 50), "model": "gemini-4-hydrogen-xhigh", "input_tokens": 1000, "cache_read_tokens": 50000, "output_tokens": 200},
            ]
            tpb = db / "transcript.jsonl"
            tpb.write_text("\n".join(json.dumps(r) for r in rows_b) + "\n", encoding="utf-8")
            os.utime(tpb, (t_t1 - 50, t_t1 - 50))

            eng = _make_engine(root, read_only=True)
            today_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            today_start = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            eng._get_cached_history_daily_profiles(now, today_start, today_str, [], fallback_label="de***8")
            hmap = eng._hist_hourly_buckets_cache["de***8"]
            self.assertIn(hk_y, hmap)
            # No hit >= t_t1 -> MUST NOT classify as t1_single!
            self.assertTrue(hmap[hk_y]["degraded"])
            self.assertEqual(hmap[hk_y]["dk"], "t1_multi")
            self.assertEqual(hmap[hk_y]["lt"], 2)
            self.assertEqual(hmap[hk_y]["m"], 6)

            # Now add one hit AFTER t_t1 on Conv B -> history rescan MUST classify as t1_single (d=False)!
            rows_b.append({"type": "PLANNER_RESPONSE", "step_index": 3, "created_at": _iso_utc(t_t1 + 30), "model": "gemini-4-hydrogen-xhigh", "input_tokens": 1000, "cache_read_tokens": 51000, "output_tokens": 200})
            tpb.write_text("\n".join(json.dumps(r) for r in rows_b) + "\n", encoding="utf-8")
            os.utime(tpb, (t_t1 + 30, t_t1 + 30))
            eng._hist_daily_profiles_cache_key = ()
            eng._get_cached_history_daily_profiles(now, today_start, today_str, [], fallback_label="de***8")
            hmap2 = eng._hist_hourly_buckets_cache["de***8"]
            self.assertFalse(hmap2[hk_y]["degraded"])
            self.assertEqual(hmap2[hk_y]["dk"], "t1_single")

    def test_total_matched_over_24_static_cache_multidim_and_quota_ring_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = 1795460000.0
            # Create 28 main conversations in the last 20m (exceeds 24 display cap) + 1 quiet static 3P conv 2h ago
            for i in range(28):
                cid = f"many{i:04d}-1111-2222-3333-444444444444"
                d = root / "brain" / cid / ".system_generated" / "logs"
                d.mkdir(parents=True)
                ts_i = now - 60.0 - i * 10.0
                rows = [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(ts_i - 5), "content": f"m{i}"},
                    {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(ts_i), "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200},
                ]
                tp = d / "transcript.jsonl"
                tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
                os.utime(tp, (ts_i, ts_i))

            # Quiet static 3P conv 2h ago to test _static_conv_agg_cache passthrough of p3 / uc_meta across two polls
            cid_q = "stat0001-1111-2222-3333-444444444444"
            dq = root / "brain" / cid_q / ".system_generated" / "logs"
            dq.mkdir(parents=True)
            ts_q = now - 7200.0
            rows_q = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(ts_q - 5), "content": "q"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(ts_q), "model": "claude-opus-5-5", "input_tokens": 60000, "cache_read_tokens": 0, "output_tokens": 200},
            ]
            tpq = dq / "transcript.jsonl"
            tpq.write_text("\n".join(json.dumps(r) for r in rows_q) + "\n", encoding="utf-8")
            os.utime(tpq, (ts_q, ts_q))

            eng = _make_engine(root, read_only=True)
            eng._account_cache = AccountQuotaInfo(
                short_label="de***5",
                online=True,
                observed_at=now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=85.0)},
            )
            eng._last_account_poll = now
            snap1 = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(len(snap1.active_conversations), 24)
            self.assertEqual(snap1.total_matched_conversations, 28)
            self.assertEqual(snap1.total_matched_main, 28)
            self.assertEqual(snap1.steps_3p_24h, 1)
            self.assertEqual(snap1.steps_gemini_24h, 28)

            # Second poll hits _static_conv_agg_cache for cid_q -> 3P count MUST stay exact!
            snap2 = eng.poll(now_ts=now + 2.0, force_account=False)
            self.assertEqual(snap2.steps_3p_24h, 1)
            self.assertEqual(snap2.total_matched_conversations, 28)

            # Verify bounded 32x180 quota sample ring LRU eviction
            for idx in range(40):
                acc = AccountQuotaInfo(
                    short_label=f"a{idx:02d}***9",
                    online=True,
                    pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=80.0, five_hour_fraction=0.8)},
                )
                eng._record_and_compute_quota_velocity(acc, now + idx * 20.0, record_sample=True)
            self.assertLessEqual(len(eng._quota_sample_rings), 32)
            # Push 210 samples on one key -> deque maxlen caps at 180
            acc_hot = AccountQuotaInfo(
                short_label="ho***1",
                online=True,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=80.0, five_hour_fraction=0.8)},
            )
            for k in range(210):
                eng._record_and_compute_quota_velocity(acc_hot, now + k * 20.0, record_sample=True)
            self.assertEqual(len(eng._quota_sample_rings[("ho***1", "gemini")]), 180)

    def test_proxy_outage_recovery_ignores_subagent_and_light_steps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = 1795470000.0
            t_out = now - 400.0  # >300s ago and <=15m
            dt_o = datetime.datetime.fromtimestamp(t_out)
            ts_pref = dt_o.strftime("%m%d %H:%M:%S")
            # Cross-bucket proxy EOF burst (stream + userinfo + playlog)
            (root / "language_server.log").write_text(
                f"W{ts_pref}.100000 100 run.go:10] streamGenerateContent googleapis.com unexpected EOF\n"
                f"W{ts_pref}.200000 101 cache.go:20] fetchUserInfo googleapis.com unexpected EOF\n"
                f"W{ts_pref}.300000 102 sync.go:30] play.googleapis.com unexpected EOF\n",
                encoding="utf-8",
            )
            # Main flagship conv has only 2 steps after outage; subagent has 10 steps after outage -> must NOT clear outage!
            dm = root / "brain" / "main0001-aaaa" / ".system_generated" / "logs"
            dm.mkdir(parents=True)
            m_rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(t_out - 20), "content": "m"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(t_out + 60), "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(t_out + 120), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000, "output_tokens": 200},
            ]
            (dm / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in m_rows) + "\n", encoding="utf-8")
            os.utime(dm / "transcript.jsonl", (t_out + 120, t_out + 120))

            ds = root / "brain" / "suba0002-bbbb" / ".system_generated" / "logs"
            ds.mkdir(parents=True)
            s_rows = [{"type": "SYSTEM", "step_index": 0, "created_at": _iso_utc(t_out + 10), "content": "<swarm_worker> sub"}]
            for j in range(1, 11):
                s_rows.append({"type": "PLANNER_RESPONSE", "step_index": j, "created_at": _iso_utc(t_out + 20 + j * 10), "model": "gemini-4-hydrogen-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000, "output_tokens": 200})
            (ds / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in s_rows) + "\n", encoding="utf-8")
            os.utime(ds / "transcript.jsonl", (t_out + 120, t_out + 120))

            eng = _make_engine(root, read_only=True)
            eng._account_cache = AccountQuotaInfo(
                short_label="de***4",
                online=True,
                observed_at=now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=90.0)},
            )
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertGreater(snap.proxy_outage_recent, 0)
            self.assertIn("断流", snap.proxy_outage_note)

            # Now add 4 more flagship main steps (total 6 post-outage flagship main steps) -> outage clears!
            for k in range(3, 7):
                m_rows.append({"type": "PLANNER_RESPONSE", "step_index": k, "created_at": _iso_utc(t_out + 120 + k * 10), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 52000, "output_tokens": 200})
            (dm / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in m_rows) + "\n", encoding="utf-8")
            os.utime(dm / "transcript.jsonl", (t_out + 200, t_out + 200))
            snap_ok = eng.poll(now_ts=now + 2.0, force_account=False)
            self.assertEqual(snap_ok.proxy_outage_recent, 0)
            self.assertIn("已平复", snap_ok.proxy_outage_note)

    def test_maybe_auto_clean_slots_accumulates_skipped_recycle_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eng = _make_engine(root, read_only=False)
            now = 1795450000.0
            eng._account_cache = AccountQuotaInfo(
                short_label="de***3",
                online=True,
                observed_at=now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=85.0)},
            )
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            snap = replace(
                snap,
                slot_inventory=SlotInventorySnapshot(
                    observed_at=now, total_db=460, cap=500, main_count=200,
                    cleanable_sub_count=5, cleanable_30m_count=10, protected_sub_count=245,
                    unknown_count=0, pinned_floor_count=0, total_bytes=1000, cleanable_bytes=500,
                    protect_age_min=120, status="main_at_risk", main_at_risk=True, risk_message="risk",
                    summary_db_ok=True,
                ),
            )
            calls: List[int] = []

            def _fake_clean(**kwargs: Any) -> SlotCleanupResult:
                calls.append(int(kwargs["protect_age_min"]))
                return SlotCleanupResult(
                    started_at=now, finished_at=now, trigger="auto_high_water", mode="recycle",
                    protect_age_min=int(kwargs["protect_age_min"]), dry_run=False,
                    removed_count=1 if len(calls) == 1 else 0,
                    freed_bytes=100 if len(calls) == 1 else 0,
                    skipped_locked_count=1,
                    skipped_recycle_unavailable_count=2 if len(calls) == 1 else 3,
                    last_skip_reason="recycle_bin_disabled_on_volume",
                    remaining_db_count=455,
                )

            eng.clean_old_subagents = _fake_clean  # type: ignore[method-assign]
            res = eng.maybe_auto_clean_slots(snap, now_ts=now)
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(len(calls), 2)
            self.assertEqual(res.skipped_recycle_unavailable_count, 5)
            self.assertEqual(res.skipped_locked_count, 2)

    def test_final_audit_reset_jitter_and_merge_degraded_never_whitewashed(self) -> None:
        now = 1795470000.0
        # 1) ±1s resetTime jitter MUST NOT break quota velocity calculation
        rst_a = _iso_utc(now + 10800.0)
        rst_b = _iso_utc(now + 10801.0)
        pool = QuotaPoolInfo(name="gemini", five_hour_pct=75.0, five_hour_fraction=0.75, five_hour_reset_at=rst_b)
        samples = [
            (now - 1800.0, 90.0, None, rst_a, ""),
            (now - 1200.0, 85.0, None, rst_a, ""),
            (now - 600.0, 80.0, None, rst_a, ""),
            (now, 75.0, None, rst_b, ""),
        ]
        compute_quota_pool_velocity(pool, samples, now)
        self.assertAlmostEqual(pool.burn_pct_per_hour or 0.0, 30.0, delta=0.5)
        self.assertTrue(pool.will_exhaust_before_reset)

        # 2) _merge_account_hourly_buckets MUST strip t1_single when degraded=True is present
        merged = AntigravityMonitorEngine._merge_account_hourly_buckets(
            {498700: {"steps": 1800, "degraded": True, "degraded_24h_at": 700, "dk": ""}},
            {498700: {"steps": 1800, "degraded": False, "dk": "t1_single"}},
        )
        self.assertTrue(merged[498700]["degraded"])
        self.assertNotEqual(merged[498700]["dk"], "t1_single")

        # 3) Non-inference background writeTrajectoryAcls 429 RESOURCE_EXHAUSTED MUST NOT false-alarm 503
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ls_log = root / "language_server.log"
            ts_str = time.strftime("%m%d %H:%M:%S.123456", time.localtime(now))
            ls_log.write_text(
                "\n".join([
                    f"ERROR: logging before google.Init: I{ts_str} 16010 http_helpers.go:876] "
                    "Failed to make code assist backend request (https://daily-cloudcode-pa.googleapis.com/v1internal:writeTrajectoryAcls): {",
                    '  "error": {',
                    '    "code": 429,',
                    '    "message": "Resource has been exhausted (e.g. check quota).",',
                    '    "status": "RESOURCE_EXHAUSTED"',
                    "  }",
                    "}",
                ]) + "\n",
                encoding="utf-8",
            )
            eng = AntigravityMonitorEngine(
                paths={
                    "brain_dir": root / "brain",
                    "conversations_dir": root / "conversations",
                    "daemon_dir": root / "daemon",
                    "ls_log_path": ls_log,
                    "cockpit_quota_db": root / "ck.sqlite",
                },
                read_only=True,
            )
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.recent_503, 0)
            self.assertEqual(snap.today_503, 0)

    def test_proxy_tls_handshake_timeout_burst_shields_stream_idle_timeout_from_false_t2(self) -> None:
        from core.log_monitor import format_json_snapshot

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 0, 23, 0).timestamp()

            def _glog(dt_sec: float) -> str:
                return datetime.datetime.fromtimestamp(now + dt_sec).strftime("%m%d %H:%M:%S.123456")

            # Seed staggered cross-endpoint TLS handshake timeout wave (matching real 23:59..00:22 log pattern)
            # plus local 127.0.0.1 TLS handshake error & errorreport.go echo traps
            log_lines = [
                f"ERROR: logging before google.Init: W{_glog(-1400)} 11001 cache.go:169] Cache(peopleInfo): Singleflight refresh failed: Get \"https://www.googleapis.com/oauth2/v2/userinfo\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: E{_glog(-1400)} 11001 errorreport.go:282] Get \"https://www.googleapis.com/oauth2/v2/userinfo\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: W{_glog(-1358)} 11002 cache.go:169] Cache(peopleInfo): Singleflight refresh failed: Get \"https://www.googleapis.com/oauth2/v2/userinfo\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: W{_glog(-1316)} 11003 user_management.go:139] Failed to get profile picture: Get \"https://lh3.googleusercontent.com/a/demo\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: W{_glog(-980)} 11004 cache.go:169] Cache(availableModels): Singleflight refresh failed: Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: W{_glog(-940)} 11005 user_management.go:139] Failed to get profile picture: Get \"https://lh3.googleusercontent.com/a/demo\": net/http: TLS handshake timeout",
                f"2026/11/26 00:11:59 http: TLS handshake error from 127.0.0.1:60150: client sent an HTTP request to an HTTPS server",
                f"ERROR: logging before google.Init: I{_glog(-660)} 62407 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                f"ERROR: logging before google.Init: W{_glog(-630)} 11006 user_management.go:139] Failed to get profile picture: Get \"https://lh3.googleusercontent.com/a/demo\": net/http: TLS handshake timeout",
                f"ERROR: logging before google.Init: I{_glog(-30)} 62407 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
            ]
            ls_log = root / "language_server.log"
            ls_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

            # Seed an active main conversation with recent healthy steps
            dm = root / "brain" / "tlswave1-aaaa" / ".system_generated" / "logs"
            dm.mkdir(parents=True)
            rows = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(now - 900), "content": "hi"},
                {"type": "PLANNER_RESPONSE", "step_index": 1, "created_at": _iso_utc(now - 700), "model": "gemini-4-neon-xhigh", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200},
                {"type": "PLANNER_RESPONSE", "step_index": 2, "created_at": _iso_utc(now - 20), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000, "output_tokens": 200},
            ]
            (dm / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(dm / "transcript.jsonl", (now - 10, now - 10))

            eng = _make_engine(root, read_only=False)
            eng._ledger.observe_online("ly***8", now - 3600)
            eng._account_cache = AccountQuotaInfo(
                short_label="ly***8",
                online=True,
                tier="g1-ultra-tier",
                observed_at=now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=93.0, weekly_pct=56.0)},
            )
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)

            # Both 10m0s stream idle timeouts occurred during the cross-endpoint TLS timeout wave -> WARN, NOT red T2!
            self.assertEqual(snap.grade, "WARN")
            self.assertEqual(snap.recent_timeout, 2)
            self.assertEqual(snap.proxy_outage_timeout_recent, 2)
            self.assertEqual(snap.last_t2_error_ts, 0.0)
            self.assertTrue(all(ev.is_proxy_outage_timeout for ev in snap.recent_events if ev.category == "timeout"))
            self.assertIn("代理抖动流超时", snap.sub_headline)

            # Critical invariant: account_ledger MUST NOT persist a false dk="t2" degradation bucket!
            hb = eng._ledger.get_account_hourly_buckets("ly***8")
            self.assertFalse(any(rec.get("degraded") for rec in hb.values()))

            # JSON contract exposes proxy_outage_timeout cleanly
            payload = json.loads(format_json_snapshot(snap))
            self.assertEqual(payload["errors_30m"]["proxy_outage_timeout"], 2)
            self.assertTrue(
                all(e["is_proxy_outage_timeout"] for e in payload["errors_30m"]["recent_events"] if e["category"] == "timeout")
            )

            # Guard A: Real server 503s during the same proxy TLS wave MUST STILL escalate to red T2!
            with ls_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"ERROR: logging before google.Init: E{_glog(-10)} 77001 http_helpers.go:876] streamGenerateContent status 503 No capacity\n"
                    f"ERROR: logging before google.Init: E{_glog(-5)} 77002 http_helpers.go:876] streamGenerateContent status 503 No capacity\n"
                )
            snap_503 = eng.poll(now_ts=now + 2.0, force_account=False)
            self.assertEqual(snap_503.grade, "T2")
            self.assertEqual(snap_503.recent_503, 2)

            # Guard B: Switched account without its own proxy burst hitting 2 standalone stream timeouts MUST escalate to T2!
            t_sw = now + 30.0
            t_sw_now = t_sw + 120.0
            eng._ledger.observe_online("ne***9", t_sw)
            eng._account_cache = AccountQuotaInfo(
                short_label="ne***9",
                online=True,
                tier="g1-ultra-tier",
                observed_at=t_sw_now,
                pools={"gemini": QuotaPoolInfo(name="gemini", five_hour_pct=90.0)},
            )
            eng._last_account_poll = t_sw_now
            with ls_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"ERROR: logging before google.Init: I{datetime.datetime.fromtimestamp(t_sw + 40).strftime('%m%d %H:%M:%S.000000')} 88001 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s\n"
                    f"ERROR: logging before google.Init: I{datetime.datetime.fromtimestamp(t_sw + 90).strftime('%m%d %H:%M:%S.000000')} 88002 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s\n"
                )
            snap_sw = eng.poll(now_ts=t_sw_now, force_account=False)
            self.assertEqual(snap_sw.grade, "T2")
            self.assertEqual(snap_sw.proxy_outage_timeout_recent, 0)

    def test_standalone_stream_timeouts_with_single_isolated_tls_still_trigger_t2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 1, 0, 0).timestamp()

            def _glog(dt_sec: float) -> str:
                return datetime.datetime.fromtimestamp(now + dt_sec).strftime("%m%d %H:%M:%S.123456")

            # Only 1 isolated single-bucket TLS timeout (NOT a >=3 cross-endpoint burst) + 2 stream timeouts -> MUST stay T2!
            (root / "language_server.log").write_text(
                f"ERROR: logging before google.Init: W{_glog(-300)} 91001 user_management.go:139] Get \"https://lh3.googleusercontent.com/a/demo\": net/http: TLS handshake timeout\n"
                f"ERROR: logging before google.Init: I{_glog(-120)} 91002 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s\n"
                f"ERROR: logging before google.Init: I{_glog(-20)} 91003 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s\n",
                encoding="utf-8",
            )
            eng = _make_engine(root, read_only=False)
            eng._ledger.observe_online("ac***7", now - 3600)
            eng._account_cache = AccountQuotaInfo(short_label="ac***7", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T2")
            self.assertEqual(snap.proxy_outage_timeout_recent, 0)

    def test_dual_review_proxy_timeout_anti_whitewash_reentry_settle_and_localhost_profile(self) -> None:
        from core.log_monitor import LanguageServerLogTailer, format_text_summary

        # 1) Dual-review M1-A: Manual proxy switch 60s AFTER 2 real server timeouts MUST NOT whitewash T2!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 12, 0, 0).timestamp()

            def _g(dt_sec: float) -> str:
                return datetime.datetime.fromtimestamp(now + dt_sec).strftime("%m%d %H:%M:%S.100000")

            (root / "language_server.log").write_text(
                "\n".join([
                    f"ERROR: logging before google.Init: I{_g(-80)} 50001 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                    f"ERROR: logging before google.Init: I{_g(-70)} 50002 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                    # 60s AFTER timeouts, user switches proxy node causing 3 cross-endpoint EOFs
                    f"ERROR: logging before google.Init: E{_g(-10)} 50003 telemetry.go:142] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics\": unexpected EOF",
                    f"ERROR: logging before google.Init: W{_g(-9)} 50004 cache.go:189] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:fetchUserInfo\": unexpected EOF",
                    f"ERROR: logging before google.Init: E{_g(-8)} 50005 client.go:88] Post \"https://play.googleapis.com/log\": unexpected EOF",
                ]) + "\n",
                encoding="utf-8",
            )
            eng = _make_engine(root, read_only=True)
            eng._ledger.observe_online("ac***1", now - 3600)
            eng._account_cache = AccountQuotaInfo(short_label="ac***1", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T2")
            self.assertEqual(snap.proxy_outage_timeout_recent, 0)

        # 2) Dual-review M1-B: Stale burst 28m ago + long silence + 1 stray EOF + 2 real timeouts MUST stay T2!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 12, 30, 0).timestamp()

            def _g(dt_sec: float) -> str:
                return datetime.datetime.fromtimestamp(now + dt_sec).strftime("%m%d %H:%M:%S.100000")

            (root / "language_server.log").write_text(
                "\n".join([
                    # 28m ago brief proxy blip
                    f"ERROR: logging before google.Init: E{_g(-1680)} 51001 telemetry.go:142] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics\": unexpected EOF",
                    f"ERROR: logging before google.Init: W{_g(-1678)} 51002 cache.go:189] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:fetchUserInfo\": unexpected EOF",
                    f"ERROR: logging before google.Init: E{_g(-1676)} 51003 client.go:88] Post \"https://play.googleapis.com/log\": unexpected EOF",
                    # 26m of complete silence, then 1 stray telemetry EOF & 2 real server timeouts
                    f"ERROR: logging before google.Init: E{_g(-100)} 51004 telemetry.go:142] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics\": unexpected EOF",
                    f"ERROR: logging before google.Init: I{_g(-60)} 51005 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                    f"ERROR: logging before google.Init: I{_g(-20)} 51006 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                ]) + "\n",
                encoding="utf-8",
            )
            eng = _make_engine(root, read_only=True)
            eng._ledger.observe_online("ac***2", now - 3600)
            eng._account_cache = AccountQuotaInfo(short_label="ac***2", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T2")
            self.assertEqual(snap.proxy_outage_timeout_recent, 0)

        # 3) Dual-review M2: A -> B -> A account re-entry MUST NOT leak prior session's proxy burst across current_acct_since_ts!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 13, 0, 0).timestamp()

            def _g(dt_sec: float) -> str:
                return datetime.datetime.fromtimestamp(now + dt_sec).strftime("%m%d %H:%M:%S.100000")

            (root / "language_server.log").write_text(
                "\n".join([
                    # Session 1 on A (t-1200s): proxy burst
                    f"ERROR: logging before google.Init: E{_g(-1200)} 52001 telemetry.go:142] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics\": unexpected EOF",
                    f"ERROR: logging before google.Init: W{_g(-1198)} 52002 cache.go:189] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:fetchUserInfo\": unexpected EOF",
                    f"ERROR: logging before google.Init: E{_g(-1196)} 52003 client.go:88] Post \"https://play.googleapis.com/log\": unexpected EOF",
                    # Session 2 back on A (after t-300s re-entry): 1 stray EOF + 2 timeouts -> MUST be T2!
                    f"ERROR: logging before google.Init: E{_g(-100)} 52004 telemetry.go:142] Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:recordTrajectoryAnalytics\": unexpected EOF",
                    f"ERROR: logging before google.Init: I{_g(-60)} 52005 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                    f"ERROR: logging before google.Init: I{_g(-20)} 52006 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                ]) + "\n",
                encoding="utf-8",
            )
            eng = _make_engine(root, read_only=True)
            eng._ledger.observe_online("ac***a", now - 2000)
            eng._ledger.observe_online("ac***b", now - 900)
            eng._ledger.observe_online("ac***a", now - 300)
            eng._account_cache = AccountQuotaInfo(short_label="ac***a", online=True, observed_at=now)
            eng._last_account_poll = now
            snap = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap.grade, "T2")
            self.assertEqual(snap.proxy_outage_timeout_recent, 0)

        # 4) Dual-review M3 + M5 + R1 + R4: Localhost proxy -> lh3.googleusercontent.com captured; settle to T0 after >=300s & >=6 flagship steps; historical 'today' stays T0!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.datetime(2026, 11, 26, 14, 0, 0).timestamp()
            t_out = now - 400.0

            def _gt(ts_v: float) -> str:
                return datetime.datetime.fromtimestamp(ts_v).strftime("%m%d %H:%M:%S.100000")

            (root / "language_server.log").write_text(
                "\n".join([
                    # Includes localhost 127.0.0.1 proxy error targeting lh3.googleusercontent.com (M5!)
                    f"ERROR: logging before google.Init: W{_gt(t_out - 20)} 53001 user_management.go:139] Get \"https://lh3.googleusercontent.com/a/demo\" via 127.0.0.1:7890: net/http: TLS handshake timeout",
                    f"ERROR: logging before google.Init: W{_gt(t_out - 10)} 53002 cache.go:169] Cache(peopleInfo): Get \"https://www.googleapis.com/oauth2/v2/userinfo\": net/http: TLS handshake timeout",
                    f"ERROR: logging before google.Init: W{_gt(t_out)} 53003 client.go:148] Post \"https://play.googleapis.com/log\": net/http: TLS handshake timeout",
                    f"ERROR: logging before google.Init: I{_gt(t_out + 5)} 53004 run.go:308] Run: attempt 1 failed (request failed: stream idle timeout (10m0s): context deadline exceeded), retrying in 4s",
                ]) + "\n",
                encoding="utf-8",
            )
            dm = root / "brain" / "settle01-aaaa" / ".system_generated" / "logs"
            dm.mkdir(parents=True)
            rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "step_index": 0, "created_at": _iso_utc(t_out - 60), "content": "hi"}]
            for k in range(1, 7):  # 6 flagship main steps after t_out+5
                rows.append({"type": "PLANNER_RESPONSE", "step_index": k, "created_at": _iso_utc(t_out + 30 + k * 30), "model": "gemini-4-neon-xhigh", "input_tokens": 2000, "cache_read_tokens": 50000 + k * 100, "output_tokens": 300})
            (dm / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            os.utime(dm / "transcript.jsonl", (now - 10, now - 10))

            eng = _make_engine(root, read_only=True)
            eng._ledger.observe_online("ac***3", now - 3600)
            eng._account_cache = AccountQuotaInfo(short_label="ac***3", online=True, observed_at=now)
            eng._last_account_poll = now
            # Check M5 directly on tailer
            eng._log_tailer.poll(now_ts=now)
            cnt_d, _, _, _, kind_d = eng._log_tailer.get_proxy_outage_detail(now, 30)
            self.assertEqual(cnt_d, 3)
            self.assertEqual(kind_d, "tls_60s")
            # Live 30m: >300s & 6 flagship main steps after both burst and proxy timeout -> settled cleanly back to T0!
            snap_live = eng.poll(now_ts=now, force_account=False)
            self.assertEqual(snap_live.grade, "T0")
            self.assertEqual(snap_live.proxy_outage_timeout_recent, 1)
            self.assertIn("已平复", snap_live.sub_headline)
            txt_sum = format_text_summary(snap_live)
            self.assertIn("代理超时:", txt_sum)
            # Historical 'today' view MUST also stay T0 (never escalate WARN for transient proxy timeouts)
            snap_today = eng.poll(now_ts=now, force_account=False, history_range="today")
            self.assertEqual(snap_today.grade, "T0")


if __name__ == "__main__":
    unittest.main()
