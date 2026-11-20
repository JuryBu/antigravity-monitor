"""Borderless Win11 rounded floating capsule & expandable diagnostics dashboard."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config_store import ConfigStore
from core.log_monitor import (
    AntigravityMonitorEngine,
    MonitorSnapshot,
    format_countdown,
    format_delta_tokens,
    format_model_name,
    format_text_summary,
    format_tokens,
    truncate_display_width,
)
from core.startup_manager import is_startup_enabled, set_startup_enabled
from ui.detail_window import ConversationDetailWindow
from ui.themes import THEMES, get_theme

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    """Linearly interpolate between two #RRGGBB colors for smooth visual warning gradients."""
    ratio = max(0.0, min(1.0, float(t)))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(round(r1 + (r2 - r1) * ratio))
    g = int(round(g1 + (g2 - g1) * ratio))
    b = int(round(b1 + (b2 - b1) * ratio))
    return f"#{r:02X}{g:02X}{b:02X}"


def compute_steps_color(steps: int, warn_threshold: int, alert_threshold: int, green_hex: str, red_hex: str) -> str:
    """Green below warn (80%), smooth yellow -> orange -> red from 80% to 100%, locked red >= 100%."""
    alert = max(2, int(alert_threshold))
    warn = max(1, min(int(warn_threshold), alert - 1))
    if steps < warn:
        return green_hex
    if steps >= alert:
        return red_hex
    yellow = "#FACC15"
    orange = "#F59E0B"
    progress = (steps - warn) / float(alert - warn)
    if progress <= 0.5:
        return _lerp_hex(yellow, orange, progress / 0.5)
    return _lerp_hex(orange, red_hex, (progress - 0.5) / 0.5)


def _get_win32_user32() -> Optional[ctypes.WinDLL]:
    if os.name != "nt":
        return None
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetParent.argtypes = [wintypes.HWND]
    u32.GetParent.restype = wintypes.HWND
    u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    u32.GetSystemMetrics.restype = ctypes.c_int
    return u32


