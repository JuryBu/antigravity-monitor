"""Real Tkinter visual capture harness for UI verification (uses synthetic demo data for alert states)."""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageGrab

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.config_store import ConfigStore
from core.log_monitor import (
    AccountQuotaInfo,
    AntigravityMonitorEngine,
    ConversationDetailPoint,
    ConversationDetailReport,
    ConversationSummary,
    MonitorSnapshot,
    StepRecord,
)
from ui.capsule_app import AntigravityCapsuleApp
from ui.detail_window import ConversationDetailWindow
from ui.icon_builder import build_tray_icon
from ui.themes import THEMES


def _enable_dpi() -> None:
    if os.name == "nt":
        try:
            ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(1)
        except Exception:
            pass


def _capture_widget(root, out_path: Path, pad: int = 0) -> None:
    root.update_idletasks()
    root.update()
    time.sleep(0.18)
    x = root.winfo_rootx() - pad
    y = root.winfo_rooty() - pad
    w = root.winfo_width() + pad * 2
    h = root.winfo_height() + pad * 2
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(out_path, "PNG")


def _build_demo_alert_snapshot() -> MonitorSnapshot:
    now = time.time()
    bad_steps = [
        StepRecord(
            i,
            now - (10 - i) * 20,
            "gemini-4-neon-xhigh",
            190000 if i not in (3, 7) else 2500,
            0 if i not in (3, 7) else 185000,
            1200,
            190000,
            0.0 if i not in (3, 7) else 97.4,
            "abnormal_miss" if i not in (3, 7) else "hit",
            "abnormal_zero" if i not in (3, 7) else "cached",
        )
        for i in range(1, 11)
    ]
    good_steps = [
        StepRecord(i, now - (10 - i) * 30, "gemini-4-hydrogen-xhigh", 3200, 61000, 900, 64200, 95.0, "hit", "cached")
        for i in range(1, 11)
    ]
    c1 = ConversationSummary(
        conversation_id="demo0001-0000-0000-0000-000000000001",
        short_id="demo01",
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        context_tokens=198300,
        max_context_tokens=512000,
        latest_hit_rate=0.0,
        latest_cached_in=0,
        latest_uncached_in=198300,
        latest_timestamp=now - 18,
        recent_steps=bad_steps,
        consec_miss=5,
        consec_miss_start_step=6,
        single_miss=False,
        wasted_cold_tokens=1_520_000,
        freshness_remaining_sec=None,
        freshness_invalid_reason="无热缓存 (连掉 5 步)",
        ttft_sec=38.9,
        streaming_sec=88.0,
        tps=27.8,
        shard_tag="dm01",
        speed_status="slow",
        recent_ttfts=[11.2, 14.5, 18.0, 22.4, 29.1, 33.8, 38.9],
        recent_tps_list=[165.0, 152.0, 128.0, 82.0, 48.0, 34.0, 27.8],
        ttft_avg_sec=24.0,
        ttft_max_sec=38.9,
    )
    c2 = ConversationSummary(
        conversation_id="demo0002-0000-0000-0000-000000000002",
        short_id="demo02",
        model="Hydrogen XH",
        model_raw="gemini-4-hydrogen-xhigh",
        context_tokens=64200,
        max_context_tokens=512000,
        latest_hit_rate=95.0,
        latest_cached_in=61000,
        latest_uncached_in=3200,
        latest_timestamp=now - 45,
        recent_steps=good_steps,
        consec_miss=0,
        consec_miss_start_step=10,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=None,
        freshness_invalid_reason="",
        ttft_sec=5.2,
        streaming_sec=4.6,
        tps=195.0,
        shard_tag="dm02",
        speed_status="ok",
        recent_ttfts=[4.8, 5.5, 6.1, 4.9, 5.2],
        recent_tps_list=[188.0, 192.0, 184.0, 198.0, 195.0],
        ttft_avg_sec=5.3,
        ttft_max_sec=6.1,
    )
    c3_cold = ConversationSummary(
        conversation_id="demo0003-0000-0000-0000-000000000003",
        short_id="demo03",
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        context_tokens=22400,
        max_context_tokens=512000,
        latest_hit_rate=0.0,
        latest_cached_in=0,
        latest_uncached_in=22400,
        latest_timestamp=now - 55,
        recent_steps=[
            StepRecord(1, now - 55, "gemini-4-neon-xhigh", 22400, 0, 320, 22400, 0.0, "expected_cold", "small_ctx"),
        ],
        consec_miss=0,
        consec_miss_start_step=1,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=None,
        freshness_invalid_reason="",
        ttft_sec=6.4,
        streaming_sec=2.8,
        tps=142.0,
        shard_tag="dm03",
        speed_status="ok",
        recent_ttfts=[6.4],
        recent_tps_list=[142.0],
        ttft_avg_sec=6.4,
        ttft_max_sec=6.4,
        latest_cache_reason="small_ctx",
        title="新建小上下文演示",
    )
    c_carry = ConversationSummary(
        conversation_id="demo0004-0000-0000-0000-000000000004",
        short_id="demo04",
        model="Carbon High",
        model_raw="gemini-4-carbon-high",
        context_tokens=168000,
        max_context_tokens=512000,
        latest_hit_rate=0.0,
        latest_cached_in=0,
        latest_uncached_in=168000,
        latest_timestamp=now - 35,
        recent_steps=[],
        consec_miss=0,
        consec_miss_start_step=0,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=None,
        freshness_invalid_reason="",
        latest_cache_reason="account_switch",
        is_carryover_preview=True,
        title="同窗口换号过渡演示",
    )
    return MonitorSnapshot(
        generated_at=now,
        level="red",
        grade="T1",
        headline="T1 连续掉缓存 · Neon XHigh",
        sub_headline="主活跃会话已连续 5 步 cr=0，建议检查账号或切回主力号",
        active_model="Neon XHigh",
        primary_hit_rate=0.0,
        primary_hit_classification="abnormal_miss",
        primary_tps=27.8,
        primary_ttft=38.9,
        primary_speed_status="slow",
        min_freshness_sec=None,
        min_freshness_conv="demo01",
        min_freshness_ctx=198300,
        min_freshness_invalid_reason="无热缓存 (连掉 5 步)",
        today_steps=312,
        daily_steps_warn=800,
        daily_steps_alert=1000,
        active_conversations=[c1, c2, c3_cold],
        carryover_conversations=[c_carry],
        large_freshness_list=[c1],
        recent_503=0,
        today_503=0,
        recent_timeout=0,
        today_timeout=0,
        recent_403=0,
        today_403=0,
        recent_retry=4,
        today_retry=4,
        recent_events=[],
        wasted_cold_tokens_total=1_520_000,
        max_consec_miss=5,
        account=AccountQuotaInfo(
            email_masked="de***o@example.com",
            short_label="de***o",
            tier="demo-tier",
            five_hour_pct=83.6,
            weekly_pct=59.0,
            online=True,
            observed_at=now,
        ),
        sliding_window_min=30,
        freshness_window_sec=210,
        primary_ttft_avg=24.0,
        primary_ttft_max=38.9,
        primary_recent_ttfts=[11.2, 14.5, 18.0, 22.4, 29.1, 33.8, 38.9],
        account_today_steps=86,
        total_today_steps=312,
        steps_scope_label="de***o",
    )


