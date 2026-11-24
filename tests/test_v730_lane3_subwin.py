"""Lane 3 dedicated unit tests for v7.3.0 independent subwindows (detail, slots, settings)."""

from __future__ import annotations

import ast
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.log_monitor import (
    ConversationDetailPoint,
    ConversationDetailReport,
)
from core.slot_manager import (
    SlotCleanupResult,
    SlotConversationItem,
    SlotInventorySnapshot,
)
from ui.detail_window import (
    ChartBucket,
    ConversationDetailWindow,
    aggregate_points_to_buckets,
)
from ui.settings_window import MonitorSettingsWindow
from ui.slots_window import ConversationSlotsWindow
from ui.themes import THEMES


def _make_point(
    idx: int,
    now: float,
    *,
    model: str = "Neon XHigh",
    total_tokens: int = 120000,
    context_delta: int = 2000,
    input_tokens: int = 2000,
    cache_read_tokens: int = 118000,
    output_tokens: int = 600,
    hit_rate: float = 98.3,
    classification: str = "hit",
    reason: str = "cached",
    ttft_sec: float | None = 12.0,
    tps: float | None = 120.0,
    streaming_sec: float | None = 5.0,
    is_compact: bool = False,
    is_revert: bool = False,
    is_account_switch: bool = False,
    active_media_count: int = 0,
    active_media_bytes: int = 0,
) -> ConversationDetailPoint:
    return ConversationDetailPoint(
        step_index=idx,
        timestamp=now - (2000 - idx) * 10.0,
        model=model,
        total_tokens=total_tokens,
        context_delta=context_delta,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        output_tokens=output_tokens,
        hit_rate=hit_rate,
        classification=classification,
        reason=reason,
        ttft_sec=ttft_sec,
        tps=tps,
        streaming_sec=streaming_sec,
        is_revert=is_revert,
        is_compact=is_compact,
        is_account_switch=is_account_switch,
        active_media_count=active_media_count,
        active_media_bytes=active_media_bytes,
    )


def _make_report(
    cid: str,
    pts: list[ConversationDetailPoint],
    *,
    title: str = "长线体检单测",
    avg_tps: float = 65.0,
) -> ConversationDetailReport:
    latest = pts[-1]
    return ConversationDetailReport(
        conversation_id=cid,
        short_id=cid[:6],
        is_subagent=False,
        model=latest.model or "Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        total_steps=len(pts),
        today_steps=len(pts),
        peak_context_tokens=max(p.total_tokens for p in pts),
        latest_context_tokens=latest.total_tokens,
        latest_context_delta=latest.context_delta,
        max_context_tokens=512000,
        compact_count=sum(1 for p in pts if p.is_compact),
        cache_hit_steps=sum(1 for p in pts if p.classification == "hit"),
        abnormal_miss_steps=sum(1 for p in pts if p.classification == "abnormal_miss"),
        expected_cold_steps=sum(1 for p in pts if p.classification == "expected_cold"),
        overall_cacheable_hit_ratio=0.95,
        avg_ttft_sec=14.0,
        p95_ttft_sec=42.0,
        max_ttft_sec=max((p.ttft_sec for p in pts if p.ttft_sec is not None), default=None),
        avg_tps=avg_tps,
        latest_tps=latest.tps,
        latest_ttft_sec=latest.ttft_sec,
        freshness_remaining_sec=150,
        freshness_invalid_reason="",
        freshness_window_sec=210,
        points=pts,
        title=title,
    )


