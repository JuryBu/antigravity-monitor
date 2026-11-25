"""Dedicated unit tests for v7.3.0 Lane 1 (Infrastructure + Next-Gen Adaptive Steps)."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core.account_ledger import AccountSwitchLedger, normalize_account_tier
from core.adaptive_steps import (
    AdaptiveStepsResult,
    compute_account_time_decayed_records,
    compute_adaptive_steps_v2,
    compute_effective_weighted_steps,
)
from core.config_store import ConfigStore, DEFAULT_CONFIG, sanitize_config
from core.slot_manager import (
    SlotCleanupResult,
    SlotInventoryScanner,
    SlotSubagentCleaner,
)

_FORBIDDEN_JARGON = (
    "ewma",
    "shrinkage",
    "beta",
    "uncached_m",
    "effective_quota_load",
    "half_life",
    "w_peer",
    "w_own",
)


def _assert_plain_chinese(tc: unittest.TestCase, res: AdaptiveStepsResult) -> None:
    texts = [res.note] + list(res.detail_lines)
    tc.assertTrue(res.note.strip())
    tc.assertGreaterEqual(len(res.detail_lines), 1)
    tc.assertLessEqual(len(res.detail_lines), 4)
    for txt in texts:
        low = txt.lower()
        for bad in _FORBIDDEN_JARGON:
            tc.assertNotIn(bad, low, f"Found forbidden jargon {bad!r} in {txt!r}")


class TestAccountLedgerV730(unittest.TestCase):
    def test_normalize_account_tier_variants(self) -> None:
        self.assertEqual(normalize_account_tier("g1-ultra-tier"), "ultra")
        self.assertEqual(normalize_account_tier("G1-PRO-TIER"), "pro")
        self.assertEqual(normalize_account_tier("free-tier"), "free")
        self.assertEqual(normalize_account_tier(""), "")
        self.assertEqual(normalize_account_tier("enterprise-custom"), "")
        self.assertEqual(normalize_account_tier(None), "")  # type: ignore[arg-type]

    def test_account_tiers_persistence_throttle_eviction_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.json"
            now = 1795470000.0

            # read_only=True must NEVER create or write disk
            ro = AccountSwitchLedger(ledger_path=lp, read_only=True)
            self.assertFalse(ro.observe_account_tier("zm***1", "g1-ultra-tier", now))
            self.assertFalse(lp.exists())
            self.assertEqual(ro.get_account_tier("zm***1"), "")

            rw = AccountSwitchLedger(ledger_path=lp, read_only=False)
            # Invalid tier or invalid label ignored
            self.assertFalse(rw.observe_account_tier("zm***1", "", now))
            self.assertFalse(rw.observe_account_tier("离线", "g1-ultra-tier", now))
            self.assertFalse(lp.exists())

            # First observation writes disk
            self.assertTrue(rw.observe_account_tier("zm***1", "g1-ultra-tier", now))
            self.assertTrue(lp.exists())
            self.assertEqual(rw.get_account_tier("ZM***1"), "ultra")
            self.assertEqual(rw.get_all_account_tiers(), {"zm***1": "ultra"})

            # Same tier within 24h throttled (no write)
            self.assertFalse(rw.observe_account_tier("zm***1", "g1-ultra-tier", now + 3600.0))
            # Same tier after >=24h refreshes updated_at
            self.assertTrue(rw.observe_account_tier("zm***1", "g1-ultra-tier", now + 86400.0))
            # Tier change writes immediately
            self.assertTrue(rw.observe_account_tier("zm***1", "g1-pro-tier", now + 86410.0))
            self.assertEqual(rw.get_account_tier("zm***1"), "pro")

            # Reload round-trip
            reloaded = AccountSwitchLedger(ledger_path=lp, read_only=True)
            self.assertEqual(reloaded.get_account_tier("zm***1"), "pro")
            self.assertEqual(reloaded.get_all_account_tiers(), {"zm***1": "pro"})

            # 32-account cap evicts oldest updated_at
            for idx in range(35):
                rw.observe_account_tier(f"u{idx:02d}***a", "g1-ultra-tier", now + 100000.0 + idx * 10.0)
            self.assertLessEqual(len(rw.get_all_account_tiers()), 32)
            self.assertEqual(rw.get_account_tier("u34***a"), "ultra")

    def test_hourly_buckets_compact_v1_compat_dk_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.json"
            now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
            h0 = int(now // 3600) - 5

            lgr = AccountSwitchLedger(ledger_path=lp, read_only=False)
            lgr.observe_account_tier("ga***3", "g1-pro-tier", now)

            # 1) Pure step count serializes as bare int (preserves test_monitor.py:5320!)
            self.assertTrue(lgr.record_hourly_step_buckets("al***1", {h0: 200}, now_ts=now))
            raw = json.loads(lp.read_text(encoding="utf-8"))
            self.assertEqual(raw["hourly_step_buckets"]["al***1"][str(h0)], 200)

            # 2) t1_single stores d=False (omitted) + dk="t1_single", and _load_own parses dk unconditionally
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "al***1",
                    {h0 + 1: 180},
                    degraded_kinds={h0 + 1: "t1_single"},
                    hour_meta={h0 + 1: {"m": 120, "sub": 60, "lt": 30, "p3": 20, "uc": 4500}},
                    now_ts=now,
                )
            )
            raw2 = json.loads(lp.read_text(encoding="utf-8"))
            slot_obj = raw2["hourly_step_buckets"]["al***1"][str(h0 + 1)]
            self.assertIsInstance(slot_obj, dict)
            self.assertNotIn("d", slot_obj)
            self.assertEqual(slot_obj["dk"], "t1_single")
            self.assertEqual(slot_obj["m"], 120)
            self.assertEqual(slot_obj["sub"], 60)
            self.assertEqual(slot_obj["lt"], 30)
            self.assertEqual(slot_obj["p3"], 20)
            self.assertEqual(slot_obj["uc"], 4500)

            rel = AccountSwitchLedger(ledger_path=lp, read_only=True)
            b_al = rel.get_account_hourly_buckets("al***1")
            self.assertFalse(b_al[h0 + 1]["degraded"])
            self.assertEqual(b_al[h0 + 1]["dk"], "t1_single")
            self.assertEqual(b_al[h0 + 1]["m"], 120)
            self.assertEqual(b_al[h0 + 1]["p3"], 20)
            self.assertEqual(b_al[h0 + 1]["uc"], 4500)

            # 3) Escalating dk from t1_single -> t2 upgrades to degraded=True and persists
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "al***1",
                    {h0 + 1: 185},
                    degraded_hours={h0 + 1: 900},
                    degraded_kinds={h0 + 1: "t2"},
                    now_ts=now,
                )
            )
            b_al2 = lgr.get_account_hourly_buckets("al***1")
            self.assertTrue(b_al2[h0 + 1]["degraded"])
            self.assertEqual(b_al2[h0 + 1]["dk"], "t2")
            self.assertEqual(b_al2[h0 + 1]["degraded_24h_at"], 900)

            # 4) Escalating t1_single via legacy degraded_hours without dk clears stale t1_single
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "al***1",
                    {h0 + 2: 100},
                    degraded_kinds={h0 + 2: "t1_single"},
                    now_ts=now,
                )
            )
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "al***1",
                    {h0 + 2: 110},
                    degraded_hours={h0 + 2: 650},
                    now_ts=now,
                )
            )
            b_al3 = lgr.get_account_hourly_buckets("al***1")
            self.assertTrue(b_al3[h0 + 2]["degraded"])
            self.assertEqual(b_al3[h0 + 2]["dk"], "")
            self.assertEqual(b_al3[h0 + 2]["degraded_24h_at"], 650)

            # 5) _pick_preferred_deg_at supports tier-aware deg_min (Pro=50 vs Ultra=400)
            self.assertEqual(AccountSwitchLedger._pick_preferred_deg_at(20, 63, deg_min=400), 20)
            self.assertEqual(AccountSwitchLedger._pick_preferred_deg_at(20, 63, deg_min=50), 63)
            self.assertEqual(AccountSwitchLedger._pick_preferred_deg_at(63, 180, deg_min=50), 63)

            # Pro account automatically uses deg_min=50 in record_hourly_step_buckets
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "ga***3",
                    {h0: 63},
                    degraded_hours={h0: 20},
                    now_ts=now,
                )
            )
            self.assertTrue(
                lgr.record_hourly_step_buckets(
                    "ga***3",
                    {h0: 63},
                    degraded_hours={h0: 63},
                    now_ts=now,
                )
            )
            self.assertEqual(lgr.get_account_hourly_buckets("ga***3")[h0]["degraded_24h_at"], 63)

            # 6) get_all_hourly_step_buckets returns read-only copies
            all_hb = lgr.get_all_hourly_step_buckets()
            self.assertIn("al***1", all_hb)
            self.assertIn("ga***3", all_hb)
            all_hb["al***1"][h0]["steps"] = 999999
            self.assertEqual(lgr.get_account_hourly_buckets("al***1")[h0]["steps"], 200)

    def test_v1_legacy_json_compat_and_case_insensitive_meta_merge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.json"
            h0 = 498700
            legacy_doc = {
                "version": 1,
                "events": [{"ts": 1795400000.0, "label": "zm***1"}],
                "hourly_step_buckets": {
                    "zm***1": {
                        str(h0): 300,
                        str(h0 + 1): {"s": 500, "d": True, "at": 900},
                        str(h0 + 2): {"s": 120, "dk": "t1_single", "m": 80, "p3": 10},
                    },
                    "ZM***1": {
                        str(h0 + 2): {"s": 140, "sub": 60, "uc": 2000},
                    },
                },
            }
            lp.write_text(json.dumps(legacy_doc), encoding="utf-8")
            lgr = AccountSwitchLedger(ledger_path=lp, read_only=True)
            hb = lgr.get_account_hourly_buckets("zm***1")
            self.assertEqual(hb[h0]["steps"], 300)
            self.assertFalse(hb[h0]["degraded"])
            self.assertEqual(hb[h0]["dk"], "")
            self.assertTrue(hb[h0 + 1]["degraded"])
            self.assertEqual(hb[h0 + 1]["degraded_24h_at"], 900)
            self.assertFalse(hb[h0 + 2]["degraded"])
            self.assertEqual(hb[h0 + 2]["dk"], "t1_single")
            self.assertEqual(hb[h0 + 2]["steps"], 140)
            self.assertEqual(hb[h0 + 2]["m"], 80)
            self.assertEqual(hb[h0 + 2]["sub"], 60)
            self.assertEqual(hb[h0 + 2]["p3"], 10)
            self.assertEqual(hb[h0 + 2]["uc"], 2000)


class TestAdaptiveStepsV2(unittest.TestCase):
    def test_no_circular_import_of_log_monitor(self) -> None:
        src = (
            Path(__file__).resolve().parent.parent / "core" / "adaptive_steps.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("import core.log_monitor", src)
        self.assertNotIn("from core.log_monitor", src)
        self.assertNotIn("from .log_monitor", src)

    def test_effective_weighted_steps_pool_isolation_and_cold_premium(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        cur_h = int(now // 3600)
        # Mixed hour: 1000 total = 800 Gemini (400 light) + 200 3P, total uc = 50,000K (50K/step)
        buckets = {
            cur_h - 1: {"steps": 1000, "p3": 200, "lt": 400, "uc": 50000},
        }
        # Gemini: raw=800, base_w = 400 + 400*0.75 = 700, avg_uc_k = (50000*0.8)/800 = 50 -> mult=1.5 -> 1050
        g_raw, g_eff = compute_effective_weighted_steps(buckets, now_ts=now, pool_family="gemini")
        self.assertEqual((g_raw, g_eff), (800, 1050))
        # 3P: raw=200, MUST NOT subtract Gemini lt=400! base_w=200, avg_uc_k=(50000*0.2)/200=50 -> mult=1.5 -> 300
        p3_raw, p3_eff = compute_effective_weighted_steps(buckets, now_ts=now, pool_family="3p")
        self.assertEqual((p3_raw, p3_eff), (200, 300))
        # Cold cap at 2.5x even when avg_uc_k = 300K
        huge_uc = {cur_h - 1: {"steps": 100, "p3": 0, "lt": 0, "uc": 30000}}
        self.assertEqual(compute_effective_weighted_steps(huge_uc, now_ts=now, pool_family="gemini"), (100, 250))

    def test_single_day_exact_preservation_and_4200_soft_cap(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_yest = int(datetime.datetime(2026, 11, 22, 10, 0, 0).timestamp() // 3600)

        # 1) 1300 -> 1500 exact (compat test_monitor.py:4534)
        res_1300 = compute_adaptive_steps_v2(
            960, 1200, {h_yest: {"steps": 1300}}, now_ts=now, account_tier="ultra"
        )
        self.assertEqual((res_1300.mode, res_1300.hist_safe_peak, res_1300.alert_limit), ("adaptive_up", 1300, 1500))
        _assert_plain_chinese(self, res_1300)

        # 2) 3048 -> 3600 exact (compat test_monitor.py:6875)
        res_3048 = compute_adaptive_steps_v2(
            960, 1200, {h_yest: {"steps": 3048}}, now_ts=now, account_tier="ultra"
        )
        self.assertEqual((res_3048.mode, res_3048.hist_safe_peak, res_3048.alert_limit), ("adaptive_up", 3048, 3600))

        # 3) be***2 single-day 4558 soft-capped at 4200 (W_own < 1.2 & known tier)
        res_ca = compute_adaptive_steps_v2(
            960, 1200, {h_yest: {"steps": 4558}}, now_ts=now, account_tier="ultra"
        )
        self.assertEqual(res_ca.mode, "adaptive_up")
        self.assertEqual(res_ca.alert_limit, 4200)

        # 4) Two full days of 4558 -> W_own >= 1.2 unlocks up to 5000
        h_prev = int(datetime.datetime(2026, 11, 21, 10, 0, 0).timestamp() // 3600)
        res_ca2 = compute_adaptive_steps_v2(
            960,
            1200,
            {h_prev: {"steps": 4558}, h_yest: {"steps": 4558}},
            now_ts=now,
            account_tier="ultra",
        )
        self.assertEqual(res_ca2.alert_limit, 5000)

    def test_time_decay_policy_change_and_ancient_peak_filter(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_10d = int((now - 10 * 86400) // 3600)
        h_3d = int((now - 3 * 86400) // 3600)
        h_2d = int((now - 2 * 86400) // 3600)
        # Policy reform: 10d ago 4500 -> recent 2 days 2000
        buckets = {
            h_10d: {"steps": 4500},
            h_3d: {"steps": 2000},
            h_2d: {"steps": 2000},
        }
        res = compute_adaptive_steps_v2(960, 1200, buckets, now_ts=now, account_tier="ultra")
        self.assertEqual(res.mode, "adaptive_up")
        self.assertLessEqual(res.alert_limit, 2800)
        self.assertGreaterEqual(res.alert_limit, 2300)

        # Ancient giant peak (14d ago 12000) MUST NOT filter out recent 600-step day via max(w_i * p_i)
        h_14d = int((now - 14 * 86400) // 3600)
        buckets_anc = {h_14d: {"steps": 12000}, h_2d: {"steps": 600}}
        res_anc = compute_adaptive_steps_v2(960, 1200, buckets_anc, now_ts=now, account_tier="ultra")
        self.assertLess(res_anc.hist_safe_peak, 2500)

    def test_pro_tier_low_step_tightening_and_zero_cross_tier_contamination(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_yest = int(datetime.datetime(2026, 11, 22, 10, 0, 0).timestamp() // 3600)
        # ga***3 (Pro): 63 steps degraded T1 -> tightens to 100 despite healthy Ultra peer at 4500
        pro_buckets = {h_yest: {"steps": 63, "degraded": True, "degraded_24h_at": 63, "dk": "t1_multi"}}
        ultra_peer = {"zm***1": {h_yest: {"steps": 4500}}}
        res = compute_adaptive_steps_v2(
            960,
            1200,
            pro_buckets,
            now_ts=now,
            account_tier="g1-pro-tier",
            account_label="ga***3",
            peer_hourly_by_account=ultra_peer,
            peer_tiers={"zm***1": "g1-ultra-tier", "ga***3": "g1-pro-tier"},
        )
        self.assertEqual(res.mode, "adaptive_down")
        self.assertEqual(res.alert_limit, 100)
        self.assertEqual(res.peer_prior_peak, 0)
        self.assertEqual(res.peer_weight_pct, 0)
        _assert_plain_chinese(self, res)

        # Free tier deg_min=30 / floor=80
        free_buckets = {h_yest: {"steps": 40, "degraded": True, "degraded_24h_at": 40, "dk": "t2"}}
        res_free = compute_adaptive_steps_v2(960, 1200, free_buckets, now_ts=now, account_tier="free")
        self.assertEqual(res_free.mode, "adaptive_down")
        self.assertEqual(res_free.alert_limit, 80)

    def test_t1_single_does_not_contaminate_or_tighten(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_day = int(datetime.datetime(2026, 11, 22, 10, 0, 0).timestamp() // 3600)
        buckets = {
            h_day: {"steps": 600, "degraded": False, "dk": "t1_single"},
            h_day + 1: {"steps": 1000, "degraded": False},
        }
        rec = compute_account_time_decayed_records(buckets, now, pool_family="all")
        self.assertEqual(rec["t1_single_ignored_count"], 1)
        self.assertEqual(len(rec["daily_peaks"]), 1)
        self.assertIsNone(rec["daily_peaks"][0]["degraded_at"])
        self.assertEqual(rec["daily_peaks"][0]["safe_steps"], 1600)

        res = compute_adaptive_steps_v2(960, 1200, buckets, now_ts=now, account_tier="ultra")
        self.assertEqual(res.mode, "adaptive_up")
        self.assertEqual(res.alert_limit, 1900)  # ceil(1600*1.15/100)*100 = 1900

    def test_4h_refutation_to_default_bridge_and_brand_new_guard(self) -> None:
        # 1) 4h recovery refutes old tightening -> unlocks back to default 1200 (compat test_monitor.py:6866)
        now_0330 = datetime.datetime(2026, 11, 24, 3, 30, 0).timestamp()
        h_20_19 = int(datetime.datetime(2026, 11, 20, 19, 0, 0).timestamp() // 3600)
        h_23_00 = int(datetime.datetime(2026, 11, 23, 0, 0, 0).timestamp() // 3600)
        buckets_refute = {
            h_20_19: {"steps": 33, "degraded": True, "degraded_24h_at": 440},
            h_23_00: {"steps": 54, "degraded": True, "degraded_24h_at": 134},
            h_23_00 + 4: {"steps": 789, "degraded": True, "degraded_24h_at": 1052},
            h_23_00 + 5: {"steps": 968},
            h_23_00 + 6: {"steps": 780},
            h_23_00 + 7: {"steps": 700},
            h_23_00 + 8: {"steps": 600},
        }
        res_ref = compute_adaptive_steps_v2(960, 1200, buckets_refute, now_ts=now_0330, account_tier="ultra")
        self.assertEqual((res_ref.mode, res_ref.alert_limit), ("default", 1200))

        # Live T2 strictly locks out refutation!
        res_t2 = compute_adaptive_steps_v2(
            960, 1200, buckets_refute, now_ts=now_0330, account_tier="ultra", live_grade="T2"
        )
        self.assertEqual(res_t2.mode, "adaptive_down")
        self.assertEqual(res_t2.alert_limit, 600)

        # 2) Cross-midnight smooth bridge (13 healthy non-empty hours with lunch gaps = 5641 -> 3200; 16h -> 4000)
        now_0555 = datetime.datetime(2026, 11, 24, 5, 55, 0).timestamp()
        cur_h = int(now_0555 // 3600)
        h_deg = cur_h - 25  # degraded 25h ago (>=12h)
        bridge_13h = {h_deg: {"steps": 100, "degraded": True, "degraded_24h_at": 500}}
        # 13 non-empty healthy hours with 0-step lunch gaps in between summing to 5641
        offsets = [1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15, 17]
        per_h = [434] * 12 + [5641 - 434 * 12]
        for off, val in zip(offsets, per_h):
            bridge_13h[h_deg + off] = {"steps": val}
        res_b13 = compute_adaptive_steps_v2(960, 1200, bridge_13h, now_ts=now_0555, account_tier="ultra")
        self.assertEqual(res_b13.mode, "adaptive_up")
        self.assertEqual(res_b13.alert_limit, 3200)
        self.assertLessEqual(res_b13.alert_limit, int(5641 * 0.70) + 100)

        # Live T1 strictly forbids bridge relaxation
        res_b13_t1 = compute_adaptive_steps_v2(
            960, 1200, bridge_13h, now_ts=now_0555, account_tier="ultra", live_grade="T1"
        )
        self.assertEqual(res_b13_t1.mode, "adaptive_down")

        # 3) Brand-new account on Day 1 MUST NOT chase its own live steps via bridge!
        new_acct_buckets = {cur_h - k: {"steps": 500} for k in range(1, 11)}  # 10 hours, 5000 steps today, 0 history
        res_new = compute_adaptive_steps_v2(960, 1200, new_acct_buckets, now_ts=now_0555, account_tier="ultra")
        self.assertEqual((res_new.mode, res_new.alert_limit), ("default", 1200))

        # 4) 16h continuous recovery bridge caps maturity at 0.70x -> ceil(5641*0.70/100)*100 == 4000
        bridge_16h = {h_deg: {"steps": 100, "degraded": True, "degraded_24h_at": 500}}
        per_16 = [352] * 15 + [5641 - 352 * 15]
        for idx, val in enumerate(per_16, start=1):
            bridge_16h[h_deg + idx] = {"steps": val}
        res_b16 = compute_adaptive_steps_v2(960, 1200, bridge_16h, now_ts=now_0555, account_tier="ultra")
        self.assertEqual(res_b16.mode, "adaptive_up")
        self.assertEqual(res_b16.alert_limit, 4000)

    def test_3p_pool_decayed_records_and_free_tier_cap(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_2d = int((now - 2 * 86400) // 3600)
        buckets = {h_2d: {"steps": 2000, "p3": 450}}
        rec_3p = compute_account_time_decayed_records(buckets, now, pool_family="3p")
        self.assertEqual(rec_3p["daily_peaks"][0]["safe_steps"], 450)
        rec_gem = compute_account_time_decayed_records(buckets, now, pool_family="gemini")
        self.assertEqual(rec_gem["daily_peaks"][0]["safe_steps"], 1550)

        # Free tier cap = min(base_alert, 600) never inflates above 600 even with high history
        res_free_hi = compute_adaptive_steps_v2(400, 500, {h_2d: {"steps": 3000}}, now_ts=now, account_tier="free")
        self.assertLessEqual(res_free_hi.alert_limit, 600)

    def test_same_tier_peer_prior_cold_start_anti_inflation_and_unsettled_guard(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_2d = int((now - 2 * 86400) // 3600)
        h_3d = int((now - 3 * 86400) // 3600)
        cur_h = int(now // 3600)

        peers = {
            "be***2": {h_2d: {"steps": 4400}},
            "ga***3": {h_2d: {"steps": 200}},
        }
        tiers = {"be***2": "ultra", "ep***5": "ultra", "de***4": "ultra", "ga***3": "pro"}

        # 1) Cold-start Ultra ep***5 borrows 25% peer prior: 1200*0.75 + 4400*0.25 = 2000
        res_cold = compute_adaptive_steps_v2(
            960,
            1200,
            {},
            now_ts=now,
            account_tier="ultra",
            account_label="ep***5",
            peer_hourly_by_account=peers,
            peer_tiers=tiers,
        )
        self.assertEqual((res_cold.mode, res_cold.alert_limit), ("adaptive_up", 2000))
        self.assertEqual(res_cold.peer_weight_pct, 25)
        self.assertEqual(res_cold.peer_prior_peak, 4400)
        _assert_plain_chinese(self, res_cold)

        # 2) Account with own history (de***4 = 1595) MUST NEVER be pushed above own_ceil=1900 by strong peer!
        res_na = compute_adaptive_steps_v2(
            960,
            1200,
            {h_3d: {"steps": 1595}},
            now_ts=now,
            account_tier="ultra",
            account_label="de***4",
            peer_hourly_by_account=peers,
            peer_tiers=tiers,
        )
        self.assertEqual(res_na.alert_limit, 1900)
        self.assertGreater(res_na.peer_weight_pct, 0)
        self.assertLessEqual(res_na.peer_weight_pct, 16)

        # 3) Unsettled 24h major degradation on own account FORBIDS borrowing peer prior!
        res_unsettled = compute_adaptive_steps_v2(
            960,
            1200,
            {cur_h - 2: {"steps": 100, "degraded": True, "degraded_24h_at": 100, "dk": "t2"}},
            now_ts=now,
            account_tier="ultra",
            account_label="ep***5",
            peer_hourly_by_account=peers,
            peer_tiers=tiers,
        )
        self.assertEqual(res_unsettled.peer_prior_peak, 0)
        self.assertEqual(res_unsettled.alert_limit, 1200)

    def test_pool_consistency_quota_headroom_low_quota_and_soft_over_limit(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_2d = int((now - 2 * 86400) // 3600)
        cur_h = int(now // 3600)

        # 1) 2500 Gemini history + 5 3P steps today -> switching pool_family="3p" MUST NOT collapse alert_limit to 1200!
        buckets_mix = {
            h_2d: {"steps": 2500, "p3": 0},
            cur_h - 1: {"steps": 2505, "p3": 5},
        }
        res_3p = compute_adaptive_steps_v2(
            960,
            1200,
            buckets_mix,
            now_ts=now,
            account_tier="ultra",
            pool_family="3p",
            live_24h_raw_steps=2505,
        )
        self.assertEqual(res_3p.alert_limit, 2900)  # ceil(2500*1.15/100)*100
        self.assertEqual(res_3p.gemini_24h_steps, 2500)
        self.assertEqual(res_3p.third_party_24h_steps, 5)
        self.assertEqual(res_3p.pool_24h_steps, 5)

        # 2) Low quota tightening: no false positive when live_24h == 0 or when alert is not lowered
        res_no_fp0 = compute_adaptive_steps_v2(
            960, 1200, {}, now_ts=now, account_tier="ultra", live_24h_raw_steps=0, live_5h_pct=8.0
        )
        self.assertFalse(res_no_fp0.low_quota_tightened)
        self.assertEqual(res_no_fp0.alert_limit, 1200)

        res_no_fp_hi = compute_adaptive_steps_v2(
            960, 1200, {}, now_ts=now, account_tier="ultra", live_24h_raw_steps=1400, live_5h_pct=8.0
        )
        self.assertFalse(res_no_fp_hi.low_quota_tightened)

        # Real low-quota tightening (weekly <= 10% lowers alert from 2900 -> 950 with isolated weekly cushion) + quota_headroom_steps
        res_tight = compute_adaptive_steps_v2(
            960,
            1200,
            {h_2d: {"steps": 2500}},
            now_ts=now,
            account_tier="ultra",
            live_24h_raw_steps=800,
            live_5h_pct=60.0,
            live_weekly_pct=5.0,
            burn_pct_per_hour=10.0,
            last_1h_steps=100,
        )
        self.assertTrue(res_tight.low_quota_tightened)
        self.assertEqual(res_tight.quota_headroom_steps, 600)
        self.assertEqual(res_tight.alert_limit, 950)
        self.assertEqual(res_tight.mode, "adaptive_down")
        _assert_plain_chinese(self, res_tight)

        # 3) Ample quota + T0 + over limit -> soft_over_limit = True
        res_soft = compute_adaptive_steps_v2(
            960,
            1200,
            {},
            now_ts=now,
            account_tier="ultra",
            live_grade="T0",
            live_24h_raw_steps=1500,
            live_5h_pct=80.0,
            live_weekly_pct=75.0,
            will_exhaust_before_reset=False,
        )
        self.assertTrue(res_soft.soft_over_limit)
        self.assertTrue(any("放心继续" in ln for ln in res_soft.detail_lines))
        _assert_plain_chinese(self, res_soft)

        # will_exhaust_before_reset=True or under limit -> soft_over_limit = False
        res_exh = compute_adaptive_steps_v2(
            960,
            1200,
            {},
            now_ts=now,
            account_tier="ultra",
            live_grade="T0",
            live_24h_raw_steps=1500,
            live_5h_pct=80.0,
            live_weekly_pct=75.0,
            will_exhaust_before_reset=True,
        )
        self.assertFalse(res_exh.soft_over_limit)


class TestConfigStoreV730(unittest.TestCase):
    def test_default_thresholds_aligned(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["freshness_window_sec"], 210)
        self.assertEqual(DEFAULT_CONFIG["idle_rewarm_sec"], 210)
        self.assertEqual(DEFAULT_CONFIG["slow_tps_threshold"], 40.0)
        clean = sanitize_config({})
        self.assertEqual(clean["idle_rewarm_sec"], 210)
        self.assertEqual(clean["slow_tps_threshold"], 40.0)

    def test_save_patch_preserves_manual_disk_edits_and_update_mode_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / "config.json"
            store = ConfigStore(cp)
            store.save({"theme": "midnight_blue", "window_x": 100, "window_y": 100})

            # Simulate user manually editing config.json on disk
            disk_obj = json.loads(cp.read_text(encoding="utf-8"))
            disk_obj["daily_steps_alert"] = 2400
            disk_obj["theme"] = "obsidian_dark"
            cp.write_text(json.dumps(disk_obj), encoding="utf-8")

            # Dragging window calls save_patch -> MUST NOT clobber manual edits!
            after = store.save_patch({"window_x": 320, "window_y": 180})
            self.assertEqual(after["window_x"], 320)
            self.assertEqual(after["window_y"], 180)
            self.assertEqual(after["daily_steps_alert"], 2400)
            self.assertEqual(after["theme"], "obsidian_dark")

            # Patching only auto_update_restart synchronizes update_mode bidirectionally
            p1 = store.save_patch({"auto_update_restart": True})
            self.assertEqual(p1["update_mode"], "auto_restart")
            self.assertTrue(p1["auto_update_restart"])
            p2 = store.save_patch({"auto_update_restart": False})
            self.assertEqual(p2["update_mode"], "download")
            self.assertFalse(p2["auto_update_restart"])
            p3 = store.save_patch({"update_mode": "auto_restart"})
            self.assertEqual(p3["update_mode"], "auto_restart")
            self.assertTrue(p3["auto_update_restart"])
            p4 = store.save_patch({"update_mode": "notify"})
            self.assertEqual(p4["update_mode"], "notify")
            self.assertFalse(p4["auto_update_restart"])

            # save(..., merge_disk=True) also preserves disk fields
            p5 = store.save({"poll_interval_ms": 3000}, merge_disk=True)
            self.assertEqual(p5["poll_interval_ms"], 3000)
            self.assertEqual(p5["daily_steps_alert"], 2400)

    def test_transient_read_failure_fallback_retry_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / "config.json"
            store = ConfigStore(cp)
            store.save({"theme": "obsidian_dark", "daily_steps_alert": 3300})

            # Corrupt file transiently -> load() falls back to _last_known_config instead of wiping to defaults
            cp.write_text("{truncated json", encoding="utf-8")
            fallback = store.load()
            self.assertEqual(fallback["theme"], "obsidian_dark")
            self.assertEqual(fallback["daily_steps_alert"], 3300)

            # save_patch during transient corruption also builds on _last_known_config
            healed = store.save_patch({"window_x": 555})
            self.assertEqual(healed["theme"], "obsidian_dark")
            self.assertEqual(healed["daily_steps_alert"], 3300)
            self.assertEqual(healed["window_x"], 555)

            # Verify Windows os.replace retries transient PermissionError and tmp filename includes pid & tid
            seen_tmps = []
            real_replace = os.replace
            calls = {"n": 0}

            def _flaky_replace(src, dst):
                seen_tmps.append(Path(src).name)
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(13, "transient lock")
                return real_replace(src, dst)

            with mock.patch("core.config_store.os.replace", side_effect=_flaky_replace):
                res = store.save_patch({"window_y": 666})
            self.assertEqual(res["window_y"], 666)
            self.assertEqual(calls["n"], 3)
            self.assertTrue(any(f".{os.getpid()}.{threading.get_ident()}" in name for name in seen_tmps))

            # Concurrent save_patch from multiple threads loses zero updates
            errors = []

            def _worker(key: str, val: object) -> None:
                try:
                    for _ in range(5):
                        store.save_patch({key: val})
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=_worker, args=("window_x", 777)),
                threading.Thread(target=_worker, args=("window_y", 888)),
                threading.Thread(target=_worker, args=("conv_list_rows", 9)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            final_cfg = store.load()
            self.assertEqual(final_cfg["window_x"], 777)
            self.assertEqual(final_cfg["window_y"], 888)
            self.assertEqual(final_cfg["conv_list_rows"], 9)


class TestSlotManagerV730(unittest.TestCase):
    @staticmethod
    def _build_env(root: Path, rows: list) -> tuple:
        conv_dir = root / "conversations"
        brain_dir = root / "brain"
        conv_dir.mkdir(parents=True, exist_ok=True)
        brain_dir.mkdir(parents=True, exist_ok=True)
        sum_db = root / "conversation_summaries.db"
        conn = sqlite3.connect(str(sum_db))
        conn.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                step_count INTEGER,
                last_modified_time TEXT,
                status TEXT,
                agent_name TEXT,
                parent_conversation_id TEXT,
                nesting_depth INTEGER,
                not_fully_idle INTEGER
            )
            """
        )
        for r in rows:
            cid, age_sec, is_sub, brain_bytes = r
            dbp = conv_dir / f"{cid}.db"
            dbp.write_bytes(b"x" * 1024)
            mtime = time.time() - age_sec
            os.utime(dbp, (mtime, mtime))
            bdir = brain_dir / cid
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "payload.bin").write_bytes(b"y" * brain_bytes)
            os.utime(bdir, (mtime, mtime))
            iso = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    "" if is_sub else "Main Chat",
                    10,
                    iso,
                    "CASCADE_RUN_STATUS_IDLE",
                    "helper" if is_sub else "",
                    "parent-1" if is_sub else "",
                    1 if is_sub else 0,
                    0,
                ),
            )
        conn.commit()
        conn.close()
        return conv_dir, brain_dir, sum_db

    def test_active_short_ttl_force_bypass_cache_max_and_targeted_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conv_dir, brain_dir, sum_db = self._build_env(
                root,
                [
                    ("active-111111", 60, False, 4096),       # <30m active dir
                    ("static-222222", 7200, True, 8192),      # >30m static dir
                ],
            )
            scanner = SlotInventoryScanner(conv_dir, brain_dir, sum_db, active_dir_cache_ttl_sec=8.0)
            self.assertEqual(scanner._dir_size_cache_max, 628)

            # First scan populates both active & static cache
            with mock.patch("core.slot_manager._dir_tree_bytes", wraps=__import__("core.slot_manager", fromlist=["_dir_tree_bytes"])._dir_tree_bytes) as spy:
                snap1 = scanner.scan(include_items=True, slot_cap=1000)
                self.assertEqual(scanner._dir_size_cache_max, 1128)
                self.assertEqual(spy.call_count, 2)
                self.assertEqual(len(snap1.items), 2)

                # Immediate second scan hits 8s short TTL for <30m active dir AND 300s TTL for static dir -> 0 scans!
                snap2 = scanner.scan(include_items=True, slot_cap=1000)
                self.assertEqual(spy.call_count, 2)
                self.assertEqual(len(snap2.items), 2)

                # force=True bypasses <30m 8s short cache (rescans active-111111, keeps static-222222 cached!)
                scanner.scan(include_items=True, slot_cap=1000, force=True)
                self.assertEqual(spy.call_count, 3)

            # Targeted invalidation removes only specified cid, preserves sibling, AND bumps epoch!
            epoch_before = scanner._dir_size_epoch
            scanner.invalidate_cache(cids=["active-111111"])
            self.assertEqual(scanner._dir_size_epoch, epoch_before + 1)
            self.assertNotIn("active-111111", scanner._dir_size_cache)
            self.assertIn("static-222222", scanner._dir_size_cache)

            # Full invalidation clears all and bumps epoch again
            scanner.invalidate_cache()
            self.assertEqual(scanner._dir_size_epoch, epoch_before + 2)
            self.assertEqual(len(scanner._dir_size_cache), 0)

    def test_cleanup_distinguishes_recycle_unavailable_vs_locked_and_targeted_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conv_dir, brain_dir, sum_db = self._build_env(
                root,
                [
                    ("sub-ok-111111", 10800, True, 2048),
                    ("sub-rb-222222", 10800, True, 2048),
                    ("sub-lk-333333", 10800, True, 2048),
                    ("main-keep-4444", 10800, False, 4096),
                ],
            )
            scanner = SlotInventoryScanner(conv_dir, brain_dir, sum_db)
            # Warm cache for all 4 items first
            scanner.scan(include_items=True)
            self.assertEqual(len(scanner._dir_size_cache), 4)

            def _fake_recycle(target: Path):
                name = target.name
                if "sub-rb-222222" in name:
                    return False, "recycle_bin_disabled_on_volume"
                if "sub-lk-333333" in name:
                    return False, "SHFileOperationW rc=32 aborted=False"
                if target.is_dir():
                    import shutil
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                return True, ""

            cleaner = SlotSubagentCleaner(scanner, recycle_fn=_fake_recycle)
            res: SlotCleanupResult = cleaner.execute_cleanup(protect_age_min=120, mode="recycle")
            self.assertEqual(res.removed_count, 1)
            self.assertEqual(res.removed_cids, ["sub-ok-111111"])
            self.assertEqual(res.skipped_recycle_unavailable_count, 1)
            self.assertEqual(res.skipped_locked_count, 1)
            self.assertTrue(bool(res.last_skip_reason))

            # Targeted invalidation: removed cid evicted, untouched main-keep-4444 cache preserved!
            self.assertNotIn("sub-ok-111111", scanner._dir_size_cache)
            self.assertIn("main-keep-4444", scanner._dir_size_cache)

    def test_targeted_invalidation_epoch_blocks_inflight_stale_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conv_dir, brain_dir, sum_db = self._build_env(
                root,
                [("inflight-11111", 7200, True, 4096)],
            )
            scanner = SlotInventoryScanner(conv_dir, brain_dir, sum_db)

            def _stale_walk(_path_str: str) -> int:
                # Simulate a concurrent cleanup invalidating this cid mid-scan
                scanner.invalidate_cache(cids=["inflight-11111"])
                return 99999

            with mock.patch("core.slot_manager._dir_tree_bytes", side_effect=_stale_walk):
                sz = scanner._get_brain_dir_bytes(
                    "inflight-11111",
                    str(brain_dir / "inflight-11111"),
                    disk_age_min=120.0,
                    transcript_mtime_ns=0,
                )
            self.assertEqual(sz, 99999)
            # Because invalidate_cache(cids=...) bumped _dir_size_epoch, stale size MUST NOT be cached!
            self.assertNotIn("inflight-11111", scanner._dir_size_cache)