def _build_demo_detail_report() -> ConversationDetailReport:
    now = time.time()
    pts = []
    specs = [
        # (step_idx, model, total, delta, hr, cls, reason, ttft, tps, strm, rev, cmp, acc_sw, mc, mb)
        (1, "Neon XHigh", 22400, 22400, 0.0, "expected_cold", "small_ctx", 6.8, 152.0, 2.4, False, False, False, 0, 0),
        (2, "Neon XHigh", 46800, 24400, 0.0, "expected_cold", "first_cacheable", 9.4, 160.0, 3.1, False, False, False, 0, 0),
        (3, "Neon XHigh", 78500, 31700, 96.4, "hit", "cached", 11.2, 168.0, 4.0, False, False, False, 1, 1_450_000),
        (4, "Neon XHigh", 124000, 45500, 98.1, "hit", "cached", 19.6, 158.0, 4.5, False, False, False, 2, 3_200_000),
        (5, "Neon XHigh", 168200, 44200, 98.9, "hit", "cached", 34.8, 162.0, 5.2, False, False, False, 3, 4_850_000),
        (6, "Carbon High", 192400, 24200, 0.0, "expected_cold", "model_switch", 22.5, 148.0, 6.0, False, False, False, 3, 4_850_000),
        (7, "Carbon High", 228900, 36500, 98.4, "hit", "cached", 16.4, 156.0, 4.8, False, False, False, 3, 4_850_000),
        (8, "Carbon High", 264100, 35200, 0.0, "expected_cold", "account_switch", 24.1, 151.0, 4.2, False, False, True, 3, 4_850_000),
        (9, "Carbon High", 385600, 31500, 99.1, "hit", "cached", 14.2, 165.0, 5.1, False, False, False, 3, 4_850_000),
        (10, "Carbon High", 218400, -167200, 0.0, "expected_cold", "compact_reset", 18.0, 171.0, 3.8, False, True, False, 0, 0),
        (11, "Carbon High", 252600, 34200, 0.0, "expected_cold", "rule_change", 17.6, 170.0, 4.0, False, False, False, 1, 1_200_000),
        (12, "Carbon High", 342000, 89400, 98.7, "hit", "cached", 19.1, 174.0, 4.1, False, False, False, 1, 2_100_000),
        (13, "Neon XHigh", 473800, 131800, 99.2, "hit", "cached", 48.2, 168.0, 5.6, False, False, False, 4, 6_400_000),
    ]
    for idx, (sidx, mod, tot, dlt, hr, cls, rsn, ttft, tps, strm, rev, cmp_, acc_sw, mc, mb) in enumerate(specs):
        cached = int(tot * (hr / 100.0))
        pts.append(
            ConversationDetailPoint(
                step_index=sidx,
                timestamp=now - (len(specs) - idx) * 28,
                model=mod,
                total_tokens=tot,
                context_delta=dlt,
                input_tokens=tot - cached,
                cache_read_tokens=cached,
                output_tokens=int(tps * strm),
                hit_rate=hr,
                classification=cls,
                reason=rsn,
                ttft_sec=ttft,
                tps=tps,
                streaming_sec=strm,
                is_revert=rev,
                is_compact=cmp_,
                is_account_switch=acc_sw,
                active_media_count=mc,
                active_media_bytes=mb,
            )
        )
    return ConversationDetailReport(
        conversation_id="demo-detail-v722-0000-000000000001",
        short_id="demo72",
        is_subagent=False,
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        total_steps=len(pts),
        today_steps=len(pts),
        peak_context_tokens=473800,
        latest_context_tokens=473800,
        latest_context_delta=131800,
        max_context_tokens=512000,
        compact_count=1,
        cache_hit_steps=7,
        abnormal_miss_steps=0,
        expected_cold_steps=6,
        overall_cacheable_hit_ratio=1.0,
        avg_ttft_sec=20.1,
        p95_ttft_sec=48.2,
        max_ttft_sec=48.2,
        avg_tps=161.8,
        latest_tps=168.0,
        latest_ttft_sec=48.2,
        freshness_remaining_sec=175,
        freshness_invalid_reason="",
        freshness_window_sec=210,
        points=pts,
        title="v7.2.2 跨号改规冷启与大窗校准演示",
        revert_count=0,
    )


