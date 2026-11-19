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
    format_text_summary,
    format_tokens,
)
from core.startup_manager import is_startup_enabled, set_startup_enabled
from ui.themes import THEMES, get_theme

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


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
        self._conv_hover_texts: List[str] = ["", "", "", ""]

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

        self._card_fresh = tk.Frame(self._mid_row, padx=10, pady=8, highlightthickness=1)
        self._card_fresh.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        fr_top = tk.Frame(self._card_fresh)
        fr_top.pack(fill="x")
        self._lbl_fresh_title = tk.Label(fr_top, text="大上下文保鲜 (≥100K)", font=(UI_FONT, 8))
        self._lbl_fresh_title.pack(side="left")
        self._lbl_fresh_sub = tk.Label(self._card_fresh, text="当前无活跃大对话", font=(MONO_FONT, 8), anchor="w")
        self._lbl_fresh_sub.pack(fill="x", pady=(4, 0))
        self._lbl_fresh_big = tk.Label(self._card_fresh, text="闲置", font=(MONO_FONT, 11, "bold"), anchor="w")
        self._lbl_fresh_big.pack(fill="x", pady=(2, 0))

        # --- Module 3: Active Conversations 10-Step TTFT + Cache Micro-Bars (DPI-safe grid layout) ---
        self._card_convs = tk.Frame(self._details, padx=10, pady=8, highlightthickness=1)
        self._card_convs.pack(fill="x", pady=(0, 8))
        conv_hdr = tk.Frame(self._card_convs)
        conv_hdr.pack(fill="x", pady=(0, 6))
        self._lbl_convs_title = tk.Label(conv_hdr, text="活跃对话 · 首字 TTFT 与缓存（近 10 步）", font=(UI_FONT, 9, "bold"))
        self._lbl_convs_title.pack(side="left")
        self._lbl_convs_legend = tk.Label(conv_hdr, text="悬停看逐拍明细 │ 绿良 橙警 红异常", font=(UI_FONT, 8))
        self._lbl_convs_legend.pack(side="right")

        self._convs_table = tk.Frame(self._card_convs)
        self._convs_table.pack(fill="x")
        self._convs_table.grid_columnconfigure(1, weight=1)
        self._conv_rows: List[Dict[str, Any]] = []
        self._lbl_convs_empty = tk.Label(self._convs_table, text="暂无近 24 小时活跃对话记录", font=(UI_FONT, 8), anchor="w")
        for r_idx in range(4):
            lbl_id = tk.Label(self._convs_table, text="", font=(MONO_FONT, 9, "bold"), anchor="w", width=7)
            lbl_mod = tk.Label(self._convs_table, text="", font=(UI_FONT, 8), anchor="w")
            lbl_ctx = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=6)
            ttft_cv = tk.Canvas(self._convs_table, width=68, height=16, highlightthickness=0, bd=0)
            lbl_ttft = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=6)
            bar_cv = tk.Canvas(self._convs_table, width=98, height=16, highlightthickness=0, bd=0)
            lbl_hit = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8, "bold"), anchor="e", width=6)
            lbl_spd = tk.Label(self._convs_table, text="", font=(MONO_FONT, 8), anchor="e", width=7)
            for hover_w in (lbl_id, lbl_mod, lbl_ctx, ttft_cv, lbl_ttft, bar_cv, lbl_hit, lbl_spd):
                hover_w.bind("<Enter>", lambda e, idx=r_idx: self._show_conv_tooltip(idx, e))
                hover_w.bind("<Leave>", lambda _e: self._hide_conv_tooltip())
            self._conv_rows.append(
                {
                    "row": r_idx,
                    "id": lbl_id,
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

        # --- Module 5: Quick Action Bar ---
        self._actions_row = tk.Frame(self._details, pady=2)
        self._actions_row.pack(fill="x", pady=(0, 10))
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

    def _bind_recursive_context_menu(self, root_widget: tk.Widget) -> None:
        excluded = set(self._action_buttons.values())
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
        self._lbl_ttft.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_tps_big.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._lbl_speed_badge.configure(bg=t["green_bg"], fg=t["green"])
        self._lbl_ttft_stats.configure(bg=t["card_bg"], fg=t["text_muted"])

        self._lbl_fresh_title.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_fresh_sub.configure(bg=t["card_bg"], fg=t["text_secondary"])
        self._lbl_fresh_big.configure(bg=t["card_bg"], fg=t["text_primary"])

        self._lbl_convs_title.configure(bg=t["card_bg"], fg=t["text_primary"])
        self._lbl_convs_legend.configure(bg=t["card_bg"], fg=t["text_muted"])
        self._convs_table.configure(bg=t["card_bg"])
        self._lbl_convs_empty.configure(bg=t["card_bg"], fg=t["text_muted"])
        for row_ui in self._conv_rows:
            row_ui["id"].configure(bg=t["card_bg"], fg=t["text_primary"])
            row_ui["model"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["ctx"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["ttft_canvas"].configure(bg=t["card_bg"])
            row_ui["ttft_val"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            row_ui["canvas"].configure(bg=t["card_bg"])
            row_ui["hit"].configure(bg=t["card_bg"])
            row_ui["speed"].configure(bg=t["card_bg"])

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

    def hide_window(self) -> None:
        if self._closing:
            return
        self._visible = False
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

        if snap.primary_hit_rate is not None:
            if snap.primary_hit_rate >= 70:
                hit_color = t["green"]
            elif snap.primary_hit_rate >= 20 or snap.primary_hit_classification == "expected_cold":
                hit_color = t["orange"]
            else:
                hit_color = t["red"]
            self._lbl_bar_cache_v.configure(text=f"{snap.primary_hit_rate:.1f}%", fg=hit_color)
        else:
            self._lbl_bar_cache_v.configure(text="—", fg=t["text_secondary"])

        if snap.primary_tps is not None:
            tps_color = t["orange"] if snap.primary_speed_status == "slow" else t["cyan"]
            self._lbl_bar_tps.configure(text=f"{snap.primary_tps:.0f} t/s", fg=tps_color)
        else:
            self._lbl_bar_tps.configure(text="— t/s", fg=t["text_muted"])

        # Freshness indicator must never look green when the active large conversation just dropped cache (cr=0)
        if snap.min_freshness_invalid_reason:
            fresh_color = t["red"] if snap.max_consec_miss >= 2 else t["orange"]
            self._lbl_bar_fresh.configure(text="保鲜 失效", fg=fresh_color)
        else:
            fresh_txt = format_countdown(snap.min_freshness_sec)
            fresh_color = t["text_secondary"]
            if snap.min_freshness_sec is not None:
                fresh_color = t["red"] if snap.min_freshness_sec <= 0 else (t["orange"] if snap.min_freshness_sec <= 60 else t["green"])
            self._lbl_bar_fresh.configure(text=f"保鲜 {fresh_txt}", fg=fresh_color)

        acc_txt = snap.account.short_label
        if snap.account.five_hour_pct is not None:
            acc_txt += f" {snap.account.five_hour_pct:.0f}%"
        self._lbl_bar_acc.configure(text=acc_txt)

        step_color = t["red"] if snap.today_steps >= snap.daily_steps_alert else (t["orange"] if snap.today_steps >= snap.daily_steps_warn else t["text_secondary"])
        self._lbl_bar_steps.configure(text=f"{snap.today_steps}/{snap.daily_steps_alert}", fg=step_color)

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
        self._lbl_steps_val.configure(text=steps_val_txt, fg=step_color)
        self._last_steps_pct = pct
        self._last_steps_color = step_color if step_color != t["text_secondary"] else t["green"]
        self._draw_steps_progress(self._last_steps_pct, self._last_steps_color)

        # Module 2: Speed & Freshness
        ttft_txt = f"TTFT {snap.primary_ttft:.1f}s" if snap.primary_ttft is not None else "TTFT —"
        ttft_col = t["red"] if (snap.primary_ttft or 0) >= 90 else (t["orange"] if (snap.primary_ttft or 0) >= 45 else t["text_secondary"])
        self._lbl_ttft.configure(text=ttft_txt, fg=ttft_col)
        if snap.primary_tps is not None:
            self._lbl_tps_big.configure(text=f"{snap.primary_tps:.1f} tok/s", fg=t["orange"] if snap.primary_speed_status == "slow" else t["text_primary"])
        else:
            self._lbl_tps_big.configure(text="— tok/s", fg=t["text_muted"])

        if snap.primary_speed_status == "slow":
            self._lbl_speed_badge.configure(text="疑似暗限速", bg=t["orange_bg"], fg=t["orange"])
        elif snap.primary_speed_status == "ok":
            self._lbl_speed_badge.configure(text="满速正常", bg=t["green_bg"], fg=t["green"])
        else:
            self._lbl_speed_badge.configure(text="待采样", bg=t["card_alt_bg"], fg=t["text_muted"])

        if snap.primary_ttft_avg is not None and snap.primary_ttft_max is not None:
            stat_col = t["red"] if snap.primary_ttft_max >= 90 else (t["orange"] if snap.primary_ttft_max >= 45 else t["text_secondary"])
            self._lbl_ttft_stats.configure(
                text=f"近10步 TTFT 均 {snap.primary_ttft_avg:.1f}s · 峰 {snap.primary_ttft_max:.1f}s",
                fg=stat_col,
            )
        else:
            self._lbl_ttft_stats.configure(text="近10步 TTFT 待采样", fg=t["text_muted"])

        if snap.min_freshness_invalid_reason:
            self._lbl_fresh_sub.configure(text=f"{snap.min_freshness_conv} · {format_tokens(snap.min_freshness_ctx)}（缓存诊断）")
            inv_col = t["red"] if snap.max_consec_miss >= 2 else t["orange"]
            self._lbl_fresh_big.configure(text=snap.min_freshness_invalid_reason, fg=inv_col)
        elif snap.min_freshness_sec is not None:
            self._lbl_fresh_sub.configure(text=f"{snap.min_freshness_conv} · {format_tokens(snap.min_freshness_ctx)}（{snap.freshness_window_sec}s窗）")
            if snap.min_freshness_sec <= 0:
                self._lbl_fresh_big.configure(text="已过期", fg=t["red"])
            elif snap.min_freshness_sec <= 60:
                self._lbl_fresh_big.configure(text=f"剩 {format_countdown(snap.min_freshness_sec)}", fg=t["orange"])
            else:
                self._lbl_fresh_big.configure(text=f"剩 {format_countdown(snap.min_freshness_sec)}", fg=t["green"])
        else:
            self._lbl_fresh_sub.configure(text=f"参考窗 {snap.freshness_window_sec}s")
            self._lbl_fresh_big.configure(text="无活跃大对话", fg=t["text_muted"])

        # Module 3: 10-step micro bars
        self._draw_conversation_bars(snap)

        # Module 4: Sliding-window log stats (always dual format recent / today; muted while offline)
        logs_live = bool(snap.account.online) and snap.grade != "IDLE"
        if logs_live:
            self._lbl_logs_title.configure(text=f"底层日志滑动窗监控（近 {snap.sliding_window_min} 分钟 / 今日）")
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
        elif logs_live and snap.recent_events and (snap.recent_503 > 0 or snap.recent_timeout > 0 or snap.recent_403 > 0):
            ev_txt = "\n".join(e.summary for e in snap.recent_events[-2:])
            self._banner_frame.configure(bg=t["red_bg"], highlightbackground=t["red"])
            self._lbl_banner.configure(bg=t["red_bg"], fg=t["red"], text=ev_txt)
        else:
            self._banner_frame.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"])
            self._lbl_banner.configure(bg=t["card_alt_bg"], fg=t["text_secondary"], text=snap.sub_headline)

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

    def _build_conv_tooltip_text(self, conv: Any) -> str:
        lines = [f"{conv.short_id} · {conv.model} · {format_tokens(conv.context_tokens)}"]
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
        lines.append("近 10 步逐拍明细（旧 → 新）:")
        for idx, st in enumerate(steps, 1):
            t_val = ttfts[idx - 1 - t_offset] if (idx - 1 - t_offset) >= 0 and (idx - 1 - t_offset) < len(ttfts) else None
            p_val = tpss[idx - 1 - s_offset] if (idx - 1 - s_offset) >= 0 and (idx - 1 - s_offset) < len(tpss) else None
            t_str = f"{t_val:5.1f}s" if t_val is not None else "    — "
            p_str = f"{p_val:3.0f}t/s" if p_val is not None else "   — "
            tag = reason_map.get(st.reason, st.reason)
            lines.append(f"  #{idx:<2} 缓存 {st.hit_rate:5.1f}% │ 首字 {t_str} │ {p_str} [{tag}]")
        return "\n".join(lines)

    def _show_conv_tooltip(self, row_idx: int, event: tk.Event) -> None:
        if self._closing or row_idx < 0 or row_idx >= len(self._conv_hover_texts):
            return
        text = self._conv_hover_texts[row_idx]
        if not text:
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

    def _hide_conv_tooltip(self) -> None:
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None

    def _draw_conversation_bars(self, snap: MonitorSnapshot) -> None:
        t = self.theme
        convs = snap.active_conversations[:4]
        all_keys = ("id", "model", "ctx", "ttft_canvas", "ttft_val", "canvas", "hit", "speed")

        if not convs:
            self._conv_hover_texts = ["", "", "", ""]
            for row_ui in self._conv_rows:
                for key in all_keys:
                    row_ui[key].grid_remove()
            self._lbl_convs_empty.grid(row=0, column=0, columnspan=8, sticky="w", pady=4)
            return

        self._lbl_convs_empty.grid_remove()
        for idx, row_ui in enumerate(self._conv_rows):
            if idx >= len(convs):
                self._conv_hover_texts[idx] = ""
                for key in all_keys:
                    row_ui[key].grid_remove()
                continue

            conv = convs[idx]
            self._conv_hover_texts[idx] = self._build_conv_tooltip_text(conv)
            row_ui["id"].configure(text=conv.short_id, fg=t["text_primary"])
            row_ui["model"].configure(text=conv.model, fg=t["text_secondary"])
            row_ui["ctx"].configure(text=format_tokens(conv.context_tokens), fg=t["text_secondary"])

            if conv.ttft_sec is not None:
                ttft_txt = f"{conv.ttft_sec:.0f}s" if conv.ttft_sec >= 100 else f"{conv.ttft_sec:.1f}s"
            else:
                ttft_txt = "—"
            row_ui["ttft_val"].configure(text=ttft_txt, fg=self._ttft_color(conv.ttft_sec))

            latest_cls = conv.recent_steps[-1].classification if conv.recent_steps else ""
            if conv.latest_hit_rate >= 70:
                hit_col = t["green"]
            elif conv.latest_hit_rate >= 20 or latest_cls == "expected_cold":
                hit_col = t["orange"]
            else:
                hit_col = t["red"]
            row_ui["hit"].configure(text=f"{conv.latest_hit_rate:.1f}%", fg=hit_col)

            sp_txt = f"{conv.tps:.0f} t/s" if conv.tps is not None else "—"
            sp_col = t["orange"] if conv.speed_status == "slow" else t["cyan"]
            row_ui["speed"].configure(text=sp_txt, fg=sp_col)

            row_ui["id"].grid(row=idx, column=0, sticky="w", padx=(0, 5), pady=3)
            row_ui["model"].grid(row=idx, column=1, sticky="w", padx=(0, 6), pady=3)
            row_ui["ctx"].grid(row=idx, column=2, sticky="e", padx=(0, 8), pady=3)
            row_ui["ttft_canvas"].grid(row=idx, column=3, sticky="e", padx=(0, 4), pady=3)
            row_ui["ttft_val"].grid(row=idx, column=4, sticky="e", padx=(0, 8), pady=3)
            row_ui["canvas"].grid(row=idx, column=5, sticky="e", padx=(0, 7), pady=3)
            row_ui["hit"].grid(row=idx, column=6, sticky="e", padx=(0, 6), pady=3)
            row_ui["speed"].grid(row=idx, column=7, sticky="e", pady=3)

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

            # Draw 10-step Cache hit-rate micro-bars
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
                elif st.classification == "expected_cold":
                    col = t["orange"]
                    h_ratio = 0.56
                else:
                    col = t["red"]
                    h_ratio = 1.0
                top_y = bar_bot - int(round((bar_bot - bar_top) * h_ratio))
                c.create_rectangle(x0, top_y, x1, bar_bot, fill=col, outline="")

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