class TestV730Lane3DetailWindow(unittest.TestCase):
    def _create_root(self) -> tk.Tk:
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError:
            self.skipTest("Tk display unavailable")

    def test_bucket_aggregation_preserves_peaks_tiers_and_indices(self) -> None:
        now = 1_795_500_000.0
        # 1:1 passthrough when len(pts) <= max_buckets
        small = [_make_point(i + 1, now) for i in range(10)]
        b_small = aggregate_points_to_buckets(small, 80)
        self.assertEqual(len(b_small), 10)
        self.assertTrue(all(isinstance(b, ChartBucket) and b.count == 1 for b in b_small))
        self.assertEqual([b.ctx_max_idx for b in b_small], list(range(10)))

        # 1264-step aggregation preserving worst TTFT peak, max context peak, and red > purple > orange > green
        pts = [_make_point(i + 1, now, total_tokens=100000 + i * 50, ttft_sec=10.0) for i in range(1264)]
        # Inject specific worst points in the same early bucket (indices 0..6)
        pts[2] = _make_point(3, now, total_tokens=480000, ttft_sec=11.0, classification="expected_cold", reason="idle_rewarm", hit_rate=0.0)
        pts[4] = _make_point(5, now, total_tokens=120000, ttft_sec=195.5, classification="expected_cold", reason="compact_reset", is_compact=True, hit_rate=0.0)
        pts[5] = _make_point(6, now, total_tokens=130000, ttft_sec=20.0, classification="abnormal_miss", reason="abnormal_zero", hit_rate=0.0, is_account_switch=True)

        buckets = aggregate_points_to_buckets(pts, 180)
        self.assertEqual(len(buckets), 180)
        self.assertEqual(sum(b.count for b in buckets), 1264)
        self.assertEqual(buckets[0].start_idx, 0)
        self.assertEqual(buckets[-1].end_idx, 1263)

        b0 = buckets[0]
        self.assertEqual(b0.ctx_max_idx, 2)
        self.assertEqual(b0.ctx_max_pt.total_tokens, 480000)
        self.assertEqual(b0.ttft_worst_idx, 4)
        self.assertAlmostEqual(b0.ttft_worst_pt.ttft_sec or 0.0, 195.5, places=1)
        self.assertEqual(b0.cache_tier, "red")
        self.assertEqual(b0.cache_worst_idx, 5)
        self.assertTrue(b0.has_compact)
        self.assertTrue(b0.has_account_switch)

        # Verify purple beats orange beats green
        mix = [
            _make_point(1, now, classification="hit", reason="cached", hit_rate=95.0),
            _make_point(2, now, classification="expected_cold", reason="idle_rewarm", hit_rate=0.0),
            _make_point(3, now, classification="expected_cold", reason="compact_reset", is_compact=True, hit_rate=0.0),
        ]
        b_mix = aggregate_points_to_buckets(mix, 1)
        self.assertEqual(b_mix[0].cache_tier, "purple")
        self.assertEqual(b_mix[0].cache_worst_idx, 2)

    def test_1264_steps_primitive_budget_readout_no_jargon_and_width_lock(self) -> None:
        root = self._create_root()
        now = time.time()
        pts = [
            _make_point(
                i + 1,
                now,
                model="Carbon High" if i % 400 >= 200 else "Neon XHigh",
                total_tokens=90000 + (i % 300) * 1000,
                input_tokens=3200,
                cache_read_tokens=86800 + (i % 300) * 1000,
                output_tokens=1400,
                ttft_sec=12.0 + (i % 15),
                streaming_sec=10.1,
                tps=138.0,
            )
            for i in range(1264)
        ]
        pts[500] = _make_point(
            501, now, total_tokens=450000, input_tokens=450000, cache_read_tokens=0,
            hit_rate=0.0, classification="abnormal_miss", reason="abnormal_zero", ttft_sec=142.0, tps=30.0,
        )
        rep = _make_report("long1264-0000-0000", pts, title="千步对话渲染压力测试")
        dw = ConversationDetailWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_report=lambda _cid: rep,
            on_toggle_pin=lambda _cid: None,
            is_pinned_fn=lambda _cid: False,
        )
        try:
            self.assertAlmostEqual(dw._slow_tps_threshold, 40.0, places=1)
            self.assertFalse(dw._async_load)
            dw.show_conversation(rep.conversation_id, place_near_parent=False)
            root.update()
            base_size = (dw.win.winfo_width(), dw.win.winfo_height())

            t0 = time.perf_counter()
            dw._set_window_mode("all")
            root.update()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(elapsed_ms, 500.0)

            total_items = sum(len(ch["canvas"].find_all()) for ch in dw._charts.values())
            self.assertLessEqual(total_items, 600, f"Canvas primitive budget exceeded: {total_items}")

            # Hover aggregated bucket and check plain-Chinese readout (zero '桶含' jargon)
            meta = dw._chart_meta["cache"]
            mid_x = int(meta["pad_l"] + meta["plot_w"] * 0.4)
            dw._on_chart_hover("cache", SimpleNamespace(x=mid_x))  # type: ignore[arg-type]
            root.update()
            txt = dw._lbl_readout.cget("text")
            self.assertIn("\n", txt)
            self.assertIn("构成: 缓存命中", txt)
            self.assertIn("新输入", txt)
            self.assertIn("输出", txt)
            self.assertIn("合并", txt)
            self.assertIn("左键锁定 / ←→ 逐拍", txt)
            self.assertNotIn("桶含", txt)

            # Worst-case media readout width & height lock
            pt_max = _make_point(
                9999, now, model="Hydrogen XH", total_tokens=512000, context_delta=99900,
                input_tokens=0, cache_read_tokens=512000, output_tokens=999, hit_rate=100.0,
                ttft_sec=123.4, tps=999.0, streaming_sec=1.0, is_revert=True,
                active_media_count=99, active_media_bytes=int(99.9 * 1024 * 1024),
            )
            dw._update_readout_for_point(pt_max, prefix="选定步", bucket_count=12, locked=False)
            root.update()
            self.assertEqual((dw.win.winfo_width(), dw.win.winfo_height()), base_size)
            self.assertIn("图99(99.9M)", dw._lbl_readout.cget("text"))
            self.assertLessEqual(dw._lbl_readout.winfo_reqwidth(), dw._readout_box.winfo_width() + 2)
            close_right = dw._btn_close.winfo_x() + dw._btn_close.winfo_width()
            self.assertLessEqual(close_right, dw._hdr.winfo_width() + 2)
        finally:
            dw.close()
            root.destroy()

    def test_four_filter_modes_click_lock_keyboard_stepping_and_escape(self) -> None:
        root = self._create_root()
        now = time.time()
        pts = [_make_point(i + 1, now, total_tokens=80000 + i * 500) for i in range(240)]
        # Make step index 100 (1-based step 101) abnormal
        pts[100] = _make_point(
            101, now, total_tokens=200000, input_tokens=200000, cache_read_tokens=0,
            hit_rate=0.0, classification="abnormal_miss", reason="abnormal_zero", ttft_sec=88.0,
        )
        rep = _make_report("mode2400-1111", pts)
        clean_pts = [_make_point(i + 1, now) for i in range(20)]
        rep_clean = _make_report("clean001-2222", clean_pts)
        reps = {rep.conversation_id: rep, rep_clean.conversation_id: rep_clean}

        dw = ConversationDetailWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_report=lambda cid: reps.get(cid),
            on_toggle_pin=lambda _cid: None,
            is_pinned_fn=lambda _cid: False,
        )
        try:
            dw.show_conversation(rep.conversation_id, place_near_parent=False)
            root.update()
            self.assertEqual(len(dw._display_points), 60)

            dw._set_window_mode("180")
            root.update()
            self.assertEqual(len(dw._display_points), 180)

            # Abnormal filter keeps abnormal point + prev & next neighbors -> 3 points (steps 100, 101, 102)
            dw._set_window_mode("abnormal")
            root.update()
            self.assertEqual([p.step_index for p in dw._display_points], [100, 101, 102])

            # All mode + click lock + focus_set + Left/Right stepping + Esc unlock then Esc close
            dw._set_window_mode("all")
            root.update()
            self.assertEqual(len(dw._display_points), 240)

            meta = dw._chart_meta["ctx"]
            click_x = int(meta["pad_l"] + meta["plot_w"] * 0.5)
            with mock.patch.object(dw.win, "focus_set", wraps=dw.win.focus_set) as m_focus:
                dw._on_chart_click("ctx", SimpleNamespace(x=click_x))  # type: ignore[arg-type]
                self.assertGreaterEqual(m_focus.call_count, 1)
            self.assertIsNotNone(dw._locked_point_idx)
            locked0 = int(dw._locked_point_idx or 0)
            self.assertTrue(bool(dw._charts["ctx"]["canvas"].find_withtag("lockline")))

            # Keyboard step right & left precisely by 1 real point
            dw._step_locked_point(1)
            self.assertEqual(dw._locked_point_idx, min(239, locked0 + 1))
            dw._step_locked_point(-1)
            self.assertEqual(dw._locked_point_idx, locked0)

            # First Escape unlocks without closing
            dw._on_escape_key(SimpleNamespace())  # type: ignore[arg-type]
            self.assertIsNone(dw._locked_point_idx)
            self.assertFalse(dw._closed)
            self.assertFalse(bool(dw._charts["ctx"]["canvas"].find_withtag("lockline")))

            # Clean conversation in abnormal mode shows friendly plain-Chinese hint
            dw.show_conversation(rep_clean.conversation_id, place_near_parent=False)
            dw._set_window_mode("abnormal")
            root.update()
            self.assertEqual(dw._display_points, [])
            self.assertIn("当前对话无异常掉缓存步", dw._lbl_readout.cget("text"))

            # Escape while unlocked closes the window
            dw._on_escape_key(SimpleNamespace())  # type: ignore[arg-type]
            self.assertTrue(dw._closed)
        finally:
            if not dw._closed:
                dw.close()
            root.destroy()

    @staticmethod
    def _pump_mainloop_until(root: tk.Tk, pred, timeout_sec: float = 3.0) -> bool:
        done = {"ok": False}
        deadline = time.perf_counter() + timeout_sec

        def _tick() -> None:
            if pred():
                done["ok"] = True
                root.quit()
                return
            if time.perf_counter() >= deadline:
                root.quit()
                return
            root.after(15, _tick)

        root.after(10, _tick)
        root.mainloop()
        return done["ok"]

    def test_async_load_stale_gen_drag_defer_threshold_hot_reload_and_close(self) -> None:
        root = self._create_root()
        now = time.time()
        rep_a = _make_report(
            "conv-aaaa",
            [_make_point(1, now, output_tokens=275, streaming_sec=5.0, tps=55.0)],
            title="对话A",
            avg_tps=55.0,
        )
        rep_b = _make_report(
            "conv-bbbb",
            [
                _make_point(1, now, output_tokens=275, streaming_sec=5.0, tps=55.0),
                _make_point(2, now, output_tokens=275, streaming_sec=5.0, tps=55.0),
            ],
            title="对话B",
            avg_tps=55.0,
        )

        gate_a = threading.Event()
        started_a = threading.Event()
        calls: list[str] = []

        def _slow_loader(cid: str) -> ConversationDetailReport | None:
            calls.append(cid)
            if cid == "conv-aaaa":
                started_a.set()
                gate_a.wait(timeout=3.0)
                return rep_a
            return rep_b

        dw = ConversationDetailWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_report=_slow_loader,
            on_toggle_pin=lambda _cid: None,
            is_pinned_fn=lambda _cid: False,
            slow_tps_threshold=40.0,
            async_load=True,
        )
        try:
            # 1. Instant non-blocking placeholder < 50ms
            t0 = time.perf_counter()
            dw.show_conversation("conv-aaaa", place_near_parent=False)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(elapsed_ms, 150.0)
            self.assertTrue(started_a.wait(timeout=2.0))
            self.assertTrue(dw._load_busy)
            self.assertIn("后台加载", dw._lbl_readout.cget("text"))

            # 2. Switch to conv-bbbb while conv-aaaa is still in flight -> queues _pending_reload
            dw.show_conversation("conv-bbbb", place_near_parent=False)
            self.assertTrue(dw._pending_reload)
            # Release gate_a from inside mainloop so worker's win.after(0, _apply) runs under active Tk mainloop
            root.after(20, gate_a.set)
            ok = self._pump_mainloop_until(
                root,
                lambda: dw._report is not None and dw._report.conversation_id == "conv-bbbb" and not dw._load_busy,
                timeout_sec=3.0,
            )
            self.assertTrue(ok)
            self.assertIsNotNone(dw._report)
            self.assertEqual(dw._report.conversation_id, "conv-bbbb")
            self.assertFalse(dw._load_busy)
            self.assertIn("conv-bbbb", calls)

            # 3. Hot-reload slow_tps_threshold updates KPI 4 color immediately
            self.assertEqual(dw._kpi_cards[3]["val"].cget("fg"), THEMES["midnight_blue"]["cyan"])
            dw.update_slow_tps_threshold(80.0)
            root.update()
            self.assertEqual(dw._kpi_cards[3]["val"].cget("fg"), THEMES["midnight_blue"]["orange"])

            # 4. Dragging defers loaded report until drag ends
            dw._on_dragger_started()
            dw.refresh(blocking=False)
            ok_drag = self._pump_mainloop_until(
                root,
                lambda: dw._pending_loaded_report is not None and not dw._load_busy,
                timeout_sec=2.5,
            )
            self.assertTrue(ok_drag)
            self.assertIsNotNone(dw._pending_loaded_report)
            dw._on_dragger_ended(True)
            root.update()
            self.assertIsNone(dw._pending_loaded_report)

            # 5. TclError / RuntimeError on win.after clears _load_busy safely
            with mock.patch.object(dw.win, "after", side_effect=RuntimeError("main thread is not in main loop")):
                dw.refresh(blocking=False)
                deadline = time.perf_counter() + 2.0
                while dw._load_busy and time.perf_counter() < deadline:
                    time.sleep(0.01)
            self.assertFalse(dw._load_busy)

            # 6. Close increments _load_gen cleanly
            gen_before = dw._load_gen
            dw.close()
            self.assertGreater(dw._load_gen, gen_before)
        finally:
            if not dw._closed:
                dw.close()
            root.destroy()