def _build_demo_v721_warn_snapshot() -> MonitorSnapshot:
    now = time.time()
    ok_steps = [
        StepRecord(i, now - (10 - i) * 25, "gemini-4-neon-xhigh", 2400, 471400, 620, 473800, 99.5, "hit", "cached")
        for i in range(1, 11)
    ]
    c_big = ConversationSummary(
        conversation_id="demo7210-0000-0000-0000-000000000001",
        short_id="demo71",
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        context_tokens=473800,
        max_context_tokens=512000,
        latest_hit_rate=99.5,
        latest_cached_in=471400,
        latest_uncached_in=2400,
        latest_timestamp=now - 12,
        recent_steps=ok_steps,
        consec_miss=0,
        consec_miss_start_step=0,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=192,
        freshness_invalid_reason="",
        ttft_sec=56.2,
        streaming_sec=4.5,
        tps=198.0,
        shard_tag="dm71",
        speed_status="ok",
        recent_ttfts=[32.0, 38.5, 42.1, 49.0, 56.2],
        recent_tps_list=[205.0, 196.0, 202.0, 194.0, 198.0],
        ttft_avg_sec=43.6,
        ttft_max_sec=56.2,
        large_ctx_ttft_allowance_sec=28.9,
        title="超大上下文与并发告警演示",
    )
    c_rule = ConversationSummary(
        conversation_id="demo7220-0000-0000-0000-000000000002",
        short_id="demo72",
        model="Neon XHigh",
        model_raw="gemini-4-neon-xhigh",
        context_tokens=142800,
        max_context_tokens=512000,
        latest_hit_rate=0.0,
        latest_cached_in=0,
        latest_uncached_in=142800,
        latest_timestamp=now - 19,
        recent_steps=[
            StepRecord(1, now - 75, "gemini-4-neon-xhigh", 2800, 121000, 580, 123800, 97.7, "hit", "cached"),
            StepRecord(2, now - 19, "gemini-4-neon-xhigh", 142800, 0, 460, 142800, 0.0, "expected_cold", "rule_change"),
        ],
        consec_miss=0,
        consec_miss_start_step=0,
        single_miss=False,
        wasted_cold_tokens=0,
        freshness_remaining_sec=None,
        freshness_invalid_reason="",
        ttft_sec=14.6,
        streaming_sec=3.2,
        tps=164.0,
        shard_tag="dm72",
        speed_status="ok",
        recent_ttfts=[11.4, 14.6],
        recent_tps_list=[168.0, 164.0],
        ttft_avg_sec=13.0,
        ttft_max_sec=14.6,
        latest_cache_reason="rule_change",
        title="全局改规冷启演示",
    )
    return MonitorSnapshot(
        generated_at=now,
        level="orange",
        grade="WARN",
        headline="留意预警 · Neon XHigh",
        sub_headline="代理线路疑似断流 (03:42 60s内 12次跨接口断连，非服务端限流) / 多图上传超时 (10m) · demo71 [图13(26.2M)] (2次)，建议压图或/compact",
        active_model="Neon XHigh",
        primary_hit_rate=99.5,
        primary_hit_classification="hit",
        primary_tps=198.0,
        primary_ttft=56.2,
        primary_speed_status="ok",
        min_freshness_sec=192,
        min_freshness_conv="demo71",
        min_freshness_ctx=473800,
        min_freshness_invalid_reason="",
        today_steps=1180,
        daily_steps_warn=1280,
        daily_steps_alert=1600,
        active_conversations=[c_big, c_rule],
        large_freshness_list=[c_big],
        recent_503=0,
        today_503=0,
        recent_timeout=2,
        today_timeout=2,
        recent_403=0,
        today_403=0,
        recent_retry=3,
        today_retry=3,
        recent_events=[],
        wasted_cold_tokens_total=0,
        max_consec_miss=0,
        account=AccountQuotaInfo(
            email_masked="de***o@example.com",
            short_label="de***o",
            tier="demo-tier",
            five_hour_pct=82.0,
            weekly_pct=71.0,
            online=True,
            observed_at=now,
        ),
        sliding_window_min=30,
        freshness_window_sec=210,
        primary_ttft_avg=43.6,
        primary_ttft_max=56.2,
        primary_recent_ttfts=[32.0, 38.5, 42.1, 49.0, 56.2],
        account_today_steps=1180,
        total_today_steps=1240,
        steps_scope_label="de***o",
        resolved_account="de***o",
        heavy_media_timeout_recent=2,
        proxy_outage_recent=12,
        proxy_outage_note="代理线路疑似断流 (03:42 60s内 12次跨接口断连，非服务端限流)",
        daily_steps_limit_mode="adaptive_up",
        daily_steps_limit_note="按历史安全峰值 1400 步自适应上调",
        daily_steps_base_alert=1200,
        account_hist_safe_peak=1400,
    )