def _get_virtual_screen_bounds(root: tk.Tk) -> Tuple[int, int, int, int]:
    """Return (left, top, right, bottom) across multi-monitor Windows setups (supports negative coords)."""
    if os.name == "nt":
        try:
            u32 = _get_win32_user32()
            if u32 is not None:
                vx = int(u32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
                vy = int(u32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
                vw = int(u32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
                vh = int(u32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
                if vw > 0 and vh > 0:
                    return vx, vy, vx + vw, vy + vh
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _apply_win11_rounded_corners(hwnd: int) -> None:
    """Request Windows 11 DWM native rounded corners; silently ignored on older OS."""
    if os.name != "nt" or not hwnd:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(DWMWCP_ROUND),
            wintypes.DWORD(ctypes.sizeof(DWMWCP_ROUND)),
        )
    except Exception:
        pass


class AntigravityCapsuleApp:
    """Two-state floating HUD (compact capsule + expandable 5-section diagnostic panel)."""

    def __init__(
        self,
        app_root: Path,
        config_store: ConfigStore,
        engine: AntigravityMonitorEngine,
        enable_tray: bool = True,
        start_minimized_override: Optional[bool] = None,
        on_restart: Optional[Callable[[], None]] = None,
    ) -> None:
        self.app_root = Path(app_root).resolve()
        self.config_store = config_store
        self.engine = engine
        self.config: Dict[str, Any] = self.config_store.load()
        self.engine.update_config(self.config)
        self.theme: Dict[str, str] = get_theme(self.config["theme"])
        self._on_restart = on_restart
        self._enable_tray = enable_tray
        self._start_minimized_override = start_minimized_override

        self._expanded: bool = False
        self._visible: bool = True
        self._closing: bool = False
        self._latest_snapshot: Optional[MonitorSnapshot] = None
        self._last_steps_pct: int = 0
        self._last_steps_color: str = self.theme["green"]
        self._startup_cached: bool = is_startup_enabled()
        self._startup_busy: bool = False

        self._drag_origin_x: int = 0
        self._drag_origin_y: int = 0
        self._win_origin_x: int = 0
        self._win_origin_y: int = 0
        self._drag_moved: bool = False

        self._after_ids: List[str] = []
        self._snapshot_queue: "queue.Queue[MonitorSnapshot]" = queue.Queue(maxsize=2)
        self._stop_event = threading.Event()
        self._force_poll_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self.tray = None

        self.root = tk.Tk()
        self.root.title("反重力监控")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.config["topmost"]))
        self.root.attributes("-alpha", float(self.config["window_alpha"]))
        self.root.configure(bg=self.theme["border"])

        ico_path = self.app_root / "assets" / "app-icon.ico"
        if ico_path.is_file():
            try:
                self.root.iconbitmap(default=str(ico_path))
            except tk.TclError:
                pass

        self._outer = tk.Frame(self.root, bg=self.theme["border"], padx=1, pady=1)
        self._outer.pack(fill="both", expand=True)
        self._surface = tk.Frame(self._outer, bg=self.theme["panel_bg"])
        self._surface.pack(fill="both", expand=True)

        self._var_topmost = tk.BooleanVar(value=bool(self.config["topmost"]))
        self._var_startup = tk.BooleanVar(value=self._startup_cached)
        self._var_notifications = tk.BooleanVar(value=bool(self.config["notifications_enabled"]))
        self._var_start_min = tk.BooleanVar(value=bool(self.config["start_minimized"]))

        self._tooltip_win: Optional[tk.Toplevel] = None
        self._conv_hover_texts: List[str] = []
        self._conv_row_ids: List[str] = []
        self._hovered_row_idx: Optional[int] = None
        self._pinned_conv_id: Optional[str] = None
        self._fresh_hover_text: str = ""
        self._speed_hover_text: str = ""
        self._detail_win: Optional[ConversationDetailWindow] = None
        self._conv_filter: str = str(self.config.get("conv_filter", "main"))
        self._conv_list_rows: int = int(self.config.get("conv_list_rows", 5))
        self._conv_row_px: int = 24
        self._resize_origin_y: int = 0
        self._resize_origin_rows: int = self._conv_list_rows
        self._scroll_drag_origin_y: int = 0
        self._scroll_drag_origin_top: float = 0.0

        self._build_collapsed_bar()
        self._build_expanded_panel()
        self._build_context_menu()
        self._bind_recursive_context_menu(self._details)
        self._apply_theme_colors()

        self.root.update_idletasks()
        self._place_initial_window()
        self._apply_dwm_rounding()

        start_min = self.config["start_minimized"] if start_minimized_override is None else start_minimized_override
        if start_min and enable_tray:
            self.hide_window()

        self.root.protocol("WM_DELETE_WINDOW", self.hide_window if enable_tray else self.shutdown)

    # ------------------------------------------------------------------ UI construction
    def _build_collapsed_bar(self) -> None:
        self._bar = tk.Frame(self._surface, bg=self.theme["panel_bg"], padx=10, pady=6)
        self._bar.pack(fill="x")

        self._dot_canvas = tk.Canvas(self._bar, width=12, height=12, highlightthickness=0, bd=0, bg=self.theme["panel_bg"])
        self._dot_canvas.pack(side="left", padx=(0, 6))
        self._dot_oval = self._dot_canvas.create_oval(2, 2, 10, 10, fill=self.theme["green"], outline="")

        self._lbl_bar_status = tk.Label(self._bar, text="T0 健康", font=(UI_FONT, 9, "bold"), bg=self.theme["panel_bg"], fg=self.theme["green"])
        self._lbl_bar_status.pack(side="left")

        self._lbl_bar_cache_k = tk.Label(self._bar, text=" 缓存 ", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["text_secondary"])
        self._lbl_bar_cache_k.pack(side="left")
        self._lbl_bar_cache_v = tk.Label(self._bar, text="—", font=(MONO_FONT, 10, "bold"), bg=self.theme["panel_bg"], fg=self.theme["green"])
        self._lbl_bar_cache_v.pack(side="left")

        self._sep1 = tk.Label(self._bar, text=" │ ", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["divider"])
        self._sep1.pack(side="left")
        self._lbl_bar_tps = tk.Label(self._bar, text="— t/s", font=(MONO_FONT, 9, "bold"), bg=self.theme["panel_bg"], fg=self.theme["cyan"])
        self._lbl_bar_tps.pack(side="left")

        self._sep2 = tk.Label(self._bar, text=" │ ", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["divider"])
        self._sep2.pack(side="left")
        self._lbl_bar_fresh = tk.Label(self._bar, text="保鲜 闲置", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["text_primary"])
        self._lbl_bar_fresh.pack(side="left")

        self._sep3 = tk.Label(self._bar, text=" │ ", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["divider"])
        self._sep3.pack(side="left")
        self._lbl_bar_acc = tk.Label(self._bar, text="离线", font=(MONO_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["text_secondary"])
        self._lbl_bar_acc.pack(side="left")

        self._sep4 = tk.Label(self._bar, text=" │ ", font=(UI_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["divider"])
        self._sep4.pack(side="left")
        self._lbl_bar_steps = tk.Label(self._bar, text="0/1000", font=(MONO_FONT, 9), bg=self.theme["panel_bg"], fg=self.theme["text_secondary"])
        self._lbl_bar_steps.pack(side="left", padx=(0, 8))

        self._btn_hide = tk.Label(self._bar, text="—", font=(UI_FONT, 9, "bold"), padx=5, pady=1, cursor="hand2")
        self._btn_hide.pack(side="right", padx=(2, 0))
        self._btn_hide.bind("<Button-1>", lambda _e: self.hide_window())

        self._btn_expand = tk.Label(self._bar, text="▾", font=(UI_FONT, 10, "bold"), padx=5, pady=0, cursor="hand2")
        self._btn_expand.pack(side="right", padx=(2, 2))
        self._btn_expand.bind("<Button-1>", lambda _e: self.toggle_expand())

        drag_targets = (
            self._bar,
            self._dot_canvas,
            self._lbl_bar_status,
            self._lbl_bar_cache_k,
            self._lbl_bar_cache_v,
            self._sep1,
            self._lbl_bar_tps,
            self._sep2,
            self._lbl_bar_fresh,
            self._sep3,
            self._lbl_bar_acc,
            self._sep4,
            self._lbl_bar_steps,
        )
        for widget in drag_targets:
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)
            widget.bind("<ButtonRelease-1>", self._on_drag_end)
            widget.bind("<Button-3>", self._show_context_menu)

    def _build_expanded_panel(self) -> None:
        self._details = tk.Frame(self._surface, bg=self.theme["panel_bg"], padx=10, pady=0)

        self._top_sep = tk.Frame(self._details, height=1, bg=self.theme["border_soft"])
        self._top_sep.pack(fill="x", pady=(0, 8))

        # --- Module 1: Health Header + Account Quota + Daily Steps ---
        self._card_head = tk.Frame(self._details, padx=10, pady=8, highlightthickness=1)
        self._card_head.pack(fill="x", pady=(0, 8))

        head_row1 = tk.Frame(self._card_head)
        head_row1.pack(fill="x")
        self._lbl_headline = tk.Label(head_row1, text="T0 健康 · 等待数据", font=(UI_FONT, 10, "bold"), anchor="w")
        self._lbl_headline.pack(side="left", fill="x", expand=True)
        self._lbl_clock = tk.Label(head_row1, text="--:--:--", font=(MONO_FONT, 9), anchor="e")
        self._lbl_clock.pack(side="right")

        head_row2 = tk.Frame(self._card_head)
        head_row2.pack(fill="x", pady=(5, 4))
        self._lbl_acc_pill = tk.Label(head_row2, text="离线", font=(MONO_FONT, 9, "bold"), padx=6, pady=1)
        self._lbl_acc_pill.pack(side="left", padx=(0, 8))
        self._lbl_acc_5h = tk.Label(head_row2, text="5h —", font=(MONO_FONT, 9))
        self._lbl_acc_5h.pack(side="left", padx=(0, 10))
        self._lbl_acc_wk = tk.Label(head_row2, text="周 —", font=(MONO_FONT, 9))
        self._lbl_acc_wk.pack(side="left")

        self._lbl_theme_tag = tk.Label(head_row2, text="主题:星蓝 ⇄", font=(UI_FONT, 8), padx=6, pady=1, cursor="hand2")
        self._lbl_theme_tag.pack(side="right")
        self._lbl_theme_tag.bind("<Button-1>", lambda _e: self.cycle_theme())

        head_row3 = tk.Frame(self._card_head)
        head_row3.pack(fill="x", pady=(2, 3))
        self._lbl_steps_title = tk.Label(head_row3, text="今日请求步数保护线", font=(UI_FONT, 8))
        self._lbl_steps_title.pack(side="left")
        self._lbl_steps_val = tk.Label(head_row3, text="0 / 1000 (0%)", font=(MONO_FONT, 8, "bold"))
        self._lbl_steps_val.pack(side="right")

        self._steps_canvas = tk.Canvas(self._card_head, height=7, highlightthickness=0, bd=0)
        self._steps_canvas.pack(fill="x")
        self._steps_canvas.bind("<Configure>", lambda _e: self._draw_steps_progress(self._last_steps_pct, self._last_steps_color))

        # --- Module 2: 2-Column Diagnostics (Real-time Speed + Large Context Freshness) ---
        self._mid_row = tk.Frame(self._details)
        self._mid_row.pack(fill="x", pady=(0, 8))
        self._mid_row.grid_columnconfigure(0, weight=1, uniform="mid")
        self._mid_row.grid_columnconfigure(1, weight=1, uniform="mid")

        self._card_speed = tk.Frame(self._mid_row, padx=10, pady=8, highlightthickness=1)
        self._card_speed.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        sp_top = tk.Frame(self._card_speed)
        sp_top.pack(fill="x")
        self._lbl_speed_title = tk.Label(sp_top, text="实时吞吐诊断", font=(UI_FONT, 8))
        self._lbl_speed_title.pack(side="left")
        self._lbl_pin_badge = tk.Label(sp_top, text="", font=(MONO_FONT, 7, "bold"), padx=4, pady=0, cursor="hand2")
        self._lbl_pin_badge.bind("<Button-1>", lambda _e: self.set_pinned_conversation(None))
        self._lbl_models_badge = tk.Label(sp_top, text="", font=(UI_FONT, 7, "bold"), padx=4, pady=0)
        self._lbl_ttft = tk.Label(sp_top, text="TTFT —", font=(MONO_FONT, 8))
        self._lbl_ttft.pack(side="right")
        sp_bot = tk.Frame(self._card_speed)
        sp_bot.pack(fill="x", pady=(4, 0))
        self._lbl_tps_big = tk.Label(sp_bot, text="— tok/s", font=(MONO_FONT, 12, "bold"))
        self._lbl_tps_big.pack(side="left")
        self._lbl_speed_badge = tk.Label(sp_bot, text="待采样", font=(UI_FONT, 8, "bold"), padx=6, pady=1)
        self._lbl_speed_badge.pack(side="right")
        self._lbl_ttft_stats = tk.Label(self._card_speed, text="近10步 TTFT 待采样", font=(MONO_FONT, 8), anchor="w")
        self._lbl_ttft_stats.pack(fill="x", pady=(3, 0))
        for sw in (self._card_speed, sp_top, sp_bot, self._lbl_speed_title, self._lbl_models_badge, self._lbl_ttft, self._lbl_tps_big, self._lbl_speed_badge, self._lbl_ttft_stats):
            sw.bind("<Enter>", self._show_speed_tooltip)
            sw.bind("<Leave>", lambda _e: self._hide_conv_tooltip())

        self._card_fresh = tk.Frame(self._mid_row, padx=10, pady=8, highlightthickness=1)
        self._card_fresh.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        fr_top = tk.Frame(self._card_fresh)
        fr_top.pack(fill="x")
        self._lbl_fresh_title = tk.Label(fr_top, text="大上下文保鲜 (≥100K)", font=(UI_FONT, 8))
        self._lbl_fresh_title.pack(side="left")
        self._lbl_fresh_count = tk.Label(fr_top, text="", font=(UI_FONT, 7, "bold"), padx=5, pady=0)
        self._lbl_fresh_sub = tk.Label(self._card_fresh, text="当前无活跃大对话", font=(MONO_FONT, 8), anchor="w")
        self._lbl_fresh_sub.pack(fill="x", pady=(4, 0))
        self._lbl_fresh_big = tk.Label(self._card_fresh, text="闲置", font=(MONO_FONT, 11, "bold"), anchor="w")
        self._lbl_fresh_big.pack(fill="x", pady=(2, 0))
        for fw in (self._card_fresh, fr_top, self._lbl_fresh_title, self._lbl_fresh_count, self._lbl_fresh_sub, self._lbl_fresh_big):
            fw.bind("<Enter>", self._show_freshness_tooltip)
            fw.bind("<Leave>", lambda _e: self._hide_conv_tooltip())

        # --- Module 3: Active Conversations (Categorized + Scrollable + Resizable) ---
        self._card_convs = tk.Frame(self._details, padx=10, pady=8, highlightthickness=1)
        self._card_convs.pack(fill="x", pady=(0, 8))
        conv_hdr = tk.Frame(self._card_convs)
        conv_hdr.pack(fill="x", pady=(0, 2))
        self._lbl_convs_title = tk.Label(conv_hdr, text="活跃对话 · TTFT 与缓存", font=(UI_FONT, 9, "bold"))
        self._lbl_convs_title.pack(side="left")

        self._filter_bar = tk.Frame(conv_hdr)
        self._filter_bar.pack(side="right")
        self._filter_buttons: Dict[str, tk.Label] = {}
        for f_key, f_lbl in (("main", "主对话"), ("subagent", "子代理"), ("all", "全部")):
            fb = tk.Label(
                self._filter_bar,
                text=f_lbl,
                font=(UI_FONT, 8),
                padx=6,
                pady=1,
                cursor="hand2",
            )
            fb.pack(side="left", padx=(4, 0))
            fb.bind("<Button-1>", lambda _e, k=f_key: self.set_conv_filter(k))
            self._filter_buttons[f_key] = fb

        self._lbl_convs_hint = tk.Label(
            self._card_convs,
            text="点行看长线曲线 · 悬停看逐拍请求体变化 │ 绿命中 紫压缩 橙冷启 红异常",
            font=(UI_FONT, 7),
            anchor="w",
        )
        self._lbl_convs_hint.pack(fill="x", pady=(0, 4))

        self._convs_viewport_wrap = tk.Frame(self._card_convs)
        self._convs_viewport_wrap.pack(fill="x")
        self._conv_viewport = tk.Canvas(
            self._convs_viewport_wrap,
            height=self._conv_list_rows * self._conv_row_px,
            highlightthickness=0,
            bd=0,
        )
        self._conv_viewport.pack(side="left", fill="both", expand=True)

        self._conv_scrollbar = tk.Canvas(
            self._convs_viewport_wrap,
            width=7,
            height=self._conv_list_rows * self._conv_row_px,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._conv_scrollbar.pack(side="right", fill="y", padx=(4, 0))
        self._conv_scrollbar.bind("<ButtonPress-1>", self._on_scrollbar_press)
        self._conv_scrollbar.bind("<B1-Motion>", self._on_scrollbar_drag)
        self._conv_scrollbar.bind("<Configure>", lambda _e: self._sync_custom_scrollbar())

        self._convs_table = tk.Frame(self._conv_viewport)
        self._convs_table.grid_columnconfigure(1, weight=1)
        self._conv_win_id = self._conv_viewport.create_window((0, 0), window=self._convs_table, anchor="nw")
        self._conv_viewport.bind(
            "<Configure>",
            lambda e: (self._conv_viewport.itemconfigure(self._conv_win_id, width=e.width), self._sync_custom_scrollbar()),
        )
        self._convs_table.bind("<Configure>", lambda _e: self._update_conv_scrollregion())

        for sc_w in (self._card_convs, self._convs_viewport_wrap, self._conv_viewport, self._convs_table, self._conv_scrollbar):
            sc_w.bind("<MouseWheel>", self._on_conv_mousewheel)

        self._conv_rows: List[Dict[str, Any]] = []
        self._lbl_convs_empty = tk.Label(self._convs_table, text="当前分类暂无活跃对话记录", font=(UI_FONT, 8), anchor="w")
        self._lbl_convs_empty.bind("<MouseWheel>", self._on_conv_mousewheel)
        for r_idx in range(24):
            lbl_id = tk.Label(self._convs_table, text="", font=(MONO_FONT, 9, "bold"), anchor="w", cursor="hand2")
            lbl_title = tk.Label(self._convs_table, text="", font=(UI_FONT, 8), anchor="w", cursor="hand2")
            lbl_mod = tk.Label(self._convs_table, text="", font=(UI_FONT, 8), anchor="w", cursor="hand2")
            lbl_ctx = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=6, cursor="hand2")
            ttft_cv = tk.Canvas(self._convs_table, width=68, height=16, highlightthickness=0, bd=0, cursor="hand2")
            lbl_ttft = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=5, cursor="hand2")
            bar_cv = tk.Canvas(self._convs_table, width=98, height=16, highlightthickness=0, bd=0, cursor="hand2")
            lbl_hit = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8, "bold"), anchor="e", width=6, cursor="hand2")
            lbl_spd = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=7, cursor="hand2")
            for hover_w in (lbl_id, lbl_title, lbl_mod, lbl_ctx, ttft_cv, lbl_ttft, bar_cv, lbl_hit, lbl_spd):
                hover_w.bind("<Enter>", lambda e, idx=r_idx: self._on_conv_row_enter(idx, e))
                hover_w.bind("<Leave>", lambda _e, idx=r_idx: self._on_conv_row_leave(idx))
                hover_w.bind("<Button-1>", lambda _e, idx=r_idx: self._on_conv_row_click(idx))
                hover_w.bind("<Button-3>", lambda e, idx=r_idx: self._on_conv_row_right_click(idx, e))
                hover_w.bind("<MouseWheel>", self._on_conv_mousewheel)
            self._conv_rows.append(
                {
                    "row": r_idx,
                    "id": lbl_id,
                    "title": lbl_title,
                    "model": lbl_mod,
                    "ctx": lbl_ctx,
                    "ttft_canvas": ttft_cv,
                    "ttft_val": lbl_ttft,
                    "canvas": bar_cv,
                    "hit": lbl_hit,
                    "speed": lbl_spd,
                }
            )

        # --- Module 4: Sliding-Window Language Server Log Monitor ---
        self._card_logs = tk.Frame(self._details, padx=10, pady=8, highlightthickness=1)
        self._card_logs.pack(fill="x", pady=(0, 8))
        log_hdr = tk.Frame(self._card_logs)
        log_hdr.pack(fill="x", pady=(0, 6))
        self._lbl_logs_title = tk.Label(log_hdr, text="底层日志滑动窗监控（近 30 分钟 / 今日）", font=(UI_FONT, 9, "bold"))
        self._lbl_logs_title.pack(side="left")

        self._log_grid = tk.Frame(self._card_logs)
        self._log_grid.pack(fill="x")
        self._log_cells: Dict[str, Dict[str, Any]] = {}
        cell_specs = [
            ("503", "503 限流"),
            ("timeout", "5m 超时"),
            ("403", "403 封禁"),
            ("retry", "代理重试"),
        ]
        for idx, (key, title) in enumerate(cell_specs):
            self._log_grid.grid_columnconfigure(idx, weight=1, uniform="lg")
            box = tk.Frame(self._log_grid, padx=6, pady=5, highlightthickness=1)
            box.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 3, 0 if idx == 3 else 3))
            lbl_t = tk.Label(box, text=title, font=(UI_FONT, 8))
            lbl_t.pack()
            lbl_v = tk.Label(box, text="0 / 0", font=(MONO_FONT, 10, "bold"))
            lbl_v.pack(pady=(2, 0))
            self._log_cells[key] = {"box": box, "title": lbl_t, "val": lbl_v}

        self._banner_frame = tk.Frame(self._card_logs, padx=8, pady=5, highlightthickness=1)
        self._banner_frame.pack(fill="x", pady=(6, 0))
        self._lbl_banner = tk.Label(self._banner_frame, text="纯本地只读监控运行中", font=(UI_FONT, 8), anchor="w", justify="left", wraplength=520)
        self._lbl_banner.pack(fill="x")

        # --- Module 5: Quick Action Bar + Bottom Height Resize Grip ---
        self._actions_row = tk.Frame(self._details, pady=2)
        self._actions_row.pack(fill="x", pady=(0, 4))
        for i in range(5):
            self._actions_row.grid_columnconfigure(i, weight=1, uniform="act")

        self._action_buttons: Dict[str, tk.Label] = {}
        btn_specs = [
            ("topmost", "置顶:开", self.toggle_topmost),
            ("notify", "通知:开", self.toggle_notifications),
            ("copy", "复制摘要", self.copy_summary_to_clipboard),
            ("logs", "日志目录", self.open_logs_folder),
            ("quit", "退出", self.shutdown),
        ]
        for i, (key, text, cmd) in enumerate(btn_specs):
            btn = tk.Label(
                self._actions_row,
                text=text,
                font=(UI_FONT, 8),
                padx=6,
                pady=5,
                cursor="hand2",
                highlightthickness=1,
            )
            btn.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 3, 0 if i == 4 else 3))
            btn.bind("<Button-1>", lambda _e, fn=cmd: fn())
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=self.theme["btn_hover"]))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=self.theme["btn_bg"]))
            self._action_buttons[key] = btn

        self._resize_grip = tk.Canvas(
            self._details,
            height=9,
            highlightthickness=0,
            bd=0,
            cursor="sb_v_double_arrow",
        )
        self._resize_grip.pack(fill="x", pady=(0, 3))
        self._resize_grip.bind("<ButtonPress-1>", self._on_resize_start)
        self._resize_grip.bind("<B1-Motion>", self._on_resize_drag)
        self._resize_grip.bind("<ButtonRelease-1>", self._on_resize_end)
        self._resize_grip.bind("<Configure>", lambda _e: self._draw_resize_grip())

    def _bind_recursive_context_menu(self, root_widget: tk.Widget) -> None:
        excluded = set(self._action_buttons.values())
        for row_ui in self._conv_rows:
            for k in ("id", "title", "model", "ctx", "ttft_canvas", "ttft_val", "canvas", "hit", "speed"):
                excluded.add(row_ui[k])
        stack = [root_widget]
        while stack:
            w = stack.pop()
            if w in excluded:
                continue
            w.bind("<Button-3>", self._show_context_menu)
            stack.extend(w.winfo_children())

    def _build_context_menu(self) -> None:
        self._ctx_menu = tk.Menu(self.root, tearoff=False)
        self._ctx_menu.add_command(label="展开 / 收起体检详情", command=self.toggle_expand)
        self._ctx_menu.add_command(label="切换主题（午夜星蓝 / 深炭极简）", command=self.cycle_theme)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_checkbutton(label="窗口置顶", variable=self._var_topmost, command=self.toggle_topmost)
        self._ctx_menu.add_checkbutton(label="开机自启动", variable=self._var_startup, command=self.toggle_startup)
        self._ctx_menu.add_checkbutton(label="启动时缩进托盘", variable=self._var_start_min, command=self.toggle_start_minimized)
        self._ctx_menu.add_checkbutton(label="启用异常通知", variable=self._var_notifications, command=self.toggle_notifications)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="立即刷新采样", command=self.request_immediate_poll)
        self._ctx_menu.add_command(label="复制文字体检摘要", command=self.copy_summary_to_clipboard)
        self._ctx_menu.add_command(label="打开日志目录", command=self.open_logs_folder)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="隐藏到系统托盘", command=self.hide_window)
        self._ctx_menu.add_command(label="重启监控", command=self.restart_app)
        self._ctx_menu.add_command(label="退出", command=self.shutdown)

    def _apply_theme_colors(self) -> None:
        t = self.theme
        self.root.configure(bg=t["border"])
        self._outer.configure(bg=t["border"])
        self._surface.configure(bg=t["panel_bg"])
        self._bar.configure(bg=t["panel_bg"])
        self._dot_canvas.configure(bg=t["panel_bg"])
        self._lbl_bar_status.configure(bg=t["panel_bg"])
        self._lbl_bar_cache_k.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._lbl_bar_cache_v.configure(bg=t["panel_bg"])
        for sep in (self._sep1, self._sep2, self._sep3, self._sep4):
            sep.configure(bg=t["panel_bg"], fg=t["divider"])
        self._lbl_bar_tps.configure(bg=t["panel_bg"])
        self._lbl_bar_fresh.configure(bg=t["panel_bg"])
        self._lbl_bar_acc.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._lbl_bar_steps.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        for b in (self._btn_expand, self._btn_hide):
            b.configure(bg=t["panel_bg"], fg=t["text_secondary"])

        self._details.configure(bg=t["panel_bg"])
        self._top_sep.configure(bg=t["border_soft"])
        self._mid_row.configure(bg=t["panel_bg"])
        self._actions_row.configure(bg=t["panel_bg"])

        for card in (self._card_head, self._card_speed, self._card_fresh, self._card_convs, self._card_logs):
            card.configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
            for child in card.winfo_children():
                if isinstance(child, tk.Frame) and child not in (self._banner_frame,):
                    child.configure(bg=t["card_bg"])

        self._lbl_headline.configure(bg=t["card_bg"])
        self._lbl_clock.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_acc_pill.configure(bg=t["card_alt_bg"], fg=t["text_primary"])
        self._lbl_acc_5h.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_acc_wk.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_theme_tag.configure(
            bg=t["btn_bg"],
            fg=t["text_secondary"],
            text="主题:星蓝 ⇄" if self.config["theme"] == "midnight_blue" else "主题:深炭 ⇄",
        )
        self._lbl_steps_title.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_steps_val.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._steps_canvas.configure(bg=t["card_bg"])

        self._lbl_speed_title.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_pin_badge.configure(bg=t["btn_hover"], fg=t["cyan"])
        self._lbl_models_badge.configure(bg=t["card_alt_bg"], fg=t["cyan"])
        self._lbl_ttft.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_tps_big.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._lbl_speed_badge.configure(bg=t["green_bg"], fg=t["green"])
        self._lbl_ttft_stats.configure(bg=t["card_bg"], fg=t["text_muted"])

        self._lbl_fresh_title.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_fresh_count.configure(bg=t["card_alt_bg"], fg=t["cyan"])
        self._lbl_fresh_sub.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_fresh_big.configure(bg=t["card_bg"], fg=t["text_primary"])

        self._lbl_convs_title.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._lbl_convs_hint.configure(bg=t["card_bg"], fg=t["text_muted"])
        self._filter_bar.configure(bg=t["card_bg"])
        self._refresh_filter_buttons()
        self._convs_viewport_wrap.configure(bg=t["card_bg"])
        self._conv_viewport.configure(bg=t["card_bg"])
        self._conv_scrollbar.configure(bg=t["card_bg"])
        self._convs_table.configure(bg=t["card_bg"])
        self._lbl_convs_empty.configure(bg=t["card_bg"], fg=t["text_muted"])
        for row_ui in self._conv_rows:
            row_ui["id"].configure(bg=t["card_bg"], fg=t["text_primary"])
            row_ui["title"].configure(bg=t["card_bg"], fg=t["text_primary"])
            row_ui["model"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["ctx"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["ttft_canvas"].configure(bg=t["card_bg"])
            row_ui["ttft_val"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["canvas"].configure(bg=t["card_bg"])
            row_ui["hit"].configure(bg=t["card_bg"])
            row_ui["speed"].configure(bg=t["card_bg"])
        self._resize_grip.configure(bg=t["panel_bg"])
        self._draw_resize_grip()
        self._sync_custom_scrollbar()

        self._lbl_logs_title.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._log_grid.configure(bg=t["card_bg"])
        for cell in self._log_cells.values():
            cell["box"].configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
            cell["title"].configure(bg=t["card_alt_bg"], fg=t["text_secondary"])
            cell["val"].configure(bg=t["card_alt_bg"])

        self._banner_frame.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._lbl_banner.configure(bg=t["card_alt_bg"], fg=t["text_secondary"])

        for btn in self._action_buttons.values():
            btn.configure(bg=t["btn_bg"], fg=t["btn_text"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])

        self._sync_action_button_labels()

    # ------------------------------------------------------------------ Window geometry & drag
    def _apply_dwm_rounding(self) -> None:
        try:
            self.root.update_idletasks()
            hwnd = 0
            u32 = _get_win32_user32()
            if u32 is not None:
                hwnd = int(u32.GetParent(wintypes.HWND(self.root.winfo_id())) or 0)
            if not hwnd:
                hwnd = int(self.root.winfo_id())
            _apply_win11_rounded_corners(hwnd)
        except Exception:
            pass

    def _place_initial_window(self) -> None:
        vx, vy, vr, vb = _get_virtual_screen_bounds(self.root)
        sw = self.root.winfo_screenwidth()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = self.config.get("window_x")
        y = self.config.get("window_y")
        if x is None or y is None:
            x = max(12, sw - w - 24)
            y = 22
        x = max(vx + 4, min(vr - w - 4, int(x)))
        y = max(vy + 4, min(vb - h - 44, int(y)))
        self.root.geometry(f"+{x}+{y}")

    def _clamp_window_to_screen(self) -> None:
        self.root.update_idletasks()
        vx, vy, vr, vb = _get_virtual_screen_bounds(self.root)
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        cur_x = self.root.winfo_x()
        cur_y = self.root.winfo_y()
        new_x = max(vx + 4, min(vr - w - 4, cur_x))
        new_y = max(vy + 4, min(vb - h - 44, cur_y))
        self.root.geometry(f"+{new_x}+{new_y}")

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_origin_x = event.x_root
        self._drag_origin_y = event.y_root
        self._win_origin_x = self.root.winfo_x()
        self._win_origin_y = self.root.winfo_y()
        self._drag_moved = False

    def _on_drag_move(self, event: tk.Event) -> None:
        dx = event.x_root - self._drag_origin_x
        dy = event.y_root - self._drag_origin_y
        if abs(dx) >= 4 or abs(dy) >= 4:
            self._drag_moved = True
        if self._drag_moved:
            self.root.geometry(f"+{self._win_origin_x + dx}+{self._win_origin_y + dy}")

    def _on_drag_end(self, _event: tk.Event) -> None:
        if self._drag_moved:
            self._clamp_window_to_screen()
            self.config["window_x"] = self.root.winfo_x()
            self.config["window_y"] = self.root.winfo_y()
            self.config_store.save(self.config)
        else:
            self.toggle_expand()

    def _show_context_menu(self, event: tk.Event) -> None:
        self._var_topmost.set(bool(self.config["topmost"]))
        self._var_startup.set(self._startup_cached)
        self._var_start_min.set(bool(self.config["start_minimized"]))
        self._var_notifications.set(bool(self.config["notifications_enabled"]))
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    # ------------------------------------------------------------------ Actions
    def dispatch_to_ui(self, fn: Callable[[], None]) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    def show_window(self) -> None:
        if self._closing:
            return
        self._visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", bool(self.config["topmost"]))
        self.root.lift()
        self._clamp_window_to_screen()
        self._apply_dwm_rounding()

    def _close_detail_window(self) -> None:
        if self._detail_win is not None:
            dw = self._detail_win
            self._detail_win = None
            dw.close()

    def hide_window(self) -> None:
        if self._closing:
            return
        self._visible = False
        self._hide_conv_tooltip()
        self._close_detail_window()
        self.root.withdraw()

    def toggle_visible(self) -> None:
        if self._visible:
            self.hide_window()
        else:
            self.show_window()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        if self._expanded:
            self._details.pack(fill="both", expand=True)
            self._btn_expand.configure(text="▴")
        else:
            self._hide_conv_tooltip()
            self._close_detail_window()
            self._details.pack_forget()
            self._btn_expand.configure(text="▾")
        self._clamp_window_to_screen()
        self._apply_dwm_rounding()
        self.root.update_idletasks()
        if self._latest_snapshot is not None:
            self.render_snapshot(self._latest_snapshot)

    def toggle_expand(self) -> None:
        if not self._visible:
            self.show_window()
        self.set_expanded(not self._expanded)

    def set_theme(self, theme_name: str) -> None:
        if theme_name not in THEMES:
            return
        self.config["theme"] = theme_name
        self.config_store.save(self.config)
        self.theme = get_theme(theme_name)
        self._apply_theme_colors()
        if self._detail_win is not None:
            self._detail_win.apply_theme(self.theme)
        if self._latest_snapshot is not None:
            self.render_snapshot(self._latest_snapshot)
        if self.tray is not None:
            self.tray.refresh_menu()

    def cycle_theme(self) -> None:
        next_theme = "obsidian_dark" if self.config["theme"] == "midnight_blue" else "midnight_blue"
        self.set_theme(next_theme)

    def toggle_topmost(self) -> None:
        self.config["topmost"] = not bool(self.config["topmost"])
        self._var_topmost.set(self.config["topmost"])
        self.root.attributes("-topmost", self.config["topmost"])
        self.config_store.save(self.config)
        self._sync_action_button_labels()
        if self.tray is not None:
            self.tray.refresh_menu()

    def toggle_notifications(self) -> None:
        self.config["notifications_enabled"] = not bool(self.config["notifications_enabled"])
        self._var_notifications.set(self.config["notifications_enabled"])
        self.config_store.save(self.config)
        self._sync_action_button_labels()
        if self.tray is not None:
            self.tray.refresh_menu()

    def toggle_start_minimized(self) -> None:
        self.config["start_minimized"] = not bool(self.config["start_minimized"])
        self._var_start_min.set(self.config["start_minimized"])
        self.config_store.save(self.config)
        if self.tray is not None:
            self.tray.refresh_menu()

    def toggle_startup(self) -> None:
        """Toggle Windows Startup shortcut asynchronously so PowerShell COM never blocks UI."""
        if self._startup_busy:
            return
        self._startup_busy = True
        target_state = not self._startup_cached

        def _worker() -> None:
            err_msg = ""
            actual = self._startup_cached
            try:
                actual = set_startup_enabled(target_state, self.app_root)
            except Exception as exc:
                err_msg = str(exc)
                actual = is_startup_enabled()

            def _finish() -> None:
                self._startup_busy = False
                self._startup_cached = actual
                self._var_startup.set(actual)
                if self.tray is not None:
                    self.tray.refresh_menu()
                    if err_msg:
                        self.tray.notify("开机自启切换失败", err_msg[:180])
                elif err_msg and self._expanded:
                    self._lbl_banner.configure(text=f"开机自启切换失败: {err_msg[:80]}")

            self.dispatch_to_ui(_finish)

        threading.Thread(target=_worker, name="StartupToggleThread", daemon=True).start()

    def _sync_action_button_labels(self) -> None:
        self._action_buttons["topmost"].configure(text=f"置顶:{'开' if self.config['topmost'] else '关'}")
        self._action_buttons["notify"].configure(text=f"通知:{'开' if self.config['notifications_enabled'] else '关'}")

    def copy_summary_to_clipboard(self) -> None:
        if self._latest_snapshot is None:
            return
        text = format_text_summary(self._latest_snapshot)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
        except tk.TclError:
            return
        btn = self._action_buttons["copy"]
        btn.configure(text="已复制 ✓", fg=self.theme["green"])
        aid = self.root.after(1500, lambda: btn.configure(text="复制摘要", fg=self.theme["btn_text"]) if not self._closing else None)
        self._after_ids.append(aid)

    def open_logs_folder(self) -> None:
        log_dir = self.engine.ls_log_path.parent
        if not log_dir.exists():
            log_dir = self.engine.brain_dir
        if log_dir.exists() and os.name == "nt":
            try:
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
            except OSError:
                pass

    def request_immediate_poll(self) -> None:
        self._force_poll_event.set()

    def restart_app(self) -> None:
        cb = self._on_restart
        self.shutdown()
        if cb is not None:
            cb()

    # ------------------------------------------------------------------ Rendering
    def render_snapshot(self, snap: MonitorSnapshot) -> None:
        self._latest_snapshot = snap
        t = self.theme
        if snap.grade == "IDLE":
            level_color = t["cyan"]
        elif snap.level == "green":
            level_color = t["green"]
        elif snap.level == "orange":
            level_color = t["orange"]
        else:
            level_color = t["red"]

        # Collapsed capsule bar
        self._dot_canvas.itemconfig(self._dot_oval, fill=level_color)
        short_grade = {
            "IDLE": "离线待机",
            "T0": "T0 健康",
            "WARN": "留意预警",
            "T1": "T1 掉缓存",
            "T2": "T2 限流",
            "T3": "T3 封禁",
        }.get(snap.grade, snap.grade)
        self._lbl_bar_status.configure(text=short_grade, fg=level_color)

        # Resolve pinned conversation focus (for multi-main-conversation workflows)
        pinned_conv = None
        if self._pinned_conv_id:
            pinned_conv = next(
                (c for c in snap.active_conversations if c.conversation_id == self._pinned_conv_id),
                None,
            )
            if pinned_conv is None:
                self._pinned_conv_id = None

        disp_hit = pinned_conv.latest_hit_rate if pinned_conv is not None else snap.primary_hit_rate
        disp_hit_cls = (
            pinned_conv.recent_steps[-1].classification
            if (pinned_conv is not None and pinned_conv.recent_steps)
            else snap.primary_hit_classification
        )
        disp_hit_reason = (
            pinned_conv.recent_steps[-1].reason
            if (pinned_conv is not None and pinned_conv.recent_steps)
            else (
                next((c.recent_steps[-1].reason for c in snap.active_conversations if not c.is_subagent and c.recent_steps), "")
                if snap.active_conversations
                else ""
            )
        )
        disp_tps = pinned_conv.tps if pinned_conv is not None else snap.primary_tps
        disp_ttft = pinned_conv.ttft_sec if pinned_conv is not None else snap.primary_ttft
        disp_ttft_avg = pinned_conv.ttft_avg_sec if pinned_conv is not None else snap.primary_ttft_avg
        disp_ttft_max = pinned_conv.ttft_max_sec if pinned_conv is not None else snap.primary_ttft_max
        disp_speed = pinned_conv.speed_status if pinned_conv is not None else snap.primary_speed_status

        if disp_hit is not None:
            if disp_hit >= 70:
                hit_color = t["green"]
            elif disp_hit_reason == "compact_reset":
                hit_color = t["purple"]
            elif disp_hit >= 20 or disp_hit_cls == "expected_cold":
                hit_color = t["orange"]
            else:
                hit_color = t["red"]
            self._lbl_bar_cache_v.configure(text=f"{disp_hit:.1f}%", fg=hit_color)
        else:
            self._lbl_bar_cache_v.configure(text="—", fg=t["text_secondary"])

        if disp_tps is not None:
            tps_color = t["orange"] if disp_speed == "slow" else t["cyan"]
            self._lbl_bar_tps.configure(text=f"{disp_tps:.0f} t/s", fg=tps_color)
        else:
            self._lbl_bar_tps.configure(text="— t/s", fg=t["text_muted"])

        # Resolve freshness target (pinned large conv takes priority, otherwise snap's prioritized main conv)
        if pinned_conv is not None and (pinned_conv.freshness_remaining_sec is not None or pinned_conv.freshness_invalid_reason):
            disp_fresh_sec = pinned_conv.freshness_remaining_sec
            disp_fresh_conv = pinned_conv.short_id
            disp_fresh_title = pinned_conv.title
            disp_fresh_ctx = pinned_conv.context_tokens
            disp_fresh_invalid = pinned_conv.freshness_invalid_reason
        else:
            disp_fresh_sec = snap.min_freshness_sec
            disp_fresh_conv = snap.min_freshness_conv
            disp_fresh_title = snap.min_freshness_title
            disp_fresh_ctx = snap.min_freshness_ctx
            disp_fresh_invalid = snap.min_freshness_invalid_reason

        # Freshness indicator must never look green when the active large conversation just dropped cache (cr=0)
        if disp_fresh_invalid:
            fresh_color = t["red"] if snap.max_consec_miss >= 2 else t["orange"]
            self._lbl_bar_fresh.configure(text="保鲜 失效", fg=fresh_color)
        else:
            fresh_txt = format_countdown(disp_fresh_sec)
            fresh_color = t["text_secondary"]
            if disp_fresh_sec is not None:
                fresh_color = t["red"] if disp_fresh_sec <= 0 else (t["orange"] if disp_fresh_sec <= 60 else t["green"])
            self._lbl_bar_fresh.configure(text=f"保鲜 {fresh_txt}", fg=fresh_color)

        acc_txt = snap.account.short_label
        if snap.account.five_hour_pct is not None and snap.account.weekly_pct is not None:
            acc_txt += f" {snap.account.five_hour_pct:.0f}%/周{snap.account.weekly_pct:.0f}%"
        elif snap.account.five_hour_pct is not None:
            acc_txt += f" {snap.account.five_hour_pct:.0f}%"
        elif snap.account.weekly_pct is not None:
            acc_txt += f" 周{snap.account.weekly_pct:.0f}%"
        self._lbl_bar_acc.configure(text=acc_txt)

        step_bar_color = compute_steps_color(
            snap.today_steps, snap.daily_steps_warn, snap.daily_steps_alert, t["green"], t["red"]
        )
        step_text_color = step_bar_color if snap.today_steps >= snap.daily_steps_warn else t["text_secondary"]
        self._lbl_bar_steps.configure(text=f"{snap.today_steps}/{snap.daily_steps_alert}", fg=step_text_color)

        if self.tray is not None:
            hit_s = f"{snap.primary_hit_rate:.1f}%" if snap.primary_hit_rate is not None else "—"
            tps_s = f"{snap.primary_tps:.0f}t/s" if snap.primary_tps is not None else "—"
            tip = f"反重力监控 {short_grade} | 缓存 {hit_s} | {tps_s} | {acc_txt} | {snap.today_steps}步"
            self.tray.update_status(snap.level, tip)

            if self.config["notifications_enabled"] and snap.alerts:
                for _key, title, msg in snap.alerts:
                    self.tray.notify(title, msg)

        if not self._expanded:
            return

        # Expanded Panel: Module 1
        self._lbl_headline.configure(text=snap.headline, fg=level_color)
        self._lbl_clock.configure(text=datetime.datetime.fromtimestamp(snap.generated_at).strftime("%H:%M:%S"))
        self._lbl_acc_pill.configure(text=snap.account.short_label or "离线")
        self._lbl_acc_5h.configure(
            text=f"5h {snap.account.five_hour_pct:.1f}%" if snap.account.five_hour_pct is not None else "5h —",
            fg=t["text_muted"] if snap.account.five_hour_pct is None else (t["green"] if snap.account.five_hour_pct >= 35 else t["orange"]),
        )
        self._lbl_acc_wk.configure(
            text=f"周 {snap.account.weekly_pct:.0f}%" if snap.account.weekly_pct is not None else "周 —",
            fg=t["text_secondary"],
        )

        pct = min(100, int(round((snap.today_steps / max(1, snap.daily_steps_alert)) * 100)))
        self._lbl_steps_title.configure(text=f"今日请求步数保护线 ({snap.steps_scope_label})")
        if snap.total_today_steps > snap.today_steps:
            steps_val_txt = f"{snap.today_steps} / {snap.daily_steps_alert} ({pct}%) · 全机 {snap.total_today_steps}"
        else:
            steps_val_txt = f"{snap.today_steps} / {snap.daily_steps_alert} ({pct}%)"
        self._lbl_steps_val.configure(text=steps_val_txt, fg=step_text_color)
        self._last_steps_pct = pct
        self._last_steps_color = step_bar_color
        self._draw_steps_progress(self._last_steps_pct, self._last_steps_color)

        # Module 2: Speed & Freshness (reflects pinned conversation when active)
        if pinned_conv is not None:
            self._lbl_pin_badge.configure(text=f"📌{pinned_conv.short_id} ×")
            self._lbl_pin_badge.pack(side="left", padx=(6, 0))
        else:
            self._lbl_pin_badge.pack_forget()

        if len(snap.model_stats) >= 2:
            self._lbl_models_badge.configure(text=f"{len(snap.model_stats)}模 ▾")
            self._lbl_models_badge.pack(side="left", padx=(5, 0))
        else:
            self._lbl_models_badge.pack_forget()
        self._speed_hover_text = self._build_model_stats_tooltip_text(snap)

        ttft_txt = f"TTFT {disp_ttft:.1f}s" if disp_ttft is not None else "TTFT —"
        ttft_col = t["red"] if (disp_ttft or 0) >= 90 else (t["orange"] if (disp_ttft or 0) >= 45 else t["text_secondary"])
        self._lbl_ttft.configure(text=ttft_txt, fg=ttft_col)
        if disp_tps is not None:
            self._lbl_tps_big.configure(text=f"{disp_tps:.1f} tok/s", fg=t["orange"] if disp_speed == "slow" else t["text_primary"])
        else:
            self._lbl_tps_big.configure(text="— tok/s", fg=t["text_muted"])

        if disp_speed == "slow":
            self._lbl_speed_badge.configure(text="疑似暗限速", bg=t["orange_bg"], fg=t["orange"])
        elif disp_speed == "ok":
            self._lbl_speed_badge.configure(text="满速正常", bg=t["green_bg"], fg=t["green"])
        else:
            self._lbl_speed_badge.configure(text="待采样", bg=t["card_alt_bg"], fg=t["text_muted"])

        if disp_ttft_avg is not None and disp_ttft_max is not None:
            stat_col = t["red"] if disp_ttft_max >= 90 else (t["orange"] if disp_ttft_max >= 45 else t["text_secondary"])
            self._lbl_ttft_stats.configure(
                text=f"近10步 TTFT 均 {disp_ttft_avg:.1f}s · 峰 {disp_ttft_max:.1f}s",
                fg=stat_col,
            )
        else:
            self._lbl_ttft_stats.configure(text="近10步 TTFT 待采样", fg=t["text_muted"])

        # Multi-main-conversation freshness badge + hover summary
        fresh_list = snap.large_freshness_list
        if len(fresh_list) >= 2:
            self._lbl_fresh_count.configure(text=f"共 {len(fresh_list)} 个 ▾")
            self._lbl_fresh_count.pack(side="right")
        else:
            self._lbl_fresh_count.pack_forget()
        self._fresh_hover_text = self._build_freshness_tooltip_text(fresh_list, snap.freshness_window_sec)

        fresh_label = (
            f"{disp_fresh_conv} {truncate_display_width(disp_fresh_title, 12)}"
            if disp_fresh_title
            else disp_fresh_conv
        )
        if disp_fresh_invalid:
            self._lbl_fresh_sub.configure(text=f"{fresh_label} · {format_tokens(disp_fresh_ctx)}")
            inv_col = t["red"] if snap.max_consec_miss >= 2 else t["orange"]
            self._lbl_fresh_big.configure(text=disp_fresh_invalid, fg=inv_col)
        elif disp_fresh_sec is not None:
            self._lbl_fresh_sub.configure(text=f"{fresh_label} · {format_tokens(disp_fresh_ctx)}（{snap.freshness_window_sec}s窗）")
            if disp_fresh_sec <= 0:
                self._lbl_fresh_big.configure(text="已过期", fg=t["red"])
            elif disp_fresh_sec <= 60:
                self._lbl_fresh_big.configure(text=f"剩 {format_countdown(disp_fresh_sec)}", fg=t["orange"])
            else:
                self._lbl_fresh_big.configure(text=f"剩 {format_countdown(disp_fresh_sec)}", fg=t["green"])
        else:
            self._lbl_fresh_sub.configure(text=f"参考窗 {snap.freshness_window_sec}s")
            self._lbl_fresh_big.configure(text="无活跃大对话", fg=t["text_muted"])

        # Module 3: 10-step micro bars
        self._draw_conversation_bars(snap)

        # Module 4: Sliding-window log stats (per-account isolated recent / today; muted while offline)
        logs_live = bool(snap.account.online) and snap.grade != "IDLE"
        if logs_live:
            self._lbl_logs_title.configure(
                text=f"底层日志滑动窗监控（{snap.log_scope_label} · 近 {snap.sliding_window_min} 分钟 / 今日）"
            )
            self._update_log_cell("503", snap.recent_503, snap.today_503, warn_on_recent=1, red_on_recent=2)
            self._update_log_cell("timeout", snap.recent_timeout, snap.today_timeout, warn_on_recent=1, red_on_recent=1)
            self._update_log_cell("403", snap.recent_403, snap.today_403, warn_on_recent=1, red_on_recent=1)
            self._update_log_cell("retry", snap.recent_retry, snap.today_retry, warn_on_recent=5, red_on_recent=9999)
        else:
            self._lbl_logs_title.configure(text="底层日志监控（客户端未运行 · 已暂停实时告警）")
            self._update_log_cell("503", snap.recent_503, snap.today_503, warn_on_recent=1, red_on_recent=2, offline_muted=True)
            self._update_log_cell("timeout", snap.recent_timeout, snap.today_timeout, warn_on_recent=1, red_on_recent=1, offline_muted=True)
            self._update_log_cell("403", snap.recent_403, snap.today_403, warn_on_recent=1, red_on_recent=1, offline_muted=True)
            self._update_log_cell("retry", snap.recent_retry, snap.today_retry, warn_on_recent=5, red_on_recent=9999, offline_muted=True)

        if snap.grade != "IDLE" and snap.wasted_cold_tokens_total >= 150_000 and (snap.max_consec_miss >= 2 or snap.max_abnormal_recent10 >= 4):
            self._banner_frame.configure(bg=t["alert_banner_bg"], highlightbackground=t["alert_banner_border"])
            self._lbl_banner.configure(
                bg=t["alert_banner_bg"],
                fg=t["alert_banner_text"],
                text=f"近 10 步异常归零已多耗约 {format_tokens(snap.wasted_cold_tokens_total)} 冷算 Token，建议核查账号",
            )
        elif logs_live and snap.grade in ("T2", "T3") and snap.recent_events:
            ev_txt = "\n".join(e.summary for e in snap.recent_events[-2:])
            self._banner_frame.configure(bg=t["red_bg"], highlightbackground=t["red"])
            self._lbl_banner.configure(bg=t["red_bg"], fg=t["red"], text=ev_txt)
        elif logs_live and snap.t2_recovery_note and snap.grade == "WARN":
            self._banner_frame.configure(bg=t["orange_bg"], highlightbackground=t["orange"])
            self._lbl_banner.configure(bg=t["orange_bg"], fg=t["orange"], text=snap.sub_headline)
        else:
            self._banner_frame.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"])
            self._lbl_banner.configure(bg=t["card_alt_bg"], fg=t["text_secondary"], text=snap.sub_headline)

        # Dynamically sync open single-conversation detail window on every poll tick
        if self._detail_win is not None and not self._detail_win._closed:
            self._detail_win.sync_from_snapshot(snap)

    def _update_log_cell(
        self,
        key: str,
        recent: int,
        today: int,
        warn_on_recent: int,
        red_on_recent: int,
        offline_muted: bool = False,
    ) -> None:
        t = self.theme
        cell = self._log_cells[key]
        if offline_muted:
            cell["val"].configure(text=f"— / {today}", fg=t["text_muted"])
            return
        txt = f"{recent} / {today}"
        if recent >= red_on_recent:
            fg = t["red"]
        elif recent >= warn_on_recent:
            fg = t["orange"]
        else:
            fg = t["green"] if key != "retry" else t["text_primary"]
        cell["val"].configure(text=txt, fg=fg)

    def _draw_steps_progress(self, pct: int, fill_color: str) -> None:
        c = self._steps_canvas
        c.delete("all")
        raw_w = c.winfo_width()
        w = raw_w if raw_w > 20 else 380
        h = 7
        c.create_rectangle(0, 1, w, h - 1, fill=self.theme["bar_track"], outline="")
        fill_w = int(round(w * max(0, min(100, pct)) / 100.0))
        if fill_w > 0:
            c.create_rectangle(0, 1, fill_w, h - 1, fill=fill_color, outline="")

    def _ttft_color(self, sec: Optional[float]) -> str:
        t = self.theme
        if sec is None:
            return t["text_muted"]
        if sec >= 90.0:
            return t["red"]
        if sec >= 45.0:
            return t["orange"]
        if sec >= 15.0:
            return t["cyan"]
        return t["green"]

    def _build_model_stats_tooltip_text(self, snap: MonitorSnapshot) -> str:
        if not snap.model_stats:
            return ""
        lines = [f"近 {snap.sliding_window_min} 分钟分模型实测 ({snap.log_scope_label}):"]
        for ms in snap.model_stats[:6]:
            ttft_s = (
                f"均 {ms.ttft_avg_sec:4.1f}s / 峰 {ms.ttft_max_sec:4.1f}s"
                if (ms.ttft_avg_sec is not None and ms.ttft_max_sec is not None)
                else "TTFT 待采样"
            )
            tps_s = f"{ms.tps_avg:3.0f}t/s" if ms.tps_avg is not None else "  — "
            cacheable = ms.hit_count + ms.abnormal_miss_count
            hit_s = f"{(ms.hit_count / cacheable) * 100:4.0f}%" if cacheable > 0 else "  — "
            post_s = f" │ 503后+{ms.steps_after_last_t2}步" if ms.steps_after_last_t2 > 0 else ""
            lines.append(f"  {ms.model:<12} │ {ms.step_count:>2}步 │ 命中 {hit_s} │ TTFT {ttft_s} │ {tps_s}{post_s}")
        if snap.t2_recovery_note:
            lines.append(f"恢复判定: {snap.t2_recovery_note}")
        return "\n".join(lines)

    def _show_speed_tooltip(self, event: tk.Event) -> None:
        if self._closing or not self._speed_hover_text:
            return
        self._show_text_tooltip(self._speed_hover_text, event)

    def _build_freshness_tooltip_text(self, fresh_list: List[Any], window_sec: int) -> str:
        if not fresh_list:
            return ""
        lines = [f"全部活跃大上下文主对话 (共 {len(fresh_list)} 个 · {window_sec}s 保鲜窗):"]
        for c in fresh_list:
            if c.freshness_invalid_reason:
                st_txt = c.freshness_invalid_reason
            elif c.freshness_remaining_sec is not None:
                st_txt = "已过期" if c.freshness_remaining_sec <= 0 else f"剩 {format_countdown(c.freshness_remaining_sec)}"
            else:
                st_txt = "闲置"
            pin_mk = "📌 " if self._pinned_conv_id == c.conversation_id else "   "
            name_part = f" {truncate_display_width(c.title, 20)}" if getattr(c, "title", "") else ""
            lines.append(f"{pin_mk}{c.short_id}{name_part} · {c.model} │ {format_tokens(c.context_tokens):>6} │ {st_txt}")
        lines.append("提示: 右键对话行或点进长线详情可切换主面板聚焦目标")
        return "\n".join(lines)

    def _show_freshness_tooltip(self, event: tk.Event) -> None:
        if self._closing or not self._fresh_hover_text:
            return
        self._show_text_tooltip(self._fresh_hover_text, event)

    def _build_conv_tooltip_text(self, conv: Any) -> str:
        delta_s = f" ({format_delta_tokens(conv.latest_context_delta)})" if conv.latest_context_delta != 0 else ""
        total_s = f" · 共 {conv.total_step_count} 步" if getattr(conv, "total_step_count", 0) > 0 else ""
        head_name = f"{conv.title} ({conv.short_id})" if getattr(conv, "title", "") else conv.short_id
        lines = [f"{head_name} · {conv.model} · {format_tokens(conv.context_tokens)}{delta_s}{total_s}（左键看长线详情）"]
        if conv.ttft_avg_sec is not None and conv.ttft_max_sec is not None:
            cur_t = f"{conv.ttft_sec:.1f}s" if conv.ttft_sec is not None else "—"
            lines.append(f"TTFT: 新 {cur_t}  │  均 {conv.ttft_avg_sec:.1f}s  │  峰 {conv.ttft_max_sec:.1f}s")
        steps = conv.recent_steps[-10:]
        ttfts = conv.recent_ttfts[-len(steps):] if conv.recent_ttfts else []
        tpss = conv.recent_tps_list[-len(steps):] if conv.recent_tps_list else []
        t_offset = len(steps) - len(ttfts)
        s_offset = len(steps) - len(tpss)
        reason_map = {
            "cached": "命中",
            "first_turn": "首轮",
            "account_switch": "换号冷启",
            "idle_rewarm": "闲置重热",
            "model_switch": "切模型",
            "compact_reset": "压缩重热",
            "small_ctx": "小上下文",
            "first_cacheable": "首次入窗",
            "abnormal_zero": "掉缓存!",
        }
        lines.append("近 10 步逐拍明细（新 → 旧，最新置顶）:")
        step_rows = []
        for i, st in enumerate(steps):
            t_val = ttfts[i - t_offset] if (i - t_offset) >= 0 and (i - t_offset) < len(ttfts) else None
            p_val = tpss[i - s_offset] if (i - s_offset) >= 0 and (i - s_offset) < len(tpss) else None
            hm = datetime.datetime.fromtimestamp(st.timestamp).strftime("%m-%d %H:%M:%S") if st.timestamp > 0 else "-- --:--:--"
            t_str = f"{t_val:5.1f}s" if t_val is not None else "    — "
            p_str = f"{p_val:3.0f}t/s" if p_val is not None else "   — "
            ctx_s = f"{format_tokens(st.total_tokens):>6}"
            d_s = f"({format_delta_tokens(st.context_delta):>7})" if st.context_delta != 0 else "(    +0)"
            mod_s = format_model_name(st.model)
            tag = reason_map.get(st.reason, st.reason)
            step_rows.append((hm, mod_s, ctx_s, d_s, st.hit_rate, t_str, p_str, tag))
        show_per_step_model = len({r[1] for r in step_rows}) >= 2
        for rev_idx, (hm, mod_s, ctx_s, d_s, hr, t_str, p_str, tag) in enumerate(reversed(step_rows), 1):
            badge = "最新" if rev_idx == 1 else f"#{rev_idx:<2}"
            mod_col = f" │ {mod_s:<11}" if show_per_step_model else ""
            lines.append(f"  {badge:<3} {hm}{mod_col} │ {ctx_s} {d_s} │ 缓存 {hr:5.1f}% │ 首字 {t_str} │ {p_str} [{tag}]")
        return "\n".join(lines)

    def _show_text_tooltip(self, text: str, event: tk.Event) -> None:
        if self._closing or not text:
            return
        self._hide_conv_tooltip()
        t = self.theme
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.96)
            win.configure(bg=t["border"])
            inner = tk.Frame(win, bg=t["panel_bg"], padx=9, pady=7)
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            lbl = tk.Label(
                inner,
                text=text,
                font=(MONO_FONT, 8),
                bg=t["panel_bg"],
                fg=t["text_primary"],
                justify="left",
                anchor="w",
            )
            lbl.pack()
            win.update_idletasks()
            tw, th = win.winfo_reqwidth(), win.winfo_reqheight()
            vx, vy, vr, vb = _get_virtual_screen_bounds(self.root)
            px = min(vr - tw - 8, max(vx + 8, event.x_root + 14))
            py = event.y_root + 16
            if py + th > vb - 44:
                py = max(vy + 8, event.y_root - th - 12)
            win.geometry(f"+{px}+{py}")
            self._tooltip_win = win
        except tk.TclError:
            self._tooltip_win = None

    def _show_conv_tooltip(self, row_idx: int, event: tk.Event) -> None:
        if self._closing or row_idx < 0 or row_idx >= len(self._conv_hover_texts):
            return
        text = self._conv_hover_texts[row_idx]
        if text:
            self._show_text_tooltip(text, event)

    def _set_conv_row_bg(self, row_idx: int, bg_hex: str) -> None:
        if row_idx < 0 or row_idx >= len(self._conv_rows):
            return
        row_ui = self._conv_rows[row_idx]
        for k in ("id", "title", "model", "ctx", "ttft_canvas", "ttft_val", "canvas", "hit", "speed"):
            try:
                row_ui[k].configure(bg=bg_hex)
            except tk.TclError:
                pass

    def _on_conv_row_enter(self, row_idx: int, event: tk.Event) -> None:
        if self._hovered_row_idx is not None and self._hovered_row_idx != row_idx:
            self._set_conv_row_bg(self._hovered_row_idx, self.theme["card_bg"])
        self._hovered_row_idx = row_idx
        self._set_conv_row_bg(row_idx, self.theme["card_alt_bg"])
        self._show_conv_tooltip(row_idx, event)

    def _on_conv_row_leave(self, row_idx: int) -> None:
        if self._hovered_row_idx == row_idx:
            self._hovered_row_idx = None
        self._set_conv_row_bg(row_idx, self.theme["card_bg"])
        self._hide_conv_tooltip()

    def _on_conv_row_click(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self._conv_row_ids):
            return
        conv_id = self._conv_row_ids[row_idx]
        if conv_id:
            self._hide_conv_tooltip()
            self.open_conversation_detail(conv_id)

    def _on_conv_row_right_click(self, row_idx: int, event: tk.Event) -> None:
        if row_idx < 0 or row_idx >= len(self._conv_row_ids) or not self._conv_row_ids[row_idx]:
            self._show_context_menu(event)
            return
        conv_id = self._conv_row_ids[row_idx]
        short_id = conv_id[:6]
        self._hide_conv_tooltip()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label=f"📈 查看 {short_id} 长线详情", command=lambda cid=conv_id: self.open_conversation_detail(cid))
        if self._pinned_conv_id == conv_id:
            menu.add_command(label=f"取消聚焦 {short_id}", command=lambda: self.set_pinned_conversation(None))
        else:
            menu.add_command(label=f"📌 聚焦 {short_id} 到主面板", command=lambda cid=conv_id: self.set_pinned_conversation(cid))
        menu.add_separator()
        menu.add_command(label="打开监控主菜单...", command=lambda e=event: self._show_context_menu(e))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def set_pinned_conversation(self, conv_id: Optional[str]) -> None:
        self._pinned_conv_id = conv_id or None
        if self._latest_snapshot is not None:
            self.render_snapshot(self._latest_snapshot)

    def _toggle_pin_conversation(self, conv_id: str) -> None:
        if self._pinned_conv_id == conv_id:
            self.set_pinned_conversation(None)
        else:
            self.set_pinned_conversation(conv_id)

    def open_conversation_detail(self, conv_id: str) -> None:
        if self._closing or not conv_id:
            return
        if self._detail_win is None or self._detail_win._closed:
            def _on_closed() -> None:
                self._detail_win = None

            self._detail_win = ConversationDetailWindow(
                parent=self.root,
                theme=self.theme,
                load_report=self.engine.read_conversation_detail,
                on_toggle_pin=self._toggle_pin_conversation,
                is_pinned_fn=lambda cid: self._pinned_conv_id == cid,
                on_closed=_on_closed,
                apply_rounding_fn=_apply_win11_rounded_corners,
                screen_bounds_fn=_get_virtual_screen_bounds,
            )
            self._detail_win.show_conversation(conv_id, place_near_parent=True)
        else:
            self._detail_win.show_conversation(conv_id, place_near_parent=False)

    def _hide_conv_tooltip(self) -> None:
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None

    def set_conv_filter(self, mode: str) -> None:
        if mode not in ("main", "subagent", "all"):
            return
        self._conv_filter = mode
        self.config["conv_filter"] = mode
        self.config_store.save(self.config)
        self._conv_viewport.yview_moveto(0.0)
        self._refresh_filter_buttons()
        if self._latest_snapshot is not None and self._expanded:
            self._draw_conversation_bars(self._latest_snapshot)

    def _refresh_filter_buttons(self, main_cnt: Optional[int] = None, sub_cnt: Optional[int] = None) -> None:
        t = self.theme
        counts = {}
        if main_cnt is not None and sub_cnt is not None:
            counts = {"main": main_cnt, "subagent": sub_cnt, "all": main_cnt + sub_cnt}
        base_labels = {"main": "主对话", "subagent": "子代理", "all": "全部"}
        for key, btn in self._filter_buttons.items():
            txt = f"{base_labels[key]}({counts[key]})" if key in counts else base_labels[key]
            active = key == self._conv_filter
            btn.configure(
                text=txt,
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )

    def _update_conv_scrollregion(self) -> None:
        try:
            bbox = self._conv_viewport.bbox("all")
            if bbox:
                self._conv_viewport.configure(scrollregion=bbox)
        except tk.TclError:
            pass
        self._sync_custom_scrollbar()

    def _sync_custom_scrollbar(self) -> None:
        sb = self._conv_scrollbar
        t = self.theme
        try:
            sb.delete("all")
            h = max(16, sb.winfo_height())
            w = max(6, sb.winfo_width())
            first, last = self._conv_viewport.yview()
            if last - first >= 0.995:
                return
            sb.create_rectangle(1, 2, w - 1, h - 2, fill=t["bar_track"], outline="")
            y0 = max(2, int(round(first * (h - 4))) + 2)
            y1 = min(h - 2, max(y0 + 18, int(round(last * (h - 4))) + 2))
            sb.create_rectangle(1, y0, w - 1, y1, fill=t["cyan"], outline="")
        except tk.TclError:
            pass

    def _on_conv_mousewheel(self, event: tk.Event) -> None:
        first, last = self._conv_viewport.yview()
        if last - first >= 0.995:
            return
        step = -1 if event.delta > 0 else 1
        self._conv_viewport.yview_scroll(step, "units")
        self._sync_custom_scrollbar()
        self._hide_conv_tooltip()

    def _on_scrollbar_press(self, event: tk.Event) -> None:
        first, last = self._conv_viewport.yview()
        if last - first >= 0.995:
            return
        self._scroll_drag_origin_y = event.y
        self._scroll_drag_origin_top = first
        h = max(16, self._conv_scrollbar.winfo_height())
        thumb_frac = max(0.05, last - first)
        target = max(0.0, min(1.0 - thumb_frac, (event.y / float(h)) - thumb_frac / 2.0))
        self._conv_viewport.yview_moveto(target)
        self._sync_custom_scrollbar()

    def _on_scrollbar_drag(self, event: tk.Event) -> None:
        first, last = self._conv_viewport.yview()
        if last - first >= 0.995:
            return
        h = max(16, self._conv_scrollbar.winfo_height())
        dy = event.y - self._scroll_drag_origin_y
        target = max(0.0, min(1.0 - (last - first), self._scroll_drag_origin_top + dy / float(h)))
        self._conv_viewport.yview_moveto(target)
        self._sync_custom_scrollbar()

    def _draw_resize_grip(self) -> None:
        g = self._resize_grip
        t = self.theme
        try:
            g.delete("all")
            w = max(80, g.winfo_width())
            cx = w // 2
            g.create_rectangle(cx - 22, 3, cx + 22, 6, fill=t["border"], outline="")
        except tk.TclError:
            pass

    def _on_resize_start(self, event: tk.Event) -> None:
        self._resize_origin_y = event.y_root
        self._resize_origin_rows = self._conv_list_rows

    def _on_resize_drag(self, event: tk.Event) -> None:
        dy = event.y_root - self._resize_origin_y
        delta_rows = int(round(dy / float(max(18, self._conv_row_px))))
        new_rows = max(3, min(12, self._resize_origin_rows + delta_rows))
        if new_rows != self._conv_list_rows:
            self._conv_list_rows = new_rows
            if self._latest_snapshot is not None:
                self._draw_conversation_bars(self._latest_snapshot)
            else:
                self._conv_viewport.configure(height=self._conv_list_rows * self._conv_row_px)
                self._sync_custom_scrollbar()

    def _on_resize_end(self, _event: tk.Event) -> None:
        self.config["conv_list_rows"] = self._conv_list_rows
        self.config_store.save(self.config)
        self._clamp_window_to_screen()

    def _draw_conversation_bars(self, snap: MonitorSnapshot) -> None:
        t = self.theme
        all_convs = snap.active_conversations[:24]
        main_convs = [c for c in all_convs if not c.is_subagent]
        sub_convs = [c for c in all_convs if c.is_subagent]
        self._refresh_filter_buttons(len(main_convs), len(sub_convs))

        if self._conv_filter == "main":
            convs = main_convs
        elif self._conv_filter == "subagent":
            convs = sub_convs
        else:
            convs = all_convs

        all_keys = ("id", "title", "model", "ctx", "ttft_canvas", "ttft_val", "canvas", "hit", "speed")
        self._conv_hover_texts = [""] * len(self._conv_rows)
        self._conv_row_ids = [""] * len(self._conv_rows)

        if not convs:
            for row_ui in self._conv_rows:
                for key in all_keys:
                    row_ui[key].grid_remove()
            empty_msg = {
                "main": "暂无近 24 小时活跃主对话",
                "subagent": "当前无近 24 小时子代理记录",
                "all": "暂无近 24 小时活跃对话记录",
            }.get(self._conv_filter, "暂无活跃对话记录")
            self._lbl_convs_empty.configure(text=empty_msg)
            self._lbl_convs_empty.grid(row=0, column=0, columnspan=9, sticky="w", pady=4)
            self._conv_viewport.configure(height=28)
            self._conv_scrollbar.configure(height=28)
            self._update_conv_scrollregion()
            return

        self._lbl_convs_empty.grid_remove()
        for idx, row_ui in enumerate(self._conv_rows):
            if idx >= len(convs):
                for key in all_keys:
                    row_ui[key].grid_remove()
                continue

            conv = convs[idx]
            self._conv_hover_texts[idx] = self._build_conv_tooltip_text(conv)
            self._conv_row_ids[idx] = conv.conversation_id
            is_pinned = self._pinned_conv_id == conv.conversation_id
            id_txt = f"📌{conv.short_id}" if is_pinned else conv.short_id
            row_ui["id"].configure(text=id_txt, fg=t["cyan"] if is_pinned else t["text_primary"])

            if conv.title:
                title_txt = truncate_display_width(conv.title, 16)
                title_col = t["cyan"] if is_pinned else t["text_primary"]
            elif conv.is_subagent:
                title_txt = "子代理"
                title_col = t["text_muted"]
            else:
                title_txt = "未命名对话"
                title_col = t["text_muted"]
            row_ui["title"].configure(text=title_txt, fg=title_col)

            mod_txt = f"子·{conv.model}" if (self._conv_filter == "all" and conv.is_subagent) else conv.model
            row_ui["model"].configure(text=mod_txt, fg=t["text_muted"] if conv.is_subagent else t["text_secondary"])

            ctx_txt = format_tokens(conv.context_tokens)
            if conv.latest_context_delta <= -32000:
                ctx_txt += "↓"
                ctx_col = t["purple"]
            else:
                ctx_col = t["text_secondary"]
            row_ui["ctx"].configure(text=ctx_txt, fg=ctx_col)

            if conv.ttft_sec is not None:
                ttft_txt = f"{conv.ttft_sec:.0f}s" if conv.ttft_sec >= 100 else f"{conv.ttft_sec:.1f}s"
            else:
                ttft_txt = "—"
            row_ui["ttft_val"].configure(text=ttft_txt, fg=self._ttft_color(conv.ttft_sec))

            latest_step = conv.recent_steps[-1] if conv.recent_steps else None
            latest_cls = latest_step.classification if latest_step else ""
            latest_reason = latest_step.reason if latest_step else ""
            if conv.latest_hit_rate >= 70:
                hit_col = t["green"]
            elif latest_reason == "compact_reset":
                hit_col = t["purple"]
            elif conv.latest_hit_rate >= 20 or latest_cls == "expected_cold":
                hit_col = t["orange"]
            else:
                hit_col = t["red"]
            row_ui["hit"].configure(text=f"{conv.latest_hit_rate:.1f}%", fg=hit_col)

            sp_txt = f"{conv.tps:.0f} t/s" if conv.tps is not None else "—"
            sp_col = t["orange"] if conv.speed_status == "slow" else t["cyan"]
            row_ui["speed"].configure(text=sp_txt, fg=sp_col)

            row_ui["id"].grid(row=idx, column=0, sticky="w", padx=(0, 6), pady=3)
            row_ui["title"].grid(row=idx, column=1, sticky="w", padx=(0, 10), pady=3)
            row_ui["model"].grid(row=idx, column=2, sticky="w", padx=(0, 6), pady=3)
            row_ui["ctx"].grid(row=idx, column=3, sticky="e", padx=(0, 6), pady=3)
            row_ui["ttft_canvas"].grid(row=idx, column=4, sticky="e", padx=(0, 4), pady=3)
            row_ui["ttft_val"].grid(row=idx, column=5, sticky="e", padx=(0, 6), pady=3)
            row_ui["canvas"].grid(row=idx, column=6, sticky="e", padx=(0, 6), pady=3)
            row_ui["hit"].grid(row=idx, column=7, sticky="e", padx=(0, 5), pady=3)
            row_ui["speed"].grid(row=idx, column=8, sticky="e", pady=3)

            # Draw 10-step TTFT spark-bars (left-to-right oldest -> newest)
            tc = row_ui["ttft_canvas"]
            tc.delete("all")
            tw, tgap = 5, 2
            ttop, tbot = 1, 15
            t_series = conv.recent_ttfts[-10:]
            t_pad = 10 - len(t_series)
            for slot in range(10):
                x0 = slot * (tw + tgap)
                x1 = x0 + tw
                if slot < t_pad or t_series[slot - t_pad] is None:
                    tc.create_rectangle(x0, tbot - 3, x1, tbot, fill=t["bar_dim"], outline="")
                    continue
                sec = float(t_series[slot - t_pad])
                col = self._ttft_color(sec)
                h_ratio = min(1.0, max(0.24, sec / 75.0))
                top_y = tbot - int(round((tbot - ttop) * h_ratio))
                tc.create_rectangle(x0, top_y, x1, tbot, fill=col, outline="")

            # Draw 10-step Cache hit-rate micro-bars (Green=hit, Purple=compact_reset, Orange=expected_cold, Red=abnormal_miss)
            c = row_ui["canvas"]
            c.delete("all")
            bar_w = 7
            bar_gap = 3
            bar_top = 1
            bar_bot = 15
            steps = conv.recent_steps[-10:]
            pad_slots = 10 - len(steps)
            for slot in range(10):
                x0 = slot * (bar_w + bar_gap)
                x1 = x0 + bar_w
                if slot < pad_slots:
                    c.create_rectangle(x0, bar_top + 4, x1, bar_bot, fill=t["bar_dim"], outline="")
                    continue
                st = steps[slot - pad_slots]
                if st.classification == "hit":
                    col = t["green"]
                    h_ratio = max(0.38, min(1.0, st.hit_rate / 100.0))
                elif st.reason == "compact_reset":
                    col = t["purple"]
                    h_ratio = 0.74
                elif st.classification == "expected_cold":
                    col = t["orange"]
                    h_ratio = 0.56
                else:
                    col = t["red"]
                    h_ratio = 1.0
                top_y = bar_bot - int(round((bar_bot - bar_top) * h_ratio))
                c.create_rectangle(x0, top_y, x1, bar_bot, fill=col, outline="")

        self._convs_table.update_idletasks()
        table_h = self._convs_table.winfo_reqheight()
        if convs:
            measured_row = max(20, int(round(table_h / float(len(convs)))))
            self._conv_row_px = measured_row
        if len(convs) <= self._conv_list_rows:
            vp_h = max(28, table_h)
        else:
            vp_h = max(28, self._conv_list_rows * self._conv_row_px)
        self._conv_viewport.configure(height=vp_h)
        self._conv_scrollbar.configure(height=vp_h)
        self._update_conv_scrollregion()

    # ------------------------------------------------------------------ Polling & lifecycle
    def _worker_loop(self) -> None:
        first = True
        while not self._stop_event.is_set():
            try:
                snap = self.engine.poll(force_account=first)
                first = False
                try:
                    while not self._snapshot_queue.empty():
                        self._snapshot_queue.get_nowait()
                except queue.Empty:
                    pass
                self._snapshot_queue.put_nowait(snap)
            except Exception:
                pass
            interval = max(1.0, float(self.config.get("poll_interval_ms", 2500)) / 1000.0)
            self._force_poll_event.wait(timeout=interval)
            self._force_poll_event.clear()

    def _ui_drain_tick(self) -> None:
        if self._closing:
            return
        try:
            while True:
                snap = self._snapshot_queue.get_nowait()
                self.render_snapshot(snap)
        except queue.Empty:
            pass
        aid = self.root.after(250, self._ui_drain_tick)
        self._after_ids.append(aid)
        if len(self._after_ids) > 16:
            self._after_ids = self._after_ids[-8:]

    def _start_tray_if_enabled(self) -> None:
        if not self._enable_tray or self.tray is not None:
            return
        from ui.tray_manager import TrayManager

        self.tray = TrayManager(
            base_icon_png=self.app_root / "assets" / "app-icon.png",
            dispatch_to_ui=self.dispatch_to_ui,
            callbacks={
                "toggle_visible": self.toggle_visible,
                "toggle_expand": self.toggle_expand,
                "theme_midnight": lambda: self.set_theme("midnight_blue"),
                "theme_obsidian": lambda: self.set_theme("obsidian_dark"),
                "toggle_topmost": self.toggle_topmost,
                "toggle_startup": self.toggle_startup,
                "toggle_start_minimized": self.toggle_start_minimized,
                "toggle_mute": self.toggle_notifications,
                "refresh_now": self.request_immediate_poll,
                "copy_summary": self.copy_summary_to_clipboard,
                "open_logs": self.open_logs_folder,
                "restart": self.restart_app,
                "quit": self.shutdown,
            },
            state_getters={
                "theme": lambda: self.config["theme"],
                "topmost": lambda: self.config["topmost"],
                "startup": lambda: self._startup_cached,
                "start_minimized": lambda: self.config["start_minimized"],
                "notifications": lambda: self.config["notifications_enabled"],
            },
        )
        self.tray.start()

    def start_polling(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="MonitorPollThread", daemon=True)
        self._worker_thread.start()
        self._ui_drain_tick()

    def run(self) -> None:
        try:
            self._start_tray_if_enabled()
            self.start_polling()
            self.root.mainloop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._hide_conv_tooltip()
        if self._detail_win is not None:
            self._detail_win.close()
            self._detail_win = None
        self._stop_event.set()
        self._force_poll_event.set()

        for aid in list(self._after_ids):
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_ids.clear()

        if self.tray is not None:
            self.tray.stop()
            self.tray = None

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
            self._worker_thread = None

        try:
            self.root.quit()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