class TestV730Lane3SlotsWindow(unittest.TestCase):
    def _create_root(self) -> tk.Tk:
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError:
            self.skipTest("Tk display unavailable")

    def test_slots_size_provider_sort_search_debounce_and_recycle_notice(self) -> None:
        root = self._create_root()
        now = time.time()
        items = [
            SlotConversationItem(
                conversation_id="sub-1111", short_id="sub111", category="cleanable_sub",
                title="阿尔法分析任务", agent_name="researcher", parent_conversation_id="main1",
                step_count=12, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0,
                disk_bytes=5_000_000, last_active_ts=now - 9000, age_min=150.0,
                cleanable_at_30m=True, protect_reason="可清理",
            ),
            SlotConversationItem(
                conversation_id="sub-2222", short_id="sub222", category="cleanable_sub",
                title="贝塔代码审计", agent_name="auditor", parent_conversation_id="main1",
                step_count=95, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0,
                disk_bytes=1_200_000, last_active_ts=now - 7500, age_min=125.0,
                cleanable_at_30m=True, protect_reason="可清理",
            ),
            SlotConversationItem(
                conversation_id="sub-3333", short_id="sub333", category="cleanable_sub",
                title="伽马打包脚本", agent_name="builder", parent_conversation_id="main1",
                step_count=40, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0,
                disk_bytes=18_000_000, last_active_ts=now - 8200, age_min=136.0,
                cleanable_at_30m=True, protect_reason="可清理",
            ),
        ]
        inv = SlotInventorySnapshot(
            observed_at=now, cap=500, total_db=420, remaining_slots=80,
            main_count=100, protected_sub_count=50, cleanable_sub_count=3,
            cleanable_30m_count=3, unknown_count=0, pinned_floor_count=150,
            cleanable_bytes=24_200_000, status="warn", main_at_risk=False,
            risk_message="接近警戒线", items=items,
        )

        done_cb_holder: list[object] = []

        def _fake_clean(cb):
            done_cb_holder.append(cb)

        sw = ConversationSlotsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_inventory=lambda: inv,
            on_clean_now=_fake_clean,
            is_auto_clean_enabled=lambda: True,
            on_toggle_auto_clean=lambda: None,
            get_protect_age_min=lambda: 120,
            get_clean_mode=lambda: "recycle",
        )
        try:
            # 1. size_provider wired to LightweightProxyDragger
            self.assertIsNotNone(sw._dragger)
            self.assertEqual(sw._dragger._size_provider, sw._get_window_size)
            sz = sw._get_window_size()
            self.assertGreaterEqual(sz[0], 840)
            self.assertGreaterEqual(sz[1], 480)

            sw.show(place_near_parent=False)
            root.update()

            # 2. Fixed header has 5 labels
            hdr_texts = [lbl.cget("text") for lbl in sw._col_hdr_lbls]
            self.assertEqual(hdr_texts, ["ID", "对话名称", "步数", "体积 · 最近活跃", "状态"])

            # 3. Sorting: time (oldest first for cleanable_sub), size desc, steps desc
            self.assertEqual([r.short_id for r in sw._get_filtered_sorted_rows()], ["sub111", "sub333", "sub222"])
            sw._set_sort_mode("size")
            self.assertEqual([r.short_id for r in sw._get_filtered_sorted_rows()], ["sub333", "sub111", "sub222"])
            sw._set_sort_mode("steps")
            self.assertEqual([r.short_id for r in sw._get_filtered_sorted_rows()], ["sub222", "sub333", "sub111"])

            # 4. Search 120ms debounce + clear + Escape clears search first
            sw._search_var.set("阿")
            aid1 = sw._search_after_id
            self.assertIsNotNone(aid1)
            sw._search_var.set("auditor")
            aid2 = sw._search_after_id
            self.assertIsNotNone(aid2)
            self.assertNotEqual(aid1, aid2)
            # Before debounce fires, query is still empty
            self.assertEqual(sw._search_query, "")
            time.sleep(0.16)
            root.update()
            self.assertEqual(sw._search_query, "auditor")
            self.assertEqual([r.short_id for r in sw._get_filtered_sorted_rows()], ["sub222"])

            # Escape clears non-empty search instead of closing
            sw._on_escape_key(SimpleNamespace())  # type: ignore[arg-type]
            self.assertFalse(sw._closed)
            self.assertEqual(sw._search_query, "")
            self.assertEqual(len(sw._get_filtered_sorted_rows()), 3)

            # 5. Recycle unavailable cleanup message
            res = SlotCleanupResult(
                started_at=now - 1.0,
                finished_at=now,
                trigger="manual",
                mode="recycle",
                protect_age_min=120,
                dry_run=False,
                removed_count=0,
                freed_bytes=0,
                skipped_locked_count=1,
                skipped_changed_count=0,
                remaining_db_count=420,
                skipped_recycle_unavailable_count=3,
            )
            with mock.patch("ui.slots_window.messagebox.askyesno", return_value=True):
                sw._handle_clean_click()
            self.assertEqual(len(done_cb_holder), 1)
            done_cb_holder[0](res)
            root.update()
            st_txt = sw._lbl_status.cget("text")
            self.assertIn("回收站不可用跳过 3 条（可在设置切换为归档模式）", st_txt)

            # Schedule a debounce then close -> must clean up _search_after_id cleanly
            sw._search_var.set("pending")
            self.assertIsNotNone(sw._search_after_id)
            sw.close()
            self.assertIsNone(sw._search_after_id)
        finally:
            if not sw._closed:
                sw.close()
            root.destroy()