class TestV730FinalAuditRegressions(unittest.TestCase):
    def test_degraded_true_never_whitewashed_by_t1_single_dk(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_yest = int(datetime.datetime(2026, 11, 22, 10, 0, 0).timestamp() // 3600)
        # Legacy account-wide degradation (d=True) co-occurring with t1_single dk MUST remain major degradation
        buckets = {
            h_yest: {"steps": 1800, "degraded": True, "degraded_24h_at": 700, "dk": "t1_single"},
        }
        res = compute_adaptive_steps_v2(960, 1200, buckets, now_ts=now, account_tier="ultra")
        self.assertEqual(res.mode, "adaptive_down")
        self.assertEqual(res.alert_limit, 600)

    def test_bridge_hard_guards_soft_cap_no_ancient_resurrection_and_day1_lock(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        cur_h = int(now // 3600)

        # 1) Single-day 4200 soft cap MUST NOT be pierced by bridge!
        h_sday = int(datetime.datetime(2026, 11, 23, 0, 0, 0).timestamp() // 3600)
        one_day_huge = {h_sday + k: {"steps": 400} for k in range(16)}  # 6400 steps in 16h
        res_cap = compute_adaptive_steps_v2(960, 1200, one_day_huge, now_ts=now, account_tier="ultra")
        self.assertLessEqual(res_cap.alert_limit, 4200)

        # 2) Ancient 10-day-old 4000-step peak MUST NOT bypass 3.5d decay via bridge!
        h_11d = cur_h - 11 * 24
        h_10d = cur_h - 10 * 24
        ancient_b = {h_11d: {"steps": 500, "degraded": True, "degraded_24h_at": 500}}
        for k in range(16):
            ancient_b[h_10d + k] = {"steps": 250}  # 4000 steps 10 days ago
        for d_ago in (5, 4, 3, 2):
            ancient_b[cur_h - d_ago * 24] = {"steps": 1500}  # recent steady 1500/day
        res_anc = compute_adaptive_steps_v2(960, 1200, ancient_b, now_ts=now, account_tier="ultra")
        self.assertLessEqual(res_anc.alert_limit, 2100)

        # 3) Brand-new Day 1 account with sub-threshold minor degradation (<400) MUST NOT chase via bridge!
        day1_b = {cur_h - 12: {"steps": 100, "degraded": True, "degraded_24h_at": 200}}
        for k in range(1, 11):
            day1_b[cur_h - 12 + k] = {"steps": 400}
        res_day1 = compute_adaptive_steps_v2(960, 1200, day1_b, now_ts=now, account_tier="ultra")
        self.assertEqual((res_day1.mode, res_day1.alert_limit), ("default", 1200))

    def test_free_tier_monotonic_and_weekly_burn_isolated_from_5h_burn(self) -> None:
        now = datetime.datetime(2026, 11, 24, 12, 0, 0).timestamp()
        h_2d = int((now - 2 * 86400) // 3600)
        # Free tier stays monotonic at 600 cap from 0 -> 1200 -> 1201 settled steps
        r0 = compute_adaptive_steps_v2(960, 1200, {}, now_ts=now, account_tier="free")
        r1 = compute_adaptive_steps_v2(960, 1200, {h_2d: {"steps": 1200}}, now_ts=now, account_tier="free")
        r2 = compute_adaptive_steps_v2(960, 1200, {h_2d: {"steps": 1201}}, now_ts=now, account_tier="free")
        self.assertEqual((r0.alert_limit, r1.alert_limit, r2.alert_limit), (600, 600, 600))

        # Low weekly (8%) with ample 5h (90%) and high 5h burn (20%/h) MUST NOT apply 5h burn onto weekly %
        r_wk = compute_adaptive_steps_v2(
            960, 1200, {h_2d: {"steps": 2500}}, now_ts=now, account_tier="ultra",
            live_24h_raw_steps=1000, live_5h_pct=90.0, live_weekly_pct=8.0,
            burn_pct_per_hour=20.0, last_1h_steps=100,
        )
        self.assertGreaterEqual(r_wk.alert_limit, 1240)

    def test_high_load_degradation_smooth_fallback_cap_recovery_aging_and_guards(self) -> None:
        now = datetime.datetime(2026, 11, 26, 0, 20, 0).timestamp()
        cur_h = int(now // 3600)

        # 1) Settled high-load degradation (ca***8 real pattern: 11-22 safe=4558, 11-24 deg=2551, post-deg recov=1546 over 11h)
        h22 = int(datetime.datetime(2026, 11, 22, 10, 0, 0).timestamp() // 3600)
        h24_deg = int(datetime.datetime(2026, 11, 24, 15, 0, 0).timestamp() // 3600)
        b_ca: Dict[int, Dict[str, Any]] = {
            h22: {"steps": 4558},
            h24_deg: {"steps": 177, "degraded": True, "degraded_24h_at": 2551, "dk": "t1_multi"},
        }
        for k in range(1, 12):
            b_ca[h24_deg + k] = {"steps": 140}  # 1540 steps over 11h (< 2551 -> must stay smoothly capped)
        res_ca = compute_adaptive_steps_v2(
            960, 1200, b_ca, now_ts=now, account_tier="ultra",
            live_24h_raw_steps=2200, live_5h_pct=90.0, live_weekly_pct=90.0,
        )
        self.assertEqual(res_ca.mode, "adaptive_up")
        self.assertEqual(res_ca.alert_limit, 2150)  # floor(2551 * 0.85 / 50) * 50 = 2150
        self.assertEqual(res_ca.warn_limit, 1720)
        self.assertFalse(res_ca.soft_over_limit)
        self.assertIn("2551", res_ca.note)
        self.assertIn("2150", res_ca.note)
        self.assertNotIn("初始建议值", res_ca.note)

        # 2) Unsettled high-load degradation with proven history (ly***8 pattern: 11-24 safe=4506, unsettled deg=1635)
        h24_safe = int(datetime.datetime(2026, 11, 24, 8, 0, 0).timestamp() // 3600)
        b_ly = {
            h24_safe: {"steps": 4506},
            cur_h - 1: {"steps": 139, "degraded": True, "degraded_24h_at": 1635, "dk": "t2"},
        }
        res_ly = compute_adaptive_steps_v2(960, 1200, b_ly, now_ts=now, account_tier="ultra")
        self.assertEqual(res_ly.mode, "adaptive_up")
        self.assertEqual(res_ly.alert_limit, 1350)  # floor(1635 * 0.85 / 50) * 50 = 1350
        self.assertIn("1635", res_ly.note)
        self.assertIn("1350", res_ly.note)

        # 3) Near-baseline high-load degradation (1300 -> 1300*0.85=1105 -> floor baseline 1200) keeps honest note!
        b_near = {
            h24_safe: {"steps": 3500},
            cur_h - 1: {"steps": 100, "degraded": True, "degraded_24h_at": 1300, "dk": "t2"},
        }
        res_near = compute_adaptive_steps_v2(960, 1200, b_near, now_ts=now, account_tier="ultra")
        self.assertEqual(res_near.mode, "default")
        self.assertEqual(res_near.alert_limit, 1200)
        self.assertIn("1300", res_near.note)
        self.assertNotIn("初始建议值", res_near.note)

        # 4) High-load degradation NEVER inflates an account above its own earned capacity (own_est=1500 -> ceil 1800 < 2150)
        b_small_own = {
            h22: {"steps": 1500},
            h24_deg: {"steps": 100, "degraded": True, "degraded_24h_at": 2551, "dk": "t2"},
        }
        res_small = compute_adaptive_steps_v2(960, 1200, b_small_own, now_ts=now, account_tier="ultra")
        self.assertEqual(res_small.mode, "adaptive_up")
        self.assertEqual(res_small.alert_limit, 1800)

        # 5) Brand-new Day 1 with ONLY unsettled high-load degradation strictly stays at 1200 (zero chasing!)
        b_day1 = {
            cur_h - 2: {"steps": 500},
            cur_h - 1: {"steps": 200, "degraded": True, "degraded_24h_at": 2551, "dk": "t2"},
        }
        res_day1 = compute_adaptive_steps_v2(960, 1200, b_day1, now_ts=now, account_tier="ultra")
        self.assertEqual((res_day1.mode, res_day1.alert_limit), ("default", 1200))
        self.assertIn("2551", res_day1.note)

        # 6) Sustained post-degradation recovery (>=3h & >=2551 steps) lifts cap under T0, but live T2 locks it!
        b_recov = dict(b_ca)
        for k in range(1, 7):
            b_recov[h24_deg + k] = {"steps": 450}  # 2700 steps over 6h >= 2551
        res_lifted = compute_adaptive_steps_v2(960, 1200, b_recov, now_ts=now, account_tier="ultra", live_grade="T0")
        self.assertEqual(res_lifted.mode, "adaptive_up")
        self.assertGreaterEqual(res_lifted.alert_limit, 4200)
        res_t2_lock = compute_adaptive_steps_v2(960, 1200, b_recov, now_ts=now, account_tier="ultra", live_grade="T2")
        self.assertEqual(res_t2_lock.alert_limit, 2150)

        # 7) Settled high-load degradation older than 7 days expires naturally when recent days are clean
        h_9d_deg = cur_h - 9 * 24
        h_2d_safe = cur_h - 2 * 24
        b_aged = {
            h_9d_deg: {"steps": 1500, "degraded": True, "degraded_24h_at": 1600, "dk": "t2"},
            h_2d_safe: {"steps": 1400},  # < 1600 so didn't surpass via _evaluate_account_history, relies on >7d aging
        }
        res_aged = compute_adaptive_steps_v2(960, 1200, b_aged, now_ts=now, account_tier="ultra")
        self.assertEqual(res_aged.mode, "adaptive_up")
        self.assertEqual(res_aged.alert_limit, 1700)  # ceil(1400 * 1.15 / 100) * 100 = 1700

        # 8) Bridge MUST NOT pierce high_deg_cap when post-deg hours >= 6h but steps < active_deg_steps
        b_bridge_trap: Dict[int, Dict[str, Any]] = {
            h22: {"steps": 4558},
            cur_h - 10: {"steps": 100, "degraded": True, "degraded_24h_at": 2551, "dk": "t2"},
        }
        for k in range(1, 9):
            b_bridge_trap[cur_h - 10 + k] = {"steps": 300}  # 2400 steps over 8h (< 2551)
        res_bt = compute_adaptive_steps_v2(960, 1200, b_bridge_trap, now_ts=now, account_tier="ultra")
        self.assertEqual(res_bt.alert_limit, 2150)

        # 9) Free tier high-load degradation stays monotonic at 600 cap & omits unreachable relax promise
        res_free = compute_adaptive_steps_v2(
            960, 1200, {h24_deg: {"steps": 700, "degraded": True, "degraded_24h_at": 700, "dk": "t2"}},
            now_ts=now, account_tier="free",
        )
        self.assertEqual(res_free.alert_limit, 600)
        self.assertNotIn("会自动放宽", res_free.note)

        # 10) Dual-review P1-1: Expired >7d high-load degradation MUST NOT resurrect when today has a new degradation
        h_9d_4k = cur_h - 9 * 24
        b_p11_low = {
            h_9d_4k: {"steps": 2000, "degraded": True, "degraded_24h_at": 4000, "dk": "t2"},
            cur_h - 5: {"steps": 400, "degraded": True, "degraded_24h_at": 800, "dk": "t2"},
            cur_h - 4: {"steps": 600},
            cur_h - 3: {"steps": 600},
            cur_h - 2: {"steps": 600},  # 1800 >= 800*2=1600 -> must lift today's low deg, NOT be held at 8000!
        }
        res_p11_low = compute_adaptive_steps_v2(960, 1200, b_p11_low, now_ts=now, account_tier="ultra")
        self.assertEqual((res_p11_low.mode, res_p11_low.alert_limit), ("default", 1200))

        b_p11_high = {
            h_9d_4k: {"steps": 1500, "degraded": True, "degraded_24h_at": 1500, "dk": "t2"},
            h_2d_safe: {"steps": 3000},
            cur_h - 1: {"steps": 100, "degraded": True, "degraded_24h_at": 2800, "dk": "t2"},
        }
        res_p11_high = compute_adaptive_steps_v2(960, 1200, b_p11_high, now_ts=now, account_tier="ultra")
        self.assertEqual(res_p11_high.alert_limit, 2350)  # floor(2800*0.85/50)*50=2350, NOT 1250 from expired 1500!
        self.assertIn("2800", res_p11_high.note)

        # 11) Dual-review P1-2: Strict monotonicity across own_est <= 1200 vs 1201 & zero Day-6->Day-8 drop
        res_own_1200 = compute_adaptive_steps_v2(
            960, 1200, {h22: {"steps": 1200}, h24_deg: {"steps": 100, "degraded": True, "degraded_24h_at": 2551, "dk": "t2"}},
            now_ts=now, account_tier="ultra",
        )
        res_own_1201 = compute_adaptive_steps_v2(
            960, 1200, {h22: {"steps": 1201}, h24_deg: {"steps": 100, "degraded": True, "degraded_24h_at": 2551, "dk": "t2"}},
            now_ts=now, account_tier="ultra",
        )
        self.assertLessEqual(res_own_1200.alert_limit, res_own_1201.alert_limit)
        self.assertEqual(res_own_1200.alert_limit, 1200)
        self.assertEqual(res_own_1201.alert_limit, 1400)

        # 12) Dual-review P1-4: Unsettled degradation with degraded_24h_at=None falls back to 24h window steps & blocks soft_over_limit
        b_none_at = {
            h22: {"steps": 4558},
            cur_h - 3: {"steps": 900},
            cur_h - 2: {"steps": 900},
            cur_h - 1: {"steps": 100, "degraded": True, "degraded_24h_at": None, "dk": "t1_multi"},
        }
        res_none_at = compute_adaptive_steps_v2(
            960, 1200, b_none_at, now_ts=now, account_tier="ultra",
            live_24h_raw_steps=2000, live_5h_pct=90.0, live_weekly_pct=90.0,
        )
        self.assertTrue(res_none_at.high_deg_capped)
        self.assertEqual(res_none_at.alert_limit, 1600)  # floor(1900*0.85/50)*50=1600
        self.assertFalse(res_none_at.soft_over_limit)

        # 13) Dual-review P2-1/P2-2: Low-deg + unsettled high-deg prefers low tightening; high_deg_capped + low_quota preserves context
        b_mix_deg = {
            h22: {"steps": 600, "degraded": True, "degraded_24h_at": 600, "dk": "t2"},
            cur_h - 1: {"steps": 100, "degraded": True, "degraded_24h_at": 2000, "dk": "t2"},
        }
        res_mix = compute_adaptive_steps_v2(960, 1200, b_mix_deg, now_ts=now, account_tier="ultra")
        self.assertEqual(res_mix.mode, "adaptive_down")
        self.assertEqual(res_mix.alert_limit, 600)

        res_hq_lowq = compute_adaptive_steps_v2(
            960, 1200, b_ca, now_ts=now, account_tier="ultra",
            live_24h_raw_steps=1400, live_5h_pct=10.0, burn_pct_per_hour=10.0, last_1h_steps=150,
        )
        self.assertTrue(res_hq_lowq.low_quota_tightened)
        self.assertTrue(res_hq_lowq.high_deg_capped)
        self.assertTrue(any("2551" in line for line in res_hq_lowq.detail_lines))

        # 14) Plain-Chinese jargon check across all new notes
        for r_chk in (res_ca, res_ly, res_near, res_day1, res_free, res_none_at, res_hq_lowq):
            _assert_plain_chinese(self, r_chk)

        # 15) Dual-review P1-3: UI tooltip suppresses false "沉淀预告" when high_deg_capped=True
        from core.log_monitor import AccountQuotaInfo, MonitorSnapshot
        from ui.capsule_app import AntigravityCapsuleApp

        snap_hd = MonitorSnapshot(
            generated_at=now, level="green", grade="T0", headline="T0", sub_headline="ok",
            active_model="Neon XHigh", primary_hit_rate=98.0, primary_hit_classification="hit",
            primary_tps=120.0, primary_ttft=4.0, primary_speed_status="ok",
            min_freshness_sec=180, min_freshness_conv="a1b2c3", min_freshness_ctx=120000,
            min_freshness_invalid_reason="", today_steps=2300, daily_steps_warn=1720, daily_steps_alert=2150,
            active_conversations=[], large_freshness_list=[], recent_503=0, today_503=0,
            recent_timeout=0, today_timeout=0, recent_403=0, today_403=0, recent_retry=0, today_retry=0,
            recent_events=[], wasted_cold_tokens_total=0, max_consec_miss=0,
            account=AccountQuotaInfo(short_label="ca***8", tier="g1-ultra-tier", online=True, observed_at=now),
            sliding_window_min=30, freshness_window_sec=210, resolved_account="ca***8",
            daily_steps_limit_mode="adaptive_up", daily_steps_limit_note=res_ca.note,
            rolling_24h_steps=2300, current_24h_has_degradation=False,
            steps_detail_lines=res_ca.detail_lines, steps_high_deg_capped=True, steps_high_deg_ref_steps=2551,
        )
        dummy_app = object.__new__(AntigravityCapsuleApp)
        dummy_app.config = {"daily_steps_warn": 960}
        tip_txt = AntigravityCapsuleApp._build_steps_tooltip_text(dummy_app, snap_hd)
        self.assertNotIn("沉淀预告", tip_txt)
        self.assertIn("2551", tip_txt)


if __name__ == "__main__":
    unittest.main()
