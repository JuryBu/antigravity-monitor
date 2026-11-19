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
    ConversationSummary,
    MonitorSnapshot,
    StepRecord,
)
from ui.capsule_app import AntigravityCapsuleApp
from ui.icon_builder import build_tray_icon


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
        active_conversations=[c1, c2],
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


def main() -> int:
    _enable_dpi()
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else APP_ROOT / "preview_shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = AntigravityMonitorEngine()
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
        finally:
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
    print(f"Captured 5 verification images in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