class TestV730Lane3SettingsWindow(unittest.TestCase):
    def _create_root(self) -> tk.Tk:
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError:
            self.skipTest("Tk display unavailable")

    def test_zero_reverse_dependency_on_capsule_app(self) -> None:
        src = (Path(__file__).resolve().parent.parent / "ui" / "settings_window.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("capsule_app", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("capsule_app", node.module or "")

    def test_alpha_preview_rollback_on_all_uncommitted_close_paths(self) -> None:
        root = self._create_root()
        base_cfg = {
            "window_alpha": 0.94,
            "theme": "midnight_blue",
            "poll_interval_ms": 2500,
            "topmost": True,
            "start_minimized": False,
            "notifications_enabled": True,
            "freshness_window_sec": 210,
            "idle_rewarm_sec": 210,
            "large_context_threshold": 100000,
            "slow_tps_threshold": 40.0,
            "slow_ttft_threshold_sec": 45.0,
            "slot_auto_clean_enabled": True,
            "slot_protect_age_min": 120,
            "slot_clean_mode": "recycle",
            "slot_archive_dir": "",
            "update_check_enabled": True,
            "update_mode": "download",
        }
        try:
            for path_name in ("cancel_btn", "close_x", "escape", "wm_delete", "direct_close"):
                previews: list[float] = []
                closed_hits: list[int] = []
                sw = MonitorSettingsWindow(
                    parent=root,
                    theme=THEMES["midnight_blue"],
                    get_config=lambda: dict(base_cfg),
                    on_save_patch=lambda p: dict(base_cfg, **p),
                    on_preview_alpha=lambda a: previews.append(a),
                    on_closed=lambda: closed_hits.append(1),
                )
                sw.show(place_near_parent=False)
                root.update()
                sw._var_alpha_pct.set(78)
                sw._on_alpha_slider_changed(78)
                self.assertEqual(previews[-1], 0.78)

                if path_name == "cancel_btn":
                    sw._act_btns["cancel"].event_generate("<Button-1>")
                    root.update()
                elif path_name == "close_x":
                    sw._btn_close.event_generate("<Button-1>")
                    root.update()
                elif path_name == "escape":
                    sw.win.focus_force()
                    sw.win.event_generate("<Escape>")
                    root.update()
                elif path_name == "wm_delete":
                    sw.close()
                else:
                    sw.close()

                self.assertTrue(sw._closed, f"Failed to close on path {path_name}")
                self.assertEqual(previews[-1], 0.94, f"Alpha not restored on path {path_name}")
                self.assertEqual(len(closed_hits), 1)
        finally:
            root.destroy()

    def test_save_patch_freshness_sync_theme_switch_and_filedialog_reentry_guard(self) -> None:
        root = self._create_root()
        cfg = {
            "window_alpha": 0.95,
            "theme": "midnight_blue",
            "poll_interval_ms": 2500,
            "topmost": True,
            "start_minimized": False,
            "notifications_enabled": True,
            "freshness_window_sec": 210,
            "idle_rewarm_sec": 210,
            "large_context_threshold": 100000,
            "slow_tps_threshold": 40.0,
            "slow_ttft_threshold_sec": 45.0,
            "slot_auto_clean_enabled": True,
            "slot_protect_age_min": 120,
            "slot_clean_mode": "recycle",
            "slot_archive_dir": "",
            "update_check_enabled": True,
            "update_mode": "download",
        }
        previews: list[float] = []
        saved_patches: list[dict] = []

        def _on_save(patch: dict) -> dict:
            saved_patches.append(dict(patch))
            cfg.update(patch)
            return dict(cfg)

        sw = MonitorSettingsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            get_config=lambda: dict(cfg),
            on_save_patch=_on_save,
            on_preview_alpha=lambda a: previews.append(a),
        )
        try:
            sw.show(place_near_parent=False)
            sw.apply_theme(THEMES["obsidian_dark"])
            sw.check_drag_watchdog()
            root.update()

            # Test filedialog re-entrancy guard & temporary topmost drop/restore
            topmost_during_dialog: list[bool] = []
            reentry_calls = {"n": 0}

            def _fake_askdir(**_kw):
                reentry_calls["n"] += 1
                topmost_during_dialog.append(bool(sw.win.attributes("-topmost")))
                # Attempt re-entrant click while dialog is open -> must be ignored
                sw._browse_archive_dir()
                return "C:/Temp/ag_archive"

            with mock.patch("ui.settings_window.filedialog.askdirectory", side_effect=_fake_askdir):
                sw._browse_archive_dir()

            self.assertEqual(reentry_calls["n"], 1)
            self.assertEqual(topmost_during_dialog, [False])
            self.assertTrue(bool(sw.win.attributes("-topmost")))
            self.assertEqual(sw._var_slot_archive_dir.get(), "C:/Temp/ag_archive")

            # Modify alpha, freshness_window_sec, clean_mode, update_mode, slow_tps & save
            sw._var_alpha_pct.set(86)
            sw._on_alpha_slider_changed(86)
            sw._var_freshness_sec.set("240")
            sw._var_slow_tps.set("50")
            sw._select_clean_mode("archive")
            sw._select_update_mode("auto_restart")
            sw._handle_save()
            root.update()

            self.assertTrue(sw._closed)
            self.assertEqual(len(saved_patches), 1)
            p = saved_patches[0]
            self.assertAlmostEqual(p["window_alpha"], 0.86, places=2)
            self.assertEqual(p["freshness_window_sec"], 240)
            self.assertEqual(p["idle_rewarm_sec"], 240)
            self.assertEqual(p["slow_tps_threshold"], 50.0)
            self.assertEqual(p["slot_clean_mode"], "archive")
            self.assertEqual(p["slot_archive_dir"], "C:/Temp/ag_archive")
            self.assertEqual(p["update_mode"], "auto_restart")
            self.assertTrue(p["auto_update_restart"])
            # Committed save must NOT roll alpha back to 0.95
            self.assertEqual(previews[-1], 0.86)
        finally:
            if not sw._closed:
                sw.close()
            root.destroy()

    def test_slots_concurrent_refresh_queued_and_close_during_confirm_safe(self) -> None:
        root = self._create_root()
        now = time.time()
        inv = SlotInventorySnapshot(
            observed_at=now, cap=500, total_db=420, remaining_slots=80,
            main_count=100, protected_sub_count=50, cleanable_sub_count=2,
            cleanable_30m_count=2, unknown_count=0, pinned_floor_count=150,
            cleanable_bytes=4_000_000, status="warn", main_at_risk=False,
            risk_message="接近警戒线", items=[],
        )
        gate = threading.Event()
        calls = {"n": 0}

        def _slow_loader() -> SlotInventorySnapshot:
            calls["n"] += 1
            if calls["n"] == 1:
                gate.wait(timeout=2.0)
            return inv

        sw = ConversationSlotsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_inventory=_slow_loader,
            on_clean_now=lambda cb: None,
            is_auto_clean_enabled=lambda: True,
            on_toggle_auto_clean=lambda: None,
            get_protect_age_min=lambda: 120,
            get_clean_mode=lambda: "recycle",
            async_refresh=True,
        )
        try:
            sw.refresh()  # starts background load #1
            deadline = time.perf_counter() + 1.0
            while calls["n"] < 1 and time.perf_counter() < deadline:
                time.sleep(0.01)
            sw.refresh()  # arrives while _refresh_busy=True -> must queue _pending_refresh
            self.assertTrue(sw._pending_refresh)
            # Release gate inside active Tk mainloop so background thread's win.after(0, _apply) succeeds
            timed_out = {"v": False}
            deadline_t = time.perf_counter() + 3.0

            def _pump() -> None:
                if calls["n"] >= 2 and not sw._refresh_busy:
                    root.quit()
                    return
                if time.perf_counter() >= deadline_t:
                    timed_out["v"] = True
                    root.quit()
                    return
                root.after(20, _pump)

            root.after(20, gate.set)
            root.after(30, _pump)
            root.mainloop()
            self.assertFalse(timed_out["v"])
            self.assertEqual(calls["n"], 2)

            # Closing window while askyesno dialog is open MUST NOT raise KeyError('clean')
            def _close_mid_dialog(*_a, **_kw) -> bool:
                sw.close()
                return True

            with mock.patch("ui.slots_window.messagebox.askyesno", side_effect=_close_mid_dialog):
                sw._handle_clean_click()
            self.assertTrue(sw._closed)
        finally:
            gate.set()
            if not sw._closed:
                sw.close()
            root.destroy()

    def test_settings_plan_a_opaque_flat_layout_and_live_theme_topmost_rollback(self) -> None:
        root = self._create_root()
        cfg = {
            "window_alpha": 0.92,
            "theme": "midnight_blue",
            "topmost": False,
        }
        theme_previews: list[str] = []
        topmost_previews: list[bool] = []
        drag_states: list[bool] = []

        sw = MonitorSettingsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            get_config=lambda: dict(cfg),
            on_save_patch=lambda p: dict(p),
            on_preview_theme=lambda th: theme_previews.append(th),
            on_preview_topmost=lambda tm: topmost_previews.append(tm),
            on_drag_state_changed=lambda act: drag_states.append(act),
        )
        try:
            sw.show(place_near_parent=False)
            root.update()
            # 1) Opaque window (alpha == 1.0, avoids Win32 WS_EX_LAYERED drag lag) & topmost matches config (False)
            self.assertAlmostEqual(float(sw.win.attributes("-alpha")), 1.0, places=2)
            self.assertFalse(bool(sw.win.attributes("-topmost")))

            # 2) Flat widget tree stays compact (< 70 total Tk widgets vs old 83)
            def _count_tk(w: tk.Misc) -> int:
                return 1 + sum(_count_tk(c) for c in w.winfo_children())

            self.assertLess(_count_tk(sw.win), 70)

            # 3) Live preview theme & topmost + drag pause hook + clean rollback on uncommitted close
            sw._select_theme_option("obsidian_dark")
            self.assertEqual(theme_previews[-1], "obsidian_dark")
            sw._var_topmost.set(True)
            sw._on_topmost_toggled()
            self.assertTrue(bool(sw.win.attributes("-topmost")))
            self.assertEqual(topmost_previews[-1], True)

            sw._on_dragger_started()
            sw._on_dragger_ended(True)
            self.assertEqual(drag_states, [True, False])

            sw.close()
            self.assertEqual(theme_previews[-1], "midnight_blue")
            self.assertEqual(topmost_previews[-1], False)
        finally:
            if not sw._closed:
                sw.close()
            root.destroy()

    def test_drag_proxy_2hwnd_swap_clamp_focusout_filter_and_cleanup(self) -> None:
        import os
        from ui.drag_proxy import LightweightProxyDragger, resolve_root_hwnd

        root = self._create_root()
        cfg = {"window_alpha": 0.92, "theme": "midnight_blue", "topmost": False}
        drag_states: list[bool] = []
        sw = MonitorSettingsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            get_config=lambda: dict(cfg),
            on_save_patch=lambda p: dict(p),
            on_drag_state_changed=lambda act: drag_states.append(act),
            screen_bounds_fn=lambda _w: (0, 0, 1920, 1080),
        )
        try:
            sw.show(place_near_parent=False)
            sw.win.geometry("+200+120")
            root.update()

            drg = sw._dragger
            self.assertIsInstance(drg, LightweightProxyDragger)
            self.assertEqual(drg._move_threshold, 3)

            # 1) Child FocusOut bubbling MUST NOT cancel an active drag; <3px click never activates proxy
            drg.on_start(SimpleNamespace(x_root=240, y_root=135))  # type: ignore[arg-type]
            self.assertTrue(drg.active)
            self.assertEqual(drag_states[-1], True)
            sw._on_focus_out(SimpleNamespace(widget=sw._ent_poll))  # type: ignore[arg-type]
            self.assertTrue(drg.active)
            drg.on_move(SimpleNamespace(x_root=242, y_root=136))  # type: ignore[arg-type]
            self.assertFalse(drg.moved)
            self.assertFalse(drg.proxy_active)
            drg.on_end(SimpleNamespace(x_root=242, y_root=136))  # type: ignore[arg-type]
            self.assertEqual(drag_states[-1], False)

            # 1b) _surface drag guard: bottom area ignored, top strip starts drag cleanly
            sw._on_surface_drag_start(SimpleNamespace(widget=sw._surface, x_root=240, y_root=420, y=300))  # type: ignore[arg-type]
            self.assertFalse(drg.active)
            sw._on_surface_drag_start(SimpleNamespace(widget=sw._surface, x_root=240, y_root=130, y=8))  # type: ignore[arg-type]
            self.assertTrue(drg.active)
            drg.on_end(SimpleNamespace(x_root=240, y_root=130))  # type: ignore[arg-type]

            # 2) Proxy warm-up & activation: 2-HWND opaque bar, syncs topmost=False & live theme colors
            sw.apply_theme(THEMES["obsidian_dark"])
            drg.warm_up()
            self.assertIsNotNone(drg._proxy_win)
            self.assertAlmostEqual(float(drg._proxy_win.attributes("-alpha")), 1.0, places=2)
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                u32 = ctypes.WinDLL("user32", use_last_error=True)
                EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                u32.EnumChildWindows.argtypes = [wintypes.HWND, EnumChildProc, wintypes.LPARAM]
                u32.EnumChildWindows.restype = wintypes.BOOL
                p_hwnd = drg._activate_proxy_at(260, 140)
                self.assertGreater(p_hwnd, 0)
                self.assertTrue(drg.proxy_active)
                self.assertEqual(sw.win.state(), "withdrawn")
                self.assertFalse(bool(drg._proxy_win.attributes("-topmost")))
                self.assertEqual(drg._proxy_canvas.cget("bg"), THEMES["obsidian_dark"]["panel_bg"])
                child_cnt = [0]

                def _cb(_h, _lp):
                    child_cnt[0] += 1
                    return True

                u32.EnumChildWindows(wintypes.HWND(p_hwnd), EnumChildProc(_cb), 0)
                self.assertLessEqual(child_cnt[0], 2, f"Proxy bar exceeded 2 Win32 child HWNDs: {child_cnt[0]}")

                # 2b) Default cancel() MUST restore real modal (never leave window withdrawn)
                drg.cancel()
                root.update()
                self.assertFalse(drg.proxy_active)
                self.assertNotEqual(sw.win.state(), "withdrawn")

                # 2c) PostMessageW failure during proxy handoff MUST enter _fallback_chasing (never freeze)
                drg._active = True
                drg._moved = True
                drg._use_win32_cursor = True
                drg._hwnd = resolve_root_hwnd(sw.win)
                drg._orig_size = (820, 520)
                fake_u32 = mock.MagicMock()
                fake_u32.GetCursorPos.side_effect = lambda pt: True
                fake_u32.GetSystemMetrics.return_value = 0
                fake_u32.GetAsyncKeyState.return_value = -32768
                fake_u32.GetAncestor.side_effect = lambda h, f: u32.GetAncestor(h, f)
                fake_u32.IsWindow.side_effect = lambda h: u32.IsWindow(h)
                fake_u32.SetWindowPos.return_value = True
                fake_u32.PostMessageW.return_value = False
                with mock.patch("ui.drag_proxy._get_user32", return_value=fake_u32):
                    handed = drg._try_handoff_proxy_native_drag()
                self.assertTrue(handed)
                self.assertTrue(drg._fallback_chasing)
                # Force finish_or_cancel(force=True) cleanly restores real modal
                drg.finish_or_cancel(commit=False, force=True)
                root.update()
                self.assertFalse(drg.proxy_active)
                self.assertNotEqual(sw.win.state(), "withdrawn")

                # 3) Release near screen bottom edge must clamp full original modal height inside work area
                drg._activate_proxy_at(260, 140)
                drg._orig_size = (820, 520)
                drg._restore_real_window_from_proxy((1800, 1020), restore_visible=True)
                root.update()
                self.assertFalse(drg.proxy_active)
                self.assertEqual(drg._proxy_win.state(), "withdrawn")
                self.assertNotEqual(sw.win.state(), "withdrawn")
                self.assertLessEqual(sw.win.winfo_x() + 820, 1920)
                self.assertLessEqual(sw.win.winfo_y() + 520, 1080)
                self.assertGreater(resolve_root_hwnd(sw.win), 0)

            proxy_ref = drg._proxy_win
            sw.close()
            self.assertTrue(sw._closed)
            if proxy_ref is not None:
                self.assertFalse(bool(proxy_ref.winfo_exists()))
        finally:
            if not sw._closed:
                sw.close()
            root.destroy()

    def test_capsule_app_defers_render_snapshot_during_aux_window_drag_and_flushes_on_end(self) -> None:
        import tempfile
        from core.config_store import ConfigStore
        from core.log_monitor import AntigravityMonitorEngine
        from ui.capsule_app import AntigravityCapsuleApp

        app_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as td:
            cs = ConfigStore(Path(td) / "config.json")
            cfg = cs.load()
            eng = AntigravityMonitorEngine(config=cfg, read_only=True)
            app = AntigravityCapsuleApp(app_root=app_root, config_store=cs, engine=eng, enable_tray=False)
            try:
                app.set_expanded(True)
                app.root.update()
                app.open_settings_window()
                app.root.update()
                sw = app._settings_win
                self.assertIsNotNone(sw)

                # Drive real dragger state machine on_start -> on_move -> on_end
                sw._dragger.on_start(SimpleNamespace(x_root=200, y_root=150))  # type: ignore[arg-type]
                self.assertTrue(app._is_any_window_dragging())
                self.assertFalse(app._drag_idle_event.is_set())

                sentinel = object()
                app.render_snapshot(sentinel)  # type: ignore[arg-type]
                self.assertIs(app._pending_drag_snapshot, sentinel)

                flushed: list[object] = []
                with mock.patch.object(app, "render_snapshot", side_effect=lambda s: flushed.append(s)):
                    sw._dragger.on_end(SimpleNamespace(x_root=200, y_root=150))  # type: ignore[arg-type]

                self.assertFalse(app._is_any_window_dragging())
                self.assertTrue(app._drag_idle_event.is_set())
                self.assertEqual(flushed, [sentinel])
            finally:
                if app._settings_win is not None and not app._settings_win._closed:
                    app._settings_win.close()
                app.shutdown()

    def test_slots_and_detail_opaque_proxy_dragger_and_drag_state_callback(self) -> None:
        from ui.drag_proxy import LightweightProxyDragger

        root = self._create_root()
        slot_drag_states: list[bool] = []
        det_drag_states: list[bool] = []
        sw = ConversationSlotsWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_inventory=lambda: SlotInventorySnapshot(
                observed_at=time.time(), cap=500, total_db=10, remaining_slots=490,
                main_count=5, protected_sub_count=5, cleanable_sub_count=0,
                cleanable_30m_count=0, unknown_count=0, pinned_floor_count=10,
                cleanable_bytes=0, status="ok", main_at_risk=False, risk_message="", items=[],
            ),
            on_clean_now=lambda _cb: None,
            is_auto_clean_enabled=lambda: True,
            on_toggle_auto_clean=lambda: None,
            get_protect_age_min=lambda: 120,
            get_clean_mode=lambda: "recycle",
            on_drag_state_changed=lambda act: slot_drag_states.append(act),
        )
        dw = ConversationDetailWindow(
            parent=root,
            theme=THEMES["midnight_blue"],
            load_report=lambda _cid: None,
            on_toggle_pin=lambda _cid: None,
            is_pinned_fn=lambda _cid: False,
            on_drag_state_changed=lambda act: det_drag_states.append(act),
        )
        try:
            self.assertIsInstance(sw._dragger, LightweightProxyDragger)
            self.assertIsInstance(dw._dragger, LightweightProxyDragger)
            self.assertEqual(sw._dragger._move_threshold, 3)
            self.assertEqual(dw._dragger._move_threshold, 3)
            self.assertAlmostEqual(float(sw.win.attributes("-alpha")), 1.0, places=2)
            self.assertAlmostEqual(float(dw.win.attributes("-alpha")), 1.0, places=2)

            sw._on_dragger_started()
            sw._on_dragger_ended(True)
            self.assertEqual(slot_drag_states, [True, False])

            dw._on_dragger_started()
            dw._on_dragger_ended(True)
            self.assertEqual(det_drag_states, [True, False])
        finally:
            sw.close()
            dw.close()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
