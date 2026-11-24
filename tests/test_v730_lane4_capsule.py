"""v7.3.0 Lane 4 unit tests: main capsule HUD zero-jargon upgrade, 18-HWND isolation,
lazy hover tooltips, log-cell timeline hover/copy, 24/N truncation, patch-save integrity,
settings hot-reload, async detail launch, and tray 'open_settings' menu item.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_store import ConfigStore
from core.log_monitor import (
    AccountQuotaInfo,
    AntigravityMonitorEngine,
    ConversationSummary,
    LogEvent,
    ModelPerfStats,
    MonitorSnapshot,
    QuotaPoolInfo,
    StepRecord,
)
from ui.capsule_app import AntigravityCapsuleApp, format_freshness_status
from ui.themes import THEMES


def _make_isolated_engine(root_d: Path) -> AntigravityMonitorEngine:
    return AntigravityMonitorEngine(
        paths={
            "brain_dir": root_d / "brain",
            "conversations_dir": root_d / "conversations",
            "daemon_dir": root_d / "daemon",
            "ls_log_path": root_d / "ls.log",
            "cockpit_quota_db": root_d / "ck.sqlite",
        },
        read_only=True,
    )


def _count_win32_child_windows(win: tk.Misc) -> int:
    if os.name != "nt":
        return 18
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetParent.argtypes = [wintypes.HWND]
    u32.GetParent.restype = wintypes.HWND
    hwnd = int(u32.GetParent(wintypes.HWND(win.winfo_id())) or 0) or int(win.winfo_id())
    children = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(child_hwnd: int, _lparam: int) -> bool:
        children.append(int(child_hwnd))
        return True

    u32.EnumChildWindows(wintypes.HWND(hwnd), _cb, 0)
    return len(children)


def _count_tk_non_menu_descendants(root: tk.Misc, skip: object = None) -> int:
    total = 0
    stack = list(root.winfo_children())
    while stack:
        w = stack.pop()
        if skip is not None and w is skip:
            continue
        if isinstance(w, (tk.Menu, tk.Toplevel)):
            continue
        total += 1
        stack.extend(w.winfo_children())
    return total


def _make_base_snapshot(now: float) -> MonitorSnapshot:
    st_ok = StepRecord(1, now - 10, "gemini-4-neon-xhigh", 2000, 148000, 350, 150000, 98.7, "hit", "cached")
    c_main = ConversationSummary(
        conversation_id="conv0001-0000-0000-0000-000000000001",
        short_id="conv01",
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        context_tokens=150000,
        max_context_tokens=512000,
        latest_hit_rate=98.7,
        latest_cached_in=148000,
        latest_uncached_in=2000,
        latest_timestamp=now - 10,
        recent_steps=[st_ok],
        consec_miss=0,
        consec_miss_start_step=0,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=160,
        ttft_sec=12.4,
        tps=142.0,
        speed_status="ok",
        ttft_avg_sec=11.0,
        ttft_max_sec=18.0,
        title="主线代码重构",
    )
    acc = AccountQuotaInfo(
        short_label="zm***1",
        tier="g1-ultra-tier",
        five_hour_pct=71.0,
        weekly_pct=92.0,
        quota_family="gemini",
        online=True,
        observed_at=now,
        pools={
            "gemini": QuotaPoolInfo(name="gemini", five_hour_pct=71.0, weekly_pct=92.0, model_count=3),
            "3p": QuotaPoolInfo(name="3p", five_hour_pct=100.0, weekly_pct=100.0, model_count=2),
        },
    )
    return MonitorSnapshot(
        generated_at=now,
        level="green",
        grade="T0",
        headline="T0 健康 · Neon XHigh",
        sub_headline="缓存命中稳定，生成速度正常",
        active_model="Neon XHigh",
        primary_hit_rate=98.7,
        primary_hit_classification="hit",
        primary_tps=142.0,
        primary_ttft=12.4,
        primary_speed_status="ok",
        min_freshness_sec=160,
        min_freshness_conv="conv01",
        min_freshness_ctx=150000,
        min_freshness_invalid_reason="",
        min_freshness_title="主线代码重构",
        today_steps=320,
        daily_steps_warn=960,
        daily_steps_alert=1200,
        active_conversations=[c_main],
        large_freshness_list=[c_main],
        recent_503=0,
        today_503=0,
        recent_timeout=0,
        today_timeout=0,
        recent_403=0,
        today_403=0,
        recent_retry=0,
        today_retry=0,
        recent_events=[],
        wasted_cold_tokens_total=0,
        max_consec_miss=0,
        account=acc,
        sliding_window_min=30,
        freshness_window_sec=210,
        resolved_account="zm***1",
        steps_scope_label="zm***1",
        log_scope_label="zm***1",
        rolling_24h_steps=320,
        account_24h_steps=320,
        total_24h_steps=320,
        main_24h_steps=260,
        subagent_24h_steps=60,
        available_accounts=["zm***1", "be***2"],
    )


class TestV730Lane4CapsuleUI(unittest.TestCase):
    def test_root_strict_18_win32_child_windows_and_lazy_bar_tooltips(self) -> None:
        """Redline: self.root stays strictly 18 Win32 child HWNDs (0 new widgets) and bar tooltips are lazy on <Enter>."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.update()
                self.assertEqual(_count_win32_child_windows(app.root), 18)
                self.assertEqual(_count_tk_non_menu_descendants(app.root), 21)

                # Verify render_snapshot does NOT eagerly build the 6 bar tooltip strings on every 2.5s tick
                build_calls = []
                orig_builders = {
                    "status": app._build_status_tooltip_text,
                    "cache": app._build_cache_tooltip_text,
                    "acc": app._build_account_tooltip_text,
                    "steps": app._build_steps_tooltip_text,
                    "speed": app._build_model_stats_tooltip_text,
                    "fresh": app._build_freshness_tooltip_text,
                }

                def _wrap(name: str, fn):
                    def _inner(*args, **kwargs):
                        build_calls.append(name)
                        return fn(*args, **kwargs)
                    return _inner

                app._build_status_tooltip_text = _wrap("status", orig_builders["status"])  # type: ignore[method-assign]
                app._build_cache_tooltip_text = _wrap("cache", orig_builders["cache"])  # type: ignore[method-assign]
                app._build_account_tooltip_text = _wrap("acc", orig_builders["acc"])  # type: ignore[method-assign]
                app._build_steps_tooltip_text = _wrap("steps", orig_builders["steps"])  # type: ignore[method-assign]
                app._build_model_stats_tooltip_text = _wrap("speed", orig_builders["speed"])  # type: ignore[method-assign]
                app._build_freshness_tooltip_text = _wrap("fresh", orig_builders["fresh"])  # type: ignore[method-assign]

                snap = _make_base_snapshot(now)
                app.render_snapshot(snap)
                app.set_expanded(True)
                app.render_snapshot(snap)
                app.root.update()

                self.assertEqual(build_calls, [], "render_snapshot must not eagerly build hover tooltip strings")
                self.assertEqual(_count_win32_child_windows(app.root), 18)
                self.assertEqual(_count_tk_non_menu_descendants(app.root, skip=app._details_win), 21)

                # Trigger lazy tooltip generation on <Enter> and confirm _on_drag_start hides active tooltip
                fake_ev = SimpleNamespace(x_root=160, y_root=160)
                for fn_name in (
                    "_show_status_tooltip",
                    "_show_cache_tooltip",
                    "_show_speed_tooltip",
                    "_show_freshness_tooltip",
                    "_show_acc_tooltip",
                    "_show_steps_tooltip",
                ):
                    getattr(app, fn_name)(fake_ev)
                    self.assertIsNotNone(app._tooltip_win, f"{fn_name} should open tooltip window")
                self.assertEqual(set(build_calls), {"status", "cache", "acc", "steps", "speed", "fresh"})

                app._on_drag_start(SimpleNamespace(x_root=100, y_root=100))
                self.assertIsNone(app._tooltip_win, "_on_drag_start must hide active tooltip before drag")
                app._dragger.finish_or_cancel(commit=False)

                app.set_expanded(False)
                app.root.update()
                self.assertEqual(_count_win32_child_windows(app.root), 18)
                self.assertEqual(_count_tk_non_menu_descendants(app.root, skip=app._details_win), 21)
            finally:
                app.shutdown()

    def test_collapsed_bar_tier_pool_exhaust_rescue_and_tooltip_truncation(self) -> None:
        """Collapsed bar shows [U]/[P]/[F] + G:/3P:, orange on exhaust risk, rescue freshness, and preserves '保鲜 —'."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                t = app.theme
                snap = _make_base_snapshot(now)
                app.render_snapshot(snap)
                self.assertEqual(app._lbl_bar_acc.cget("text"), "zm***1[U] G:71%/周92%")
                self.assertEqual(app._lbl_bar_acc.cget("fg"), t["text_secondary"])
                self.assertEqual(app._lbl_bar_fresh.cget("text"), "保鲜 剩 2:40")
                self.assertEqual(app._lbl_bar_fresh.cget("fg"), t["green"])

                # 3P Pro + will_exhaust_before_reset -> orange + [P] + 3P:
                acc_p = AccountQuotaInfo(
                    short_label="ga***3",
                    tier="g1-pro-tier",
                    five_hour_pct=30.0,
                    weekly_pct=85.0,
                    quota_family="3p",
                    online=True,
                    observed_at=now,
                )
                setattr(acc_p, "will_exhaust_before_reset", True)
                snap_p = replace(snap, account=acc_p, min_freshness_sec=-15)
                app.render_snapshot(snap_p)
                self.assertEqual(app._lbl_bar_acc.cget("text"), "ga***3[P] 3P:30%/周85%")
                self.assertEqual(app._lbl_bar_acc.cget("fg"), t["orange"])
                self.assertEqual(app._lbl_bar_fresh.cget("text"), "保鲜 抢救 1:15")
                self.assertEqual(app._lbl_bar_fresh.cget("fg"), t["red"])

                # Free tier + expired (-120s)
                acc_f = AccountQuotaInfo(
                    short_label="fr***1",
                    tier="g1-free-tier",
                    five_hour_pct=50.0,
                    weekly_pct=None,
                    quota_family="gemini",
                    online=True,
                    observed_at=now,
                )
                snap_f = replace(snap, account=acc_f, min_freshness_sec=-120)
                app.render_snapshot(snap_f)
                self.assertEqual(app._lbl_bar_acc.cget("text"), "fr***1[F] G:50%")
                self.assertEqual(app._lbl_bar_fresh.cget("text"), "保鲜 已冷却")
                self.assertEqual(app._lbl_bar_fresh.cget("fg"), t["orange"])

                # plan_status MUST NOT spoof tier when userTier.id (tier) is empty
                acc_unknown = AccountQuotaInfo(
                    short_label="no***0",
                    tier="",
                    plan_status="Pro (TEAMS_TIER_PRO)",
                    five_hour_pct=80.0,
                    weekly_pct=90.0,
                    quota_family="gemini",
                    online=True,
                    observed_at=now,
                )
                snap_unk = replace(snap, account=acc_unknown, min_freshness_sec=None, is_custom_account_view=True, resolved_account="be***2")
                app.render_snapshot(snap_unk)
                self.assertEqual(app._lbl_bar_fresh.cget("text"), "保鲜 —")
                self.assertEqual(app._resolve_account_tier(snap_unk), ("", ""))

                # Tooltip > 18 lines gets truncated with hint & bounded wraplength
                long_txt = "\n".join(f"line-{i}" for i in range(1, 26))
                app._show_text_tooltip(long_txt, SimpleNamespace(x_root=100, y_root=100))
                self.assertIsNotNone(app._tooltip_win)
                inner = app._tooltip_win.winfo_children()[0]
                lbl = inner.winfo_children()[0]
                shown = str(lbl.cget("text"))
                self.assertIn("line-18", shown)
                self.assertNotIn("line-19", shown)
                self.assertIn("内容较长已折叠", shown)
                wrap_val = int(lbl.cget("wraplength"))
                self.assertGreaterEqual(wrap_val, 320)
                self.assertLessEqual(wrap_val, 1160)
                app._hide_conv_tooltip()

                # Tabular per-step tooltip (contains '|') gets wider wrap bound on high-DPI screens
                tbl_txt = "最新 11-24 07:33:38 | 355.5K (+267) | 缓存 99.3% | 首字 4.7s | 95t/s [命中]"
                with mock.patch("ui.capsule_app._get_virtual_screen_bounds", return_value=(0, 0, 2560, 1600)):
                    app._show_text_tooltip(tbl_txt, SimpleNamespace(x_root=100, y_root=100))
                    lbl_tbl = app._tooltip_win.winfo_children()[0].winfo_children()[0]
                    self.assertGreaterEqual(int(lbl_tbl.cget("wraplength")), 900)
                    app._hide_conv_tooltip()
            finally:
                app.shutdown()

    def test_expanded_panel_zero_jargon_dual_pools_history_view_and_anchors(self) -> None:
        """Expanded panel shows tier badge, settings btn, dual pools, rescue freshness, plain speed labels, and preserves legacy anchors."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                app.set_expanded(True)
                snap = _make_base_snapshot(now)
                gem_pool = snap.account.pools["gemini"]
                setattr(gem_pool, "five_hour_reset_in_sec", 7800)
                setattr(gem_pool, "will_exhaust_before_reset", True)
                setattr(gem_pool, "burn_pct_per_hour", 32.0)
                setattr(gem_pool, "eta_exhaust_sec", 5400)
                setattr(gem_pool, "weekly_pace_ratio", 2.1)
                setattr(snap.account, "five_hour_reset_in_sec", 7800)
                setattr(snap.account, "will_exhaust_before_reset", True)
                setattr(snap.account, "weekly_pace_ratio", 2.1)

                ms = ModelPerfStats(
                    model="Neon XHigh",
                    step_count=8,
                    hit_count=8,
                    abnormal_miss_count=0,
                    ttft_avg_sec=20.0,
                    ttft_max_sec=35.0,
                    latest_ttft_sec=18.0,
                    tps_avg=140.0,
                    latest_timestamp=now - 5,
                    main_large_ctx_calibrated=True,
                )
                snap_rich = replace(
                    snap,
                    model_stats=[ms],
                    min_freshness_sec=-20,
                    daily_steps_limit_mode="adaptive_up",
                    daily_steps_limit_note="按近期稳跑记录自动放宽",
                    account_hist_safe_peak=1400,
                    rolling_24h_steps=1650,
                    daily_steps_alert=1600,
                    daily_steps_warn=1280,
                )
                setattr(
                    snap_rich,
                    "steps_detail_lines",
                    (
                        "按近期稳跑记录（约 1400 步）自动放宽，越近的日子权重越高",
                        "按当前消耗速度，5 小时余量预计还可跑约 220 步",
                    ),
                )
                app.render_snapshot(snap_rich)
                app.root.update()

                # Tier badge & settings button & dual pools
                self.assertEqual(app._lbl_tier_badge.cget("text"), "Ultra")
                self.assertTrue(bool(app._lbl_tier_badge.winfo_manager()))
                self.assertEqual(app._lbl_settings_btn.cget("text"), "⚙ 设置")
                self.assertIn("Gemini 5h 71%", app._lbl_acc_5h.cget("text"))
                self.assertIn("⚡耗快", app._lbl_acc_5h.cget("text"))
                self.assertIn("周耗偏快", app._lbl_acc_wk.cget("text"))
                self.assertTrue(bool(app._head_row_pools.winfo_manager()))
                self.assertIn("Claude/GPT", app._lbl_pool_secondary.cget("text"))

                # Plain-Chinese speed labels & rescue freshness card
                self.assertEqual(app._lbl_speed_badge.cget("text"), "速度正常")
                self.assertEqual(app._lbl_fresh_big.cget("text"), "临界抢救 剩 1:10")
                self.assertEqual(app._lbl_fresh_big.cget("fg"), app.theme["red"])

                # Switch to slow -> 响应偏慢 (zero old jargon)
                c_slow = replace(snap_rich.active_conversations[0], speed_status="slow", tps=22.0)
                snap_slow = replace(snap_rich, primary_speed_status="slow", primary_tps=22.0, active_conversations=[c_slow])
                app.render_snapshot(snap_slow)
                self.assertEqual(app._lbl_speed_badge.cget("text"), "响应偏慢")

                # Steps title & tooltip anchors + zero math jargon
                self.assertIn("近24h请求步数保护线", app._lbl_steps_title.cget("text"))
                self.assertIn("自适应", app._lbl_steps_title.cget("text"))
                steps_tip = app._steps_hover_text
                self.assertIn("按近期稳跑记录", steps_tip)
                self.assertIn("5 小时余量预计还可跑约 220 步", steps_tip)
                self.assertIn("沉淀预告", steps_tip)
                for jargon in ("EWMA", "Shrinkage", "Beta", "uncached_M", "effective_quota_load"):
                    self.assertNotIn(jargon, steps_tip)
                    self.assertNotIn(jargon, app._acc_hover_text)
                    self.assertNotIn(jargon, app._speed_hover_text)
                self.assertIn("[大窗校准]", app._speed_hover_text)

                # Historical custom account view keeps online account softly visible + full hover
                snap_hist = replace(
                    snap_rich,
                    is_custom_account_view=True,
                    resolved_account="be***2",
                    history_range="7d",
                    history_range_label="近 7 天",
                )
                app.render_snapshot(snap_hist)
                self.assertIn("在线 zm***1[U]", app._lbl_acc_5h.cget("text"))
                self.assertIn("正回看历史", app._lbl_acc_5h.cget("text"))
                self.assertIn("北京时间", app._acc_hover_text)
                self.assertIn("Claude / GPT 池", app._acc_hover_text)
            finally:
                app.shutdown()

    def test_conv_list_truncation_24_of_n_and_empty_list_zero_scrollbar(self) -> None:
        """Over 24 conversations shows '已显示最新 24 / N 条对话'; <=24 or empty removes it cleanly with zero scrollbar."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                app.set_expanded(True)
                base = _make_base_snapshot(now)
                st_ok = base.active_conversations[0].recent_steps[0]
                convs_30 = [
                    ConversationSummary(
                        conversation_id=f"c{i:05d}00-0000",
                        short_id=f"c{i:05d}",
                        model="Neon XHigh",
                        model_raw="gemini-4-neon-xhigh",
                        context_tokens=90000,
                        max_context_tokens=512000,
                        latest_hit_rate=98.0,
                        latest_cached_in=88000,
                        latest_uncached_in=2000,
                        latest_timestamp=now - i * 30,
                        recent_steps=[st_ok],
                        consec_miss=0,
                        consec_miss_start_step=0,
                        single_miss=False,
                        wasted_cold_tokens=0,
                        freshness_remaining_sec=None,
                        title=f"任务对话{i}",
                    )
                    for i in range(30)
                ]
                snap_many = replace(base, active_conversations=convs_30)
                setattr(snap_many, "total_matched_main", 38)
                setattr(snap_many, "total_matched_conversations", 38)
                app.render_snapshot(snap_many)
                app.root.update()
                self.assertTrue(bool(app._lbl_convs_trunc.winfo_manager()))
                self.assertIn("已显示最新 24 / 38 条对话", app._lbl_convs_trunc.cget("text"))
                self.assertTrue(len(app._conv_scrollbar.find_all()) > 0)

                # Transition directly to empty list -> _lbl_convs_trunc grid_removed & ZERO scrollbar items
                snap_empty = replace(base, active_conversations=[], large_freshness_list=[], min_freshness_sec=None)
                setattr(snap_empty, "total_matched_main", 0)
                setattr(snap_empty, "total_matched_conversations", 0)
                app.render_snapshot(snap_empty)
                app.root.update()
                self.assertEqual(app._lbl_convs_trunc.winfo_manager(), "")
                self.assertTrue(bool(app._lbl_convs_empty.winfo_manager()))
                self.assertEqual(app._conv_scrollbar.find_all(), ())
                self.assertEqual(app._conv_viewport.yview(), (0.0, 1.0))
            finally:
                app.shutdown()

    def test_module4_log_cells_plain_titles_hover_timeline_and_click_copy(self) -> None:
        """Module 4 uses plain titles, builds categorized hover timelines (newest first), and click-copies via _temp_banner_until."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                app.set_expanded(True)
                base = _make_base_snapshot(now)
                ev_t1 = LogEvent(
                    timestamp=now - 120,
                    category="timeout",
                    summary="流请求超时 (10m)",
                    event_key="t1",
                    model="Carbon High",
                    is_heavy_media_timeout=True,
                    culprit_cid=base.active_conversations[0].conversation_id,
                )
                ev_t2 = LogEvent(
                    timestamp=now - 30,
                    category="timeout",
                    summary="请求等待超时 (2m)",
                    event_key="t2",
                    model="Neon XHigh",
                    is_heavy_media_timeout=False,
                    culprit_cid="",
                )
                snap = replace(
                    base,
                    recent_timeout=2,
                    today_timeout=2,
                    recent_events=[ev_t1, ev_t2],
                    proxy_outage_note="代理线路疑似断流 (06:10 60s内 4次断连)",
                )
                setattr(snap, "recent_events_by_cat", {"timeout": [ev_t1, ev_t2], "503": [], "403": [], "retry": []})
                app.render_snapshot(snap)
                app.root.update()

                self.assertEqual(app._log_cells["503"]["title"].cget("text"), "503 繁忙")
                self.assertEqual(app._log_cells["timeout"]["title"].cget("text"), "请求超时")
                self.assertEqual(app._log_cells["403"]["title"].cget("text"), "403 权限")
                self.assertEqual(app._log_cells["retry"]["title"].cget("text"), "网络重试")

                tip_timeout = app._build_log_cell_tooltip_text("timeout", snap)
                self.assertLess(tip_timeout.index("Neon XHigh"), tip_timeout.index("Carbon High"))
                self.assertIn("已识别为多图上传耗时，不计入限流", tip_timeout)
                self.assertIn("主线代码重构(conv01)", tip_timeout)

                tip_503 = app._build_log_cell_tooltip_text("503", snap)
                self.assertIn("当前窗口无此类异常，运行平稳", tip_503)

                tip_retry = app._build_log_cell_tooltip_text("retry", snap)
                self.assertIn("代理线路疑似断流", tip_retry)

                # Click-copy uses _temp_banner_until without piling up _after_ids
                after_cnt_before = len(app._after_ids)
                app._copy_log_cell_timeline("timeout")
                self.assertEqual(len(app._after_ids), after_cnt_before)
                self.assertEqual(app._lbl_banner.cget("text"), "已复制日志明细 ✓")
                self.assertEqual(app._lbl_banner.cget("fg"), app.theme["green"])
                self.assertGreater(app._temp_banner_until, time.monotonic())
                clip = app.root.clipboard_get()
                self.assertIn("请求超时明细", clip)
            finally:
                app.shutdown()

    def test_save_config_patch_preserves_external_json_edits(self) -> None:
        """Local UI config saves (drag/theme/topmost/filter/rows/update_mode) never clobber external JSON edits."""
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg_path = root_d / "config.json"
            cfg = ConfigStore(cfg_path)
            cfg.save(cfg.load())
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                # Simulate user externally hand-editing config.json while app is running
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                disk["poll_interval_ms"] = 7777
                disk["slow_tps_threshold"] = 55.0
                cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")

                # Perform multiple UI local saves
                app.set_theme("obsidian_dark")
                app.toggle_topmost()
                app.set_conv_filter("all")
                app._conv_list_rows = 8
                app._on_resize_end(SimpleNamespace())
                app._on_dragger_ended(moved=True)
                app.set_update_mode("notify")

                after_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(after_disk["poll_interval_ms"], 7777)
                self.assertEqual(after_disk["slow_tps_threshold"], 55.0)
                self.assertEqual(after_disk["theme"], "obsidian_dark")
                self.assertEqual(after_disk["conv_filter"], "all")
                self.assertEqual(after_disk["conv_list_rows"], 8)
                self.assertEqual(after_disk["update_mode"], "notify")
            finally:
                app.shutdown()

    def test_settings_hot_reload_alpha_preview_async_detail_and_tray_open_settings(self) -> None:
        """Settings hot-reload, alpha preview (+0.03 offset & cancel restore), async_load=True detail launch, and tray open_settings."""
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root_d = Path(td)
            cfg = ConfigStore(root_d / "config.json")
            eng = _make_isolated_engine(root_d)
            app = AntigravityCapsuleApp(PROJECT_ROOT, cfg, eng, enable_tray=False, start_minimized_override=False)
            try:
                app.root.withdraw()
                app.set_expanded(True)
                app.render_snapshot(_make_base_snapshot(now))
                app.root.update()

                # Alpha preview updates BOTH root and detached _details_win (+0.03 offset)
                app._preview_window_alpha(0.80)
                self.assertAlmostEqual(float(app.root.attributes("-alpha")), 0.80, places=2)
                self.assertAlmostEqual(float(app._details_win.attributes("-alpha")), 0.83, places=2)
                app._restore_window_alpha_from_config()
                self.assertAlmostEqual(float(app.root.attributes("-alpha")), 0.95, places=2)
                self.assertAlmostEqual(float(app._details_win.attributes("-alpha")), 0.98, places=2)

                # Mock subwindows & tray to verify full hot-reload fanout
                detail_calls = []
                slots_calls = []
                settings_calls = []
                tray_calls = []

                class _FakeDetailWin:
                    _closed = False

                    def apply_theme(self, theme) -> None:
                        detail_calls.append(("theme", theme["display_name"]))

                    def update_slow_tps_threshold(self, val: float) -> None:
                        detail_calls.append(("slow_tps", val))

                    def sync_from_snapshot(self, _snap) -> None:
                        pass

                    def check_drag_watchdog(self) -> None:
                        detail_calls.append(("watchdog", True))

                    def close(self) -> None:
                        self._closed = True
                        detail_calls.append(("close", True))

                class _FakeSlotsWin:
                    _closed = False

                    def apply_theme(self, theme) -> None:
                        slots_calls.append(("theme", theme["display_name"]))

                    def sync_auto_button(self) -> None:
                        slots_calls.append(("sync_auto", True))

                    def refresh(self) -> None:
                        slots_calls.append(("refresh", True))

                    def sync_from_summary(self, _inv) -> None:
                        pass

                    def check_drag_watchdog(self) -> None:
                        slots_calls.append(("watchdog", True))

                    def close(self) -> None:
                        self._closed = True
                        slots_calls.append(("close", True))

                class _FakeSettingsWin:
                    _closed = False

                    def apply_theme(self, theme) -> None:
                        settings_calls.append(("theme", theme["display_name"]))

                    def check_drag_watchdog(self) -> None:
                        settings_calls.append(("watchdog", True))

                    def close(self) -> None:
                        self._closed = True
                        settings_calls.append(("close", True))

                class _FakeTray:
                    def refresh_menu(self) -> None:
                        tray_calls.append("refresh_menu")

                    def update_status(self, _lvl: str, _tip: str) -> None:
                        pass

                    def stop(self) -> None:
                        tray_calls.append("stop")

                app._detail_win = _FakeDetailWin()  # type: ignore[assignment]
                app._slots_win = _FakeSlotsWin()  # type: ignore[assignment]
                app._settings_win = _FakeSettingsWin()  # type: ignore[assignment]
                app.tray = _FakeTray()  # type: ignore[assignment]

                app._force_poll_event.clear()
                res = app.apply_settings_patch(
                    {
                        "theme": "obsidian_dark",
                        "window_alpha": 0.84,
                        "topmost": False,
                        "notifications_enabled": False,
                        "start_minimized": True,
                        "freshness_window_sec": 240,
                        "slow_tps_threshold": 42.5,
                        "slot_auto_clean_enabled": False,
                        "update_mode": "notify",
                    }
                )
                self.assertEqual(res["theme"], "obsidian_dark")
                self.assertEqual(res["freshness_window_sec"], 240)
                self.assertEqual(res["idle_rewarm_sec"], 240)
                self.assertEqual(app.engine.config["freshness_window_sec"], 240)
                self.assertEqual(app.engine.config["idle_rewarm_sec"], 240)
                self.assertAlmostEqual(float(app.root.attributes("-alpha")), 0.84, places=2)
                self.assertAlmostEqual(float(app._details_win.attributes("-alpha")), 0.87, places=2)
                self.assertFalse(bool(app.root.attributes("-topmost")))
                self.assertFalse(bool(app._details_win.attributes("-topmost")))
                self.assertFalse(app._var_topmost.get())
                self.assertFalse(app._var_notifications.get())
                self.assertTrue(app._var_start_min.get())
                self.assertFalse(app._var_auto_clean.get())
                self.assertEqual(app._var_update_mode.get(), "notify")
                self.assertIn(("slow_tps", 42.5), detail_calls)
                self.assertIn(("theme", THEMES["obsidian_dark"]["display_name"]), detail_calls)
                self.assertIn(("theme", THEMES["obsidian_dark"]["display_name"]), slots_calls)
                self.assertIn(("theme", THEMES["obsidian_dark"]["display_name"]), settings_calls)
                self.assertIn(("sync_auto", True), slots_calls)
                self.assertIn(("refresh", True), slots_calls)
                self.assertIn("refresh_menu", tray_calls)
                self.assertTrue(app._force_poll_event.is_set())

                # _ui_drain_tick invokes settings window drag watchdog
                app._ui_drain_tick()
                self.assertIn(("watchdog", True), settings_calls)

                # Production open_conversation_detail passes async_load=True and default slow_tps_threshold=40.0
                app._detail_win = None
                app.config.pop("slow_tps_threshold", None)
                captured_kwargs = {}

                class _CaptureDetail:
                    _closed = False

                    def __init__(self, **kwargs) -> None:
                        captured_kwargs.update(kwargs)

                    def show_conversation(self, *_args, **_kwargs) -> None:
                        pass

                    def close(self) -> None:
                        self._closed = True

                with mock.patch("ui.capsule_app.ConversationDetailWindow", _CaptureDetail):
                    app.open_conversation_detail("conv0001-0000-0000-0000-000000000001")
                self.assertTrue(captured_kwargs.get("async_load"), "open_conversation_detail must pass async_load=True")
                self.assertEqual(captured_kwargs.get("slow_tps_threshold"), 40.0)

                # Verify open_settings_window wires all frozen callbacks & restores alpha on cancel-close
                app._settings_win = None
                captured_set_kwargs = {}

                class _FakeSettingsModal:
                    _closed = False

                    def __init__(self, **kwargs) -> None:
                        captured_set_kwargs.update(kwargs)

                    def show(self, place_near_parent: bool = True) -> None:
                        captured_set_kwargs["show_called"] = place_near_parent

                    def close(self) -> None:
                        self._closed = True
                        cb = captured_set_kwargs.get("on_closed")
                        if callable(cb):
                            cb()

                fake_mod = SimpleNamespace(MonitorSettingsWindow=_FakeSettingsModal)
                with mock.patch.dict(sys.modules, {"ui.settings_window": fake_mod}):
                    app.open_settings_window()
                self.assertIs(captured_set_kwargs.get("parent"), app.root)
                self.assertEqual(captured_set_kwargs.get("on_save_patch"), app.apply_settings_patch)
                self.assertTrue(callable(captured_set_kwargs.get("on_preview_alpha")))
                # Simulate user dragging alpha slider to 0.75 then cancelling without saving
                captured_set_kwargs["on_preview_alpha"](0.75)
                self.assertAlmostEqual(float(app.root.attributes("-alpha")), 0.75, places=2)
                self.assertAlmostEqual(float(app._details_win.attributes("-alpha")), 0.78, places=2)
                app._settings_win.close()
                self.assertIsNone(app._settings_win)
                self.assertAlmostEqual(float(app.root.attributes("-alpha")), 0.84, places=2)
                self.assertAlmostEqual(float(app._details_win.attributes("-alpha")), 0.87, places=2)
                # Re-attach fake settings win so finally verifies shutdown().close()
                app._settings_win = _FakeSettingsWin()  # type: ignore[assignment]

                # Verify tray_manager exposes '参数设置' wired to callback key 'open_settings'
                fired = []
                try:
                    from ui.tray_manager import TrayManager
                    tm = TrayManager(
                        base_icon_png=PROJECT_ROOT / "assets" / "app-icon.png",
                        dispatch_to_ui=lambda fn: fn(),
                        callbacks={"open_settings": lambda: fired.append("open_settings")},
                        state_getters={
                            "theme": lambda: "midnight_blue",
                            "topmost": lambda: True,
                            "startup": lambda: False,
                            "start_minimized": lambda: False,
                            "notifications": lambda: True,
                        },
                    )
                    menu = tm._build_menu()
                    settings_items = [it for it in menu.items if getattr(it, "text", "") == "参数设置"]
                    self.assertEqual(len(settings_items), 1)
                    settings_items[0](None)
                except ModuleNotFoundError:
                    # Minimal Py3.9 env without pystray/PIL: stub pystray to still exercise real TrayManager._build_menu()
                    import importlib

                    class _StubMenuItem:
                        def __init__(self, text, action=None, **_kw) -> None:
                            self.text = text
                            self.action = action

                        def __call__(self, icon) -> None:
                            if callable(self.action):
                                self.action(icon, self)

                    class _StubMenu:
                        SEPARATOR = object()

                        def __init__(self, *items) -> None:
                            self.items = list(items)

                    fake_pystray = SimpleNamespace(Menu=_StubMenu, MenuItem=_StubMenuItem, Icon=object)
                    fake_pil = SimpleNamespace(Image=SimpleNamespace(Image=object))
                    fake_ib = SimpleNamespace(build_tray_icon=lambda *_a, **_kw: None)
                    with mock.patch.dict(
                        sys.modules,
                        {
                            "pystray": fake_pystray,
                            "PIL": fake_pil,
                            "PIL.Image": fake_pil.Image,
                            "ui.icon_builder": fake_ib,
                        },
                    ):
                        sys.modules.pop("ui.tray_manager", None)
                        tm_mod = importlib.import_module("ui.tray_manager")
                        tm = tm_mod.TrayManager(
                            base_icon_png=PROJECT_ROOT / "assets" / "app-icon.png",
                            dispatch_to_ui=lambda fn: fn(),
                            callbacks={"open_settings": lambda: fired.append("open_settings")},
                            state_getters={
                                "theme": lambda: "midnight_blue",
                                "topmost": lambda: True,
                                "startup": lambda: False,
                                "start_minimized": lambda: False,
                                "notifications": lambda: True,
                            },
                        )
                        menu = tm._build_menu()
                        settings_items = [it for it in menu.items if getattr(it, "text", "") == "参数设置"]
                        self.assertEqual(len(settings_items), 1)
                        settings_items[0](None)
                self.assertEqual(fired, ["open_settings"])
            finally:
                app.shutdown()
                self.assertIn(("close", True), settings_calls)


if __name__ == "__main__":
    unittest.main()