def main() -> int:
    _enable_dpi()
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else APP_ROOT / "preview_shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = AntigravityMonitorEngine(read_only=True)
    live_snap = engine.poll(force_account=True)

    with tempfile.TemporaryDirectory() as td:
        cfg_store = ConfigStore(Path(td) / "cfg.json")
        cfg_store.save({"theme": "midnight_blue", "window_x": 120, "window_y": 80, "window_alpha": 0.95})
        app = AntigravityCapsuleApp(
            app_root=APP_ROOT,
            config_store=cfg_store,
            engine=engine,
            enable_tray=False,
            start_minimized_override=False,
        )
        dw = None
        try:
            # 1. Live collapsed capsule (Scheme B Midnight Blue)
            app.render_snapshot(live_snap)
            _capture_widget(app.root, out_dir / "shot_1_live_collapsed_midnight.png")

            # 2. Live expanded panel first-frame (Scheme B Midnight Blue)
            app.set_expanded(True)
            _capture_widget(app.root, out_dir / "shot_2_live_expanded_midnight.png")

            # 3. Alert simulation (Scheme B Midnight Blue - T1 + invalid freshness + slow throttle)
            alert_snap = _build_demo_alert_snapshot()
            app.render_snapshot(alert_snap)
            _capture_widget(app.root, out_dir / "shot_3_alert_expanded_midnight.png")

            # 4. Live expanded panel (Scheme A Obsidian Dark)
            app.set_theme("obsidian_dark")
            app.render_snapshot(live_snap)
            _capture_widget(app.root, out_dir / "shot_4_live_expanded_obsidian.png")

            # 6. Auto-update prompt banner simulation (Scheme B Midnight Blue - notify mode preview)
            from core.updater import UpdateCheckResult
            app.set_theme("midnight_blue")
            app.config["update_mode"] = "notify"
            app.render_snapshot(live_snap)
            app._on_update_check_result(
                UpdateCheckResult(
                    checked_at=time.time(),
                    local_version="7.2.2",
                    remote_version="7.3.0",
                    has_update=True,
                    summary="已恢复掉缓存预警自动销账、识别全局改规则冷启与后台子代理单步瞬抖降噪",
                    released_at="2026-11-22",
                    install_mode="zip",
                    is_manual=True,
                )
            )
            _capture_widget(app.root, out_dir / "shot_6_update_banner_midnight.png")

            # 8. v7.2.2 concurrent heavy-media timeout + proxy outage WARN + rule_change + adaptive steps preview
            app._hide_update_banner()
            warn_v721 = _build_demo_v721_warn_snapshot()
            app.render_snapshot(warn_v721)
            _capture_widget(app.root, out_dir / "shot_8_v721_warn_adaptive_midnight.png")

            # 7. Detail window simulation (multimodal payload + account-switch line + 1109x722 lock)
            demo_rep = _build_demo_detail_report()
            dw = ConversationDetailWindow(
                parent=app.root,
                theme=THEMES["midnight_blue"],
                load_report=lambda _cid: demo_rep,
                on_toggle_pin=lambda _cid: None,
                is_pinned_fn=lambda _cid: False,
            )
            dw.show_conversation(demo_rep.conversation_id, place_near_parent=True)
            dw.win.lift()
            dw.win.attributes("-topmost", True)
            _capture_widget(dw.win, out_dir / "shot_7_detail_window_midnight.png")
        finally:
            if dw is not None:
                try:
                    dw.close()
                except Exception:
                    pass
            app.shutdown()

    # 5. Composite strip showing 80x80, 24x24, and 16x16 real taskbar tray icon scales
    base_png = APP_ROOT / "assets" / "app-icon.png"
    strip = Image.new("RGBA", (320, 152), (20, 26, 38, 255))
    for idx, lvl in enumerate(("green", "orange", "red")):
        x_base = 24 + idx * 100
        strip.alpha_composite(build_tray_icon(base_png, level=lvl, size=80), (x_base, 12))
        strip.alpha_composite(build_tray_icon(base_png, level=lvl, size=24), (x_base + 14, 108))
        strip.alpha_composite(build_tray_icon(base_png, level=lvl, size=16), (x_base + 50, 112))
    strip.save(out_dir / "shot_5_tray_three_states.png", "PNG")
    print(f"Captured 8 verification images in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
