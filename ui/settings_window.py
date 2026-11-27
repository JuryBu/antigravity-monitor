"""Lightweight plain-Chinese settings modal with live preview, flat grid layout, and hot-reload patch save."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import filedialog
from typing import Any, Callable, Dict, List, Optional, Tuple

from ui.drag_proxy import LightweightProxyDragger, resolve_root_hwnd

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

_THEME_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("midnight_blue", "午夜星蓝"),
    ("obsidian_dark", "深炭极简"),
)

_CLEAN_MODE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("recycle", "移入回收站"),
    ("archive", "先归档校验再删"),
    ("delete", "直接删除"),
)

_UPDATE_MODE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("download", "后台下载，下次生效"),
    ("auto_restart", "自动更新并重启"),
    ("notify", "仅提示"),
)


def _clamp_int(raw: str, low: int, high: int, label: str) -> int:
    text = str(raw).strip()
    try:
        fval = float(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"「{label}」请输入有效数字") from exc
    if not math.isfinite(fval):
        raise ValueError(f"「{label}」请输入有效数字")
    try:
        val = int(round(fval))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"「{label}」请输入有效数字") from exc
    return max(low, min(high, val))


def _clamp_float(raw: str, low: float, high: float, label: str) -> float:
    text = str(raw).strip()
    try:
        val = float(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"「{label}」请输入有效数字") from exc
    if not math.isfinite(val):
        raise ValueError(f"「{label}」请输入有效数字")
    return max(low, min(high, val))


class MonitorSettingsWindow:
    """Borderless opaque settings Toplevel using a flat grid layout for zero-lag Win32 native dragging.

    Plan A behavior:
    - Theme & topmost sync live with the main monitor (and roll back cleanly on cancel/close).
    - The settings window itself stays fully opaque (no Win32 WS_EX_LAYERED compositing overhead),
      while the transparency slider previews the main capsule & expanded panel live.
    """

    def __init__(
        self,
        parent: tk.Tk,
        theme: Dict[str, str],
        get_config: Callable[[], Dict[str, Any]],
        on_save_patch: Callable[[Dict[str, Any]], Dict[str, Any]],
        on_preview_alpha: Optional[Callable[[float], None]] = None,
        on_preview_theme: Optional[Callable[[str], None]] = None,
        on_preview_topmost: Optional[Callable[[bool], None]] = None,
        on_drag_state_changed: Optional[Callable[[bool], None]] = None,
        on_closed: Optional[Callable[[], None]] = None,
        apply_rounding_fn: Optional[Callable[[int], None]] = None,
        screen_bounds_fn: Optional[Callable[[tk.Tk], Tuple[int, int, int, int]]] = None,
    ) -> None:
        self._parent = parent
        self.theme: Dict[str, str] = theme
        self._get_config = get_config
        self._on_save_patch = on_save_patch
        self._on_preview_alpha = on_preview_alpha
        self._on_preview_theme = on_preview_theme
        self._on_preview_topmost = on_preview_topmost
        self._on_drag_state_changed = on_drag_state_changed
        self._on_closed = on_closed
        self._apply_rounding_fn = apply_rounding_fn
        self._screen_bounds_fn = screen_bounds_fn

        self._initial_config: Dict[str, Any] = {}
        self._initial_alpha: float = 0.95
        self._initial_theme: str = "midnight_blue"
        self._initial_topmost: bool = True
        self._preview_dirty: bool = False
        self._theme_preview_dirty: bool = False
        self._topmost_preview_dirty: bool = False
        self._committed: bool = False
        self._loading_form: bool = False
        self._dialog_open: bool = False
        self._dragging: bool = False
        self._closed: bool = False
        self._after_ids: List[str] = []

        self.win = tk.Toplevel(parent)
        self.win.title("监控器参数设置")
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        # Intentionally DO NOT set -alpha here: keeping the settings modal fully opaque avoids
        # Win32 WS_EX_LAYERED per-child compositing lag during drags and keeps form text crisp.
        self.win.attributes("-topmost", True)
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.bind("<FocusOut>", self._on_focus_out)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._dragger: Optional[LightweightProxyDragger] = LightweightProxyDragger(
            self.win,
            move_threshold=3,
            on_drag_start=self._on_dragger_started,
            on_drag_end=self._on_dragger_ended,
            on_drag_cancel=self._on_dragger_cancelled,
            size_provider=self._get_window_size,
            theme_provider=lambda: self.theme,
            title_provider=lambda: "⚙ 参数设置 · 监控器常用设置",
            header_height_provider=self._get_header_height,
            screen_bounds_fn=self._screen_bounds_fn,
        )

        # Form state variables
        self._var_alpha_pct = tk.IntVar(master=self.win, value=95)
        self._var_theme = tk.StringVar(master=self.win, value="midnight_blue")
        self._var_poll_sec = tk.StringVar(master=self.win, value="2.5")
        self._var_topmost = tk.BooleanVar(master=self.win, value=True)
        self._var_start_min = tk.BooleanVar(master=self.win, value=False)
        self._var_notify = tk.BooleanVar(master=self.win, value=True)

        self._var_freshness_sec = tk.StringVar(master=self.win, value="210")
        self._var_large_ctx_k = tk.StringVar(master=self.win, value="100")
        self._var_slow_tps = tk.StringVar(master=self.win, value="40")
        self._var_slow_ttft = tk.StringVar(master=self.win, value="45")

        self._var_slot_auto = tk.BooleanVar(master=self.win, value=False)
        self._var_slot_protect_min = tk.StringVar(master=self.win, value="120")
        self._var_slot_cap = tk.StringVar(master=self.win, value="0")
        self._var_slot_clean_mode = tk.StringVar(master=self.win, value="recycle")
        self._var_slot_archive_dir = tk.StringVar(master=self.win, value="")

        self._var_update_check = tk.BooleanVar(master=self.win, value=True)
        self._var_update_mode = tk.StringVar(master=self.win, value="download")

        self._cards: List[tk.Frame] = []
        self._card_titles: List[tk.Label] = []
        self._sub_frames: List[tk.Frame] = []
        self._field_labels: List[tk.Label] = []
        self._hint_labels: List[tk.Label] = []
        self._entries: List[tk.Entry] = []
        self._checkbuttons: List[tk.Checkbutton] = []
        self._theme_btns: Dict[str, tk.Label] = {}
        self._clean_mode_btns: Dict[str, tk.Label] = {}
        self._update_mode_btns: Dict[str, tk.Label] = {}
        self._small_btns: List[tk.Label] = []
        self._act_btns: Dict[str, tk.Label] = {}

        # Single bordered surface frame (eliminates redundant outer wrapper HWND)
        self._surface = tk.Frame(self.win, padx=14, pady=11, highlightthickness=1)
        self._surface.pack(fill="both", expand=True)

        self._build_ui()
        self._load_from_config()
        self.apply_theme(theme)

    def _get_window_size(self) -> Tuple[int, int]:
        try:
            w = int(self.win.winfo_width())
            h = int(self.win.winfo_height())
            if w > 100 and h > 100:
                return (w, h)
        except tk.TclError:
            pass
        try:
            self.win.update_idletasks()
            return (
                max(760, int(self.win.winfo_reqwidth())),
                max(450, int(self.win.winfo_reqheight())),
            )
        except tk.TclError:
            return (780, 470)

    def _make_card(self, parent: tk.Widget, row: int, col: int, title: str, cols: int = 3) -> tk.Frame:
        card = tk.Frame(parent, padx=10, pady=8, highlightthickness=1)
        card.grid(
            row=row,
            column=col,
            sticky="nsew",
            padx=(0 if col == 0 else 5, 0 if col == 1 else 5),
            pady=(0 if row == 0 else 5, 0 if row == 1 else 5),
        )
        card.grid_columnconfigure(0, weight=1)
        lbl_t = tk.Label(card, text=title, font=(UI_FONT, 9, "bold"), anchor="w")
        lbl_t.grid(row=0, column=0, columnspan=cols, sticky="ew", pady=(0, 6))
        self._cards.append(card)
        self._card_titles.append(lbl_t)
        return card

    def _make_entry(self, parent: tk.Widget, var: tk.StringVar, width: int = 7) -> tk.Entry:
        ent = tk.Entry(
            parent,
            textvariable=var,
            font=(MONO_FONT, 8),
            width=width,
            bd=0,
            highlightthickness=1,
            justify="right",
        )
        self._entries.append(ent)
        return ent

    def _make_checkbox(
        self,
        parent: tk.Widget,
        text: str,
        var: tk.BooleanVar,
        command: Optional[Callable[[], None]] = None,
    ) -> tk.Checkbutton:
        cb = tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            command=command,
            font=(UI_FONT, 8),
            anchor="w",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self._checkbuttons.append(cb)
        return cb

    def _build_ui(self) -> None:
        # Draggable header bar
        self._hdr = tk.Frame(self._surface)
        self._hdr.pack(fill="x", pady=(0, 8))

        self._btn_close = tk.Label(self._hdr, text="✕", font=(UI_FONT, 9, "bold"), padx=6, pady=1, cursor="hand2")
        self._btn_close.pack(side="right", padx=(6, 0))
        self._btn_close.bind("<Button-1>", lambda _e: self.close())

        self._lbl_badge = tk.Label(self._hdr, text="⚙ 参数设置", font=(UI_FONT, 8, "bold"), padx=6, pady=1)
        self._lbl_badge.pack(side="left", padx=(0, 6))
        self._lbl_title = tk.Label(self._hdr, text="监控器常用设置", font=(UI_FONT, 10, "bold"), anchor="w")
        self._lbl_title.pack(side="left")
        self._lbl_sub = tk.Label(self._hdr, text="修改后点「保存并生效」即时热重载，无需重启", font=(UI_FONT, 8), anchor="w")
        self._lbl_sub.pack(side="left", padx=(8, 0))

        for w in (self._hdr, self._lbl_badge, self._lbl_title, self._lbl_sub):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)
            w.bind("<ButtonRelease-1>", self._on_drag_end)
        # Allow grabbing the top/outer margin of _surface directly without intercepting child widgets
        self._surface.bind("<ButtonPress-1>", self._on_surface_drag_start)
        self._surface.bind("<B1-Motion>", self._on_surface_drag_move)
        self._surface.bind("<ButtonRelease-1>", self._on_surface_drag_end)

        # 2x2 grouped cards using flat internal grids (drastically cuts Win32 child HWND count!)
        self._grid_wrap = tk.Frame(self._surface)
        self._grid_wrap.pack(fill="both", expand=True, pady=(0, 8))
        self._grid_wrap.grid_columnconfigure(0, weight=1, uniform="setcol")
        self._grid_wrap.grid_columnconfigure(1, weight=1, uniform="setcol")
        self._grid_wrap.grid_rowconfigure(0, weight=1)
        self._grid_wrap.grid_rowconfigure(1, weight=1)

        # --- Group 1: 外观与刷新 (flat 3-column grid: label | control | unit/value) ---
        c1 = self._make_card(self._grid_wrap, 0, 0, "🎨 外观与刷新", cols=3)

        lbl_a = tk.Label(c1, text="主窗透明度", font=(UI_FONT, 8), anchor="w")
        lbl_a.grid(row=1, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_a)
        self._scale_alpha = tk.Scale(
            c1,
            from_=72,
            to=100,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self._var_alpha_pct,
            command=self._on_alpha_slider_changed,
            length=150,
            sliderlength=14,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self._scale_alpha.grid(row=1, column=1, sticky="e", padx=(6, 4), pady=(0, 5))
        self._lbl_alpha_val = tk.Label(c1, text="95%", font=(MONO_FONT, 8, "bold"), width=4, anchor="e")
        self._lbl_alpha_val.grid(row=1, column=2, sticky="e", pady=(0, 5))

        lbl_th = tk.Label(c1, text="界面主题", font=(UI_FONT, 8), anchor="w")
        lbl_th.grid(row=2, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_th)
        th_wrap = tk.Frame(c1)
        th_wrap.grid(row=2, column=1, columnspan=2, sticky="e", pady=(0, 5))
        self._sub_frames.append(th_wrap)
        for idx_t, (key, txt) in enumerate(_THEME_OPTIONS):
            btn = tk.Label(th_wrap, text=txt, font=(UI_FONT, 8), padx=8, pady=2, cursor="hand2")
            btn.pack(side="left", padx=(4 if idx_t > 0 else 0, 0))
            btn.bind("<Button-1>", lambda _e, k=key: self._select_theme_option(k))
            self._theme_btns[key] = btn

        lbl_p = tk.Label(c1, text="采样刷新间隔 (1.0–30.0)", font=(UI_FONT, 8), anchor="w")
        lbl_p.grid(row=3, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_p)
        self._ent_poll = self._make_entry(c1, self._var_poll_sec, width=7)
        self._ent_poll.grid(row=3, column=1, sticky="e", pady=(0, 5))
        lbl_pu = tk.Label(c1, text="秒", font=(UI_FONT, 8), width=4, anchor="w")
        lbl_pu.grid(row=3, column=2, sticky="w", padx=(4, 0), pady=(0, 5))
        self._field_labels.append(lbl_pu)

        self._make_checkbox(c1, "窗口始终置顶", self._var_topmost, command=self._on_topmost_toggled).grid(
            row=4, column=0, sticky="w", pady=(2, 2)
        )
        self._make_checkbox(c1, "启动时缩进托盘", self._var_start_min).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=(2, 2)
        )

        self._make_checkbox(c1, "开启系统异常提醒通知", self._var_notify).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 3)
        )

        lbl_alpha_hint = tk.Label(
            c1,
            text="滑块实时预览主窗口透明度（设置窗保持不透明更跟手），取消自动还原",
            font=(UI_FONT, 7),
            anchor="w",
        )
        lbl_alpha_hint.grid(row=6, column=0, columnspan=3, sticky="ew")
        self._hint_labels.append(lbl_alpha_hint)

        # --- Group 2: 缓存保鲜与测速 (flat 3-column grid: entries aligned vertically!) ---
        c2 = self._make_card(self._grid_wrap, 0, 1, "⚡ 缓存保鲜与测速", cols=3)

        lbl_fr = tk.Label(c2, text="保鲜提醒基准 (60–900)", font=(UI_FONT, 8), anchor="w")
        lbl_fr.grid(row=1, column=0, sticky="w", pady=(0, 2))
        self._field_labels.append(lbl_fr)
        self._ent_freshness = self._make_entry(c2, self._var_freshness_sec, width=7)
        self._ent_freshness.grid(row=1, column=1, sticky="e", pady=(0, 2))
        lbl_fru = tk.Label(c2, text="秒", font=(UI_FONT, 8), width=5, anchor="w")
        lbl_fru.grid(row=1, column=2, sticky="w", padx=(4, 0), pady=(0, 2))
        self._field_labels.append(lbl_fru)

        lbl_fr_hint = tk.Label(
            c2,
            text="按模型自动适配（默认 Carbon 180s / Neon 240s），并同步闲置重热窗",
            font=(UI_FONT, 7),
            anchor="w",
        )
        lbl_fr_hint.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self._hint_labels.append(lbl_fr_hint)

        lbl_lc = tk.Label(c2, text="触发保鲜的对话大小 (16–1000)", font=(UI_FONT, 8), anchor="w")
        lbl_lc.grid(row=3, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_lc)
        self._ent_large_ctx = self._make_entry(c2, self._var_large_ctx_k, width=7)
        self._ent_large_ctx.grid(row=3, column=1, sticky="e", pady=(0, 5))
        lbl_lcu = tk.Label(c2, text="K", font=(MONO_FONT, 8), width=5, anchor="w")
        lbl_lcu.grid(row=3, column=2, sticky="w", padx=(4, 0), pady=(0, 5))
        self._field_labels.append(lbl_lcu)

        lbl_tps = tk.Label(c2, text="生成速度偏慢判定 (默认 40)", font=(UI_FONT, 8), anchor="w")
        lbl_tps.grid(row=4, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_tps)
        self._ent_slow_tps = self._make_entry(c2, self._var_slow_tps, width=7)
        self._ent_slow_tps.grid(row=4, column=1, sticky="e", pady=(0, 5))
        lbl_tpsu = tk.Label(c2, text="tok/s", font=(MONO_FONT, 8), width=5, anchor="w")
        lbl_tpsu.grid(row=4, column=2, sticky="w", padx=(4, 0), pady=(0, 5))
        self._field_labels.append(lbl_tpsu)

        lbl_ttft = tk.Label(c2, text="首字等待偏慢基线 (默认 45)", font=(UI_FONT, 8), anchor="w")
        lbl_ttft.grid(row=5, column=0, sticky="w")
        self._field_labels.append(lbl_ttft)
        self._ent_slow_ttft = self._make_entry(c2, self._var_slow_ttft, width=7)
        self._ent_slow_ttft.grid(row=5, column=1, sticky="e")
        lbl_ttftu = tk.Label(c2, text="秒", font=(UI_FONT, 8), width=5, anchor="w")
        lbl_ttftu.grid(row=5, column=2, sticky="w", padx=(4, 0))
        self._field_labels.append(lbl_ttftu)

        # --- Group 3: 本地会话清理 (flat 3-column grid; keep total widget count < 70) ---
        c3 = self._make_card(self._grid_wrap, 1, 0, "🧹 本地会话清理", cols=3)

        self._make_checkbox(c3, "自动清理超期老旧子代理（主对话永久保护）", self._var_slot_auto).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )

        lbl_sp = tk.Label(c3, text="子代理保护时长 (分，硬下限 30)", font=(UI_FONT, 8), anchor="w")
        lbl_sp.grid(row=2, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_sp)
        self._ent_slot_protect = self._make_entry(c3, self._var_slot_protect_min, width=7)
        self._ent_slot_protect.grid(row=2, column=1, columnspan=2, sticky="e", pady=(0, 5))

        lbl_cap = tk.Label(c3, text="旧版槽位上限兼容 (0=不限或100-5000)", font=(UI_FONT, 8), anchor="w")
        lbl_cap.grid(row=3, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_cap)
        self._ent_slot_cap = self._make_entry(c3, self._var_slot_cap, width=7)
        self._ent_slot_cap.grid(row=3, column=1, columnspan=2, sticky="e", pady=(0, 5))

        lbl_cm = tk.Label(c3, text="清理方式", font=(UI_FONT, 8), anchor="w")
        lbl_cm.grid(row=4, column=0, sticky="w", pady=(0, 5))
        self._field_labels.append(lbl_cm)
        cm_wrap = tk.Frame(c3)
        cm_wrap.grid(row=4, column=1, columnspan=2, sticky="e", pady=(0, 5))
        self._sub_frames.append(cm_wrap)
        for idx_c, (key, txt) in enumerate(_CLEAN_MODE_OPTIONS):
            btn = tk.Label(cm_wrap, text=txt, font=(UI_FONT, 8), padx=6, pady=2, cursor="hand2")
            btn.pack(side="left", padx=(3 if idx_c > 0 else 0, 0))
            btn.bind("<Button-1>", lambda _e, k=key: self._select_clean_mode(k))
            self._clean_mode_btns[key] = btn

        lbl_ad = tk.Label(c3, text="归档目录", font=(UI_FONT, 8), anchor="w")
        lbl_ad.grid(row=5, column=0, sticky="w", pady=(0, 3))
        self._field_labels.append(lbl_ad)
        self._ent_archive_dir = tk.Entry(
            c3,
            textvariable=self._var_slot_archive_dir,
            font=(MONO_FONT, 8),
            bd=0,
            highlightthickness=1,
            justify="left",
        )
        self._ent_archive_dir.grid(row=5, column=1, sticky="ew", padx=(4, 4), pady=(0, 3))
        self._entries.append(self._ent_archive_dir)
        c3.grid_columnconfigure(1, weight=1)
        self._btn_browse_archive = tk.Label(c3, text="选择目录…", font=(UI_FONT, 8), padx=6, pady=2, cursor="hand2", highlightthickness=1)
        self._btn_browse_archive.grid(row=5, column=2, sticky="e", pady=(0, 3))
        self._btn_browse_archive.bind("<Button-1>", lambda _e: self._browse_archive_dir())
        self._small_btns.append(self._btn_browse_archive)

        lbl_ad_hint = tk.Label(
            c3,
            text="v2.35+ 默认无限历史（上限填 0）；旧版可填 500 启用告警；归档留空用默认位置",
            font=(UI_FONT, 7),
            anchor="w",
        )
        lbl_ad_hint.grid(row=6, column=0, columnspan=3, sticky="ew")
        self._hint_labels.append(lbl_ad_hint)

        # --- Group 4: 版本更新 (flat grid) ---
        c4 = self._make_card(self._grid_wrap, 1, 1, "🔄 版本更新", cols=3)

        self._make_checkbox(c4, "后台定时检查新版本", self._var_update_check).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )

        lbl_um = tk.Label(c4, text="发现新版本时的处理方式", font=(UI_FONT, 8), anchor="w")
        lbl_um.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self._field_labels.append(lbl_um)

        um_wrap = tk.Frame(c4)
        um_wrap.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self._sub_frames.append(um_wrap)
        for idx_u, (key, txt) in enumerate(_UPDATE_MODE_OPTIONS):
            btn = tk.Label(um_wrap, text=txt, font=(UI_FONT, 8), padx=6, pady=3, cursor="hand2")
            btn.pack(side="left", padx=(4 if idx_u > 0 else 0, 0))
            btn.bind("<Button-1>", lambda _e, k=key: self._select_update_mode(k))
            self._update_mode_btns[key] = btn

        lbl_upd_hint = tk.Label(
            c4,
            text="默认在后台静默下载新版本，下次打开自动生效，\n不会在工作途中擅自打断或重开窗口。",
            font=(UI_FONT, 7),
            anchor="w",
            justify="left",
        )
        lbl_upd_hint.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._hint_labels.append(lbl_upd_hint)

        # Single bordered feedback label (no extra wrapper frame HWND)
        self._lbl_feedback = tk.Label(
            self._surface,
            text="准备就绪 · 仅修改上方常用项，不会覆盖你手动改过的其它高级配置",
            font=(UI_FONT, 8),
            anchor="w",
            padx=8,
            pady=5,
            highlightthickness=1,
        )
        self._lbl_feedback.pack(fill="x", pady=(0, 8))

        # Bottom action buttons
        self._act_row = tk.Frame(self._surface)
        self._act_row.pack(fill="x")
        actions = [
            ("reset", "恢复推荐默认", self._handle_reset_defaults),
            ("cancel", "取消", self.close),
            ("save", "保存并生效", self._handle_save),
        ]
        for idx, (k, txt, cmd) in enumerate(actions):
            self._act_row.grid_columnconfigure(idx, weight=2 if k == "save" else 1, uniform="setact")
            btn = tk.Label(
                self._act_row,
                text=txt,
                font=(UI_FONT, 8, "bold" if k == "save" else "normal"),
                padx=8,
                pady=5,
                cursor="hand2",
                highlightthickness=1,
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 5, 0 if idx == 2 else 5))
            btn.bind("<Button-1>", lambda _e, fn=cmd: fn())
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=self.theme["btn_hover"]))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=self.theme["btn_bg"]))
            self._act_btns[k] = btn

    def _load_from_config(self) -> None:
        cfg = dict(self._get_config() or {}) if self._get_config is not None else {}
        self._initial_config = dict(cfg)
        alpha = float(cfg.get("window_alpha", 0.95) or 0.95)
        alpha = max(0.72, min(1.0, alpha))
        self._initial_alpha = alpha

        theme_key = str(cfg.get("theme", "midnight_blue") or "midnight_blue")
        if theme_key not in ("midnight_blue", "obsidian_dark"):
            theme_key = "midnight_blue"
        self._initial_theme = theme_key

        topmost_val = bool(cfg.get("topmost", True))
        self._initial_topmost = topmost_val

        self._preview_dirty = False
        self._theme_preview_dirty = False
        self._topmost_preview_dirty = False
        self._committed = False

        self._loading_form = True
        try:
            alpha_pct = max(72, min(100, int(round(alpha * 100.0))))
            self._var_alpha_pct.set(alpha_pct)
            self._lbl_alpha_val.configure(text=f"{alpha_pct}%")
            self._var_theme.set(theme_key)

            poll_ms = int(cfg.get("poll_interval_ms", 2500) or 2500)
            poll_sec = max(1.0, min(30.0, poll_ms / 1000.0))
            self._var_poll_sec.set(f"{poll_sec:.1f}".rstrip("0").rstrip(".") if "." in f"{poll_sec:.1f}" else f"{poll_sec:.1f}")
            self._var_topmost.set(topmost_val)
            self._var_start_min.set(bool(cfg.get("start_minimized", False)))
            self._var_notify.set(bool(cfg.get("notifications_enabled", True)))

            fresh_sec = int(cfg.get("freshness_window_sec", 210) or 210)
            self._var_freshness_sec.set(str(max(60, min(900, fresh_sec))))

            lc_tok = int(cfg.get("large_context_threshold", 100000) or 100000)
            lc_k = max(16, min(1000, int(round(lc_tok / 1000.0))))
            self._var_large_ctx_k.set(str(lc_k))

            slow_tps = float(cfg.get("slow_tps_threshold", 40.0) or 40.0)
            self._var_slow_tps.set(f"{slow_tps:g}")
            slow_ttft = float(cfg.get("slow_ttft_threshold_sec", 45.0) or 45.0)
            self._var_slow_ttft.set(f"{slow_ttft:g}")

            self._var_slot_auto.set(bool(cfg.get("slot_auto_clean_enabled", False)))
            prot_min = int(cfg.get("slot_protect_age_min", 120) or 120)
            self._var_slot_protect_min.set(str(max(30, min(10080, prot_min))))
            raw_cap = cfg.get("slot_cap", 0)
            if raw_cap is None or isinstance(raw_cap, bool):
                cap_val = 0
            else:
                try:
                    f_cap = float(raw_cap)
                    cap_val = int(f_cap) if math.isfinite(f_cap) else 0
                except (TypeError, ValueError, OverflowError):
                    cap_val = 0
            self._var_slot_cap.set(str(0 if cap_val <= 0 else max(100, min(5000, cap_val))))

            cmode = str(cfg.get("slot_clean_mode", "recycle") or "recycle")
            if cmode not in ("recycle", "archive", "delete"):
                cmode = "recycle"
            self._var_slot_clean_mode.set(cmode)
            self._var_slot_archive_dir.set(str(cfg.get("slot_archive_dir", "") or "").strip())

            self._var_update_check.set(bool(cfg.get("update_check_enabled", True)))
            umode = str(cfg.get("update_mode", "download") or "download")
            if umode not in ("download", "auto_restart", "notify"):
                umode = "auto_restart" if bool(cfg.get("auto_update_restart", False)) else "download"
            self._var_update_mode.set(umode)
        finally:
            self._loading_form = False

        try:
            self.win.attributes("-topmost", topmost_val)
        except tk.TclError:
            pass
        self._refresh_option_pills()

    def sync_topmost(self, topmost: bool) -> None:
        """Keep settings window topmost attribute & checkbox synced when toggled from main bar."""
        if self._closed:
            return
        val = bool(topmost)
        self._initial_topmost = val
        self._topmost_preview_dirty = False
        try:
            self._var_topmost.set(val)
            if not self._dialog_open:
                self.win.attributes("-topmost", val)
        except tk.TclError:
            pass

    def _on_alpha_slider_changed(self, raw_val: Any) -> None:
        try:
            pct = max(72, min(100, int(round(float(raw_val)))))
        except (TypeError, ValueError):
            return
        self._lbl_alpha_val.configure(text=f"{pct}%")
        if self._loading_form or self._closed:
            return
        alpha = round(pct / 100.0, 2)
        self._preview_dirty = True
        if self._on_preview_alpha is not None:
            try:
                self._on_preview_alpha(alpha)
            except Exception:
                pass

    def _on_topmost_toggled(self) -> None:
        if self._loading_form or self._closed:
            return
        val = bool(self._var_topmost.get())
        self._topmost_preview_dirty = True
        try:
            self.win.attributes("-topmost", val)
        except tk.TclError:
            pass
        if self._on_preview_topmost is not None:
            try:
                self._on_preview_topmost(val)
            except Exception:
                pass

    def _select_theme_option(self, key: str) -> None:
        if key in self._theme_btns:
            self._var_theme.set(key)
            self._refresh_option_pills()
            if not self._loading_form and not self._closed:
                self._theme_preview_dirty = True
                if self._on_preview_theme is not None:
                    try:
                        self._on_preview_theme(key)
                    except Exception:
                        pass

    def _select_clean_mode(self, key: str) -> None:
        if key in self._clean_mode_btns:
            self._var_slot_clean_mode.set(key)
            self._refresh_option_pills()

    def _select_update_mode(self, key: str) -> None:
        if key in self._update_mode_btns:
            self._var_update_mode.set(key)
            self._refresh_option_pills()

    def _refresh_option_pills(self) -> None:
        t = self.theme
        cur_theme = self._var_theme.get()
        for k, btn in self._theme_btns.items():
            active = k == cur_theme
            btn.configure(
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )
        cur_cm = self._var_slot_clean_mode.get()
        for k, btn in self._clean_mode_btns.items():
            active = k == cur_cm
            btn.configure(
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )
        cur_um = self._var_update_mode.get()
        for k, btn in self._update_mode_btns.items():
            active = k == cur_um
            btn.configure(
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )

    def _browse_archive_dir(self) -> None:
        if self._closed or self._dialog_open:
            return
        self._dialog_open = True
        prev_topmost = True
        try:
            prev_topmost = bool(self.win.attributes("-topmost"))
        except tk.TclError:
            prev_topmost = True
        try:
            try:
                self.win.attributes("-topmost", False)
            except tk.TclError:
                pass
            initial = self._var_slot_archive_dir.get().strip()
            chosen = filedialog.askdirectory(
                parent=self.win,
                title="选择子代理归档目录",
                initialdir=initial if initial else None,
            )
            if chosen and not self._closed:
                self._var_slot_archive_dir.set(str(chosen).strip())
        finally:
            self._dialog_open = False
            if not self._closed:
                try:
                    self.win.attributes("-topmost", prev_topmost)
                    self.win.lift()
                except tk.TclError:
                    pass

    def _handle_reset_defaults(self) -> None:
        if self._closed:
            return
        self._var_alpha_pct.set(95)
        self._on_alpha_slider_changed(95)
        self._select_theme_option("midnight_blue")
        self._var_poll_sec.set("2.5")
        self._var_topmost.set(True)
        self._on_topmost_toggled()
        self._var_start_min.set(False)
        self._var_notify.set(True)
        self._var_freshness_sec.set("210")
        self._var_large_ctx_k.set("100")
        self._var_slow_tps.set("40")
        self._var_slow_ttft.set("45")
        self._var_slot_auto.set(False)
        self._var_slot_protect_min.set("120")
        self._var_slot_cap.set("0")
        self._var_slot_clean_mode.set("recycle")
        self._var_slot_archive_dir.set("")
        self._var_update_check.set(True)
        self._var_update_mode.set("download")
        self._refresh_option_pills()
        self._lbl_feedback.configure(
            text="已填入推荐默认值，点击「保存并生效」后写入配置",
            fg=self.theme["cyan"],
        )

    def _build_patch_from_form(self) -> Dict[str, Any]:
        alpha_pct = max(72, min(100, int(self._var_alpha_pct.get())))
        window_alpha = round(alpha_pct / 100.0, 2)
        poll_sec = _clamp_float(self._var_poll_sec.get(), 1.0, 30.0, "采样刷新间隔")
        poll_interval_ms = max(1000, min(30000, int(round(poll_sec * 1000.0))))
        freshness_sec = _clamp_int(self._var_freshness_sec.get(), 60, 900, "保鲜提醒基准")
        large_ctx_k = _clamp_int(self._var_large_ctx_k.get(), 16, 1000, "触发保鲜的对话大小")
        slow_tps = round(_clamp_float(self._var_slow_tps.get(), 5.0, 200.0, "生成速度偏慢判定"), 1)
        slow_ttft = round(_clamp_float(self._var_slow_ttft.get(), 10.0, 300.0, "首字等待偏慢基线"), 1)
        slot_protect_min = _clamp_int(self._var_slot_protect_min.get(), 30, 10080, "子代理保护时长")
        raw_cap_txt = self._var_slot_cap.get().strip()
        if not raw_cap_txt:
            slot_cap = 0
        else:
            raw_cap_val = _clamp_int(raw_cap_txt, 0, 5000, "旧版槽位上限兼容")
            slot_cap = 0 if raw_cap_val <= 0 else max(100, raw_cap_val)

        theme_val = self._var_theme.get()
        if theme_val not in ("midnight_blue", "obsidian_dark"):
            theme_val = "midnight_blue"
        clean_mode = self._var_slot_clean_mode.get()
        if clean_mode not in ("recycle", "archive", "delete"):
            clean_mode = "recycle"
        upd_enabled = bool(self._var_update_check.get())
        upd_mode = self._var_update_mode.get()
        if upd_mode not in ("download", "auto_restart", "notify"):
            upd_mode = "download"

        patch: Dict[str, Any] = {
            "window_alpha": window_alpha,
            "theme": theme_val,
            "poll_interval_ms": poll_interval_ms,
            "topmost": bool(self._var_topmost.get()),
            "start_minimized": bool(self._var_start_min.get()),
            "notifications_enabled": bool(self._var_notify.get()),
            "freshness_window_sec": freshness_sec,
            "large_context_threshold": large_ctx_k * 1000,
            "slow_tps_threshold": slow_tps,
            "slow_ttft_threshold_sec": slow_ttft,
            "slot_cap": slot_cap,
            "slot_auto_clean_enabled": bool(self._var_slot_auto.get()),
            "slot_protect_age_min": slot_protect_min,
            "slot_clean_mode": clean_mode,
            "slot_archive_dir": self._var_slot_archive_dir.get().strip(),
            "update_check_enabled": upd_enabled,
            "update_mode": upd_mode,
            "auto_update_restart": bool(upd_enabled and upd_mode == "auto_restart"),
        }

        # Keep idle_rewarm_sec strictly aligned whenever freshness_window_sec is modified
        init_fresh = int(self._initial_config.get("freshness_window_sec", 210) or 210)
        if freshness_sec != init_fresh:
            patch["idle_rewarm_sec"] = freshness_sec

        return patch

    def _handle_save(self) -> None:
        if self._closed:
            return
        try:
            patch = self._build_patch_from_form()
        except ValueError as exc:
            self._lbl_feedback.configure(text=str(exc), fg=self.theme["red"])
            return

        if self._on_save_patch is not None:
            try:
                saved = self._on_save_patch(patch) or {}
            except Exception as exc:
                self._lbl_feedback.configure(text=f"保存失败：{exc}", fg=self.theme["red"])
                return
            self._initial_alpha = float(saved.get("window_alpha", patch["window_alpha"]))
            self._initial_theme = str(saved.get("theme", patch["theme"]))
            self._initial_topmost = bool(saved.get("topmost", patch["topmost"]))
        else:
            self._initial_alpha = float(patch["window_alpha"])
            self._initial_theme = str(patch["theme"])
            self._initial_topmost = bool(patch["topmost"])

        self._committed = True
        self._preview_dirty = False
        self._theme_preview_dirty = False
        self._topmost_preview_dirty = False
        self.close()

    def apply_theme(self, theme: Dict[str, str]) -> None:
        self.theme = theme
        t = theme
        self.win.configure(bg=t["border"])
        self._surface.configure(bg=t["panel_bg"], highlightbackground=t["border"], highlightcolor=t["border"])
        self._hdr.configure(bg=t["panel_bg"])
        self._lbl_badge.configure(bg=t["btn_hover"], fg=t["cyan"])
        self._lbl_title.configure(bg=t["panel_bg"], fg=t["text_primary"])
        self._lbl_sub.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._btn_close.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._grid_wrap.configure(bg=t["panel_bg"])

        for card in self._cards:
            card.configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        for ct in self._card_titles:
            ct.configure(bg=t["card_bg"], fg=t["text_primary"])
        for sf in self._sub_frames:
            sf.configure(bg=t["card_bg"])
        for fl in self._field_labels:
            fl.configure(bg=t["card_bg"], fg=t["text_secondary"])
        for hl in self._hint_labels:
            hl.configure(bg=t["card_bg"], fg=t["text_muted"])

        self._lbl_alpha_val.configure(bg=t["card_bg"], fg=t["cyan"])
        self._scale_alpha.configure(
            bg=t["card_bg"],
            fg=t["text_primary"],
            troughcolor=t["bar_track"],
            activebackground=t["cyan"],
        )

        for ent in self._entries:
            ent.configure(
                bg=t["card_alt_bg"],
                fg=t["text_primary"],
                insertbackground=t["cyan"],
                highlightbackground=t["border_soft"],
                highlightcolor=t["cyan"],
                selectbackground=t["btn_active"],
                selectforeground=t["text_primary"],
            )
        for cb in self._checkbuttons:
            cb.configure(
                bg=t["card_bg"],
                fg=t["text_primary"],
                activebackground=t["card_bg"],
                activeforeground=t["text_primary"],
                selectcolor=t["card_alt_bg"],
            )
        for sb in self._small_btns:
            sb.configure(
                bg=t["btn_bg"],
                fg=t["btn_text"],
                highlightbackground=t["border_soft"],
                highlightcolor=t["border_soft"],
            )
        self._lbl_feedback.configure(
            bg=t["card_alt_bg"],
            fg=t["text_secondary"],
            highlightbackground=t["border_soft"],
            highlightcolor=t["border_soft"],
        )
        self._act_row.configure(bg=t["panel_bg"])
        for k, btn in self._act_btns.items():
            fg = t["cyan"] if k == "save" else t["btn_text"]
            btn.configure(bg=t["btn_bg"], fg=fg, highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._refresh_option_pills()

    def show(self, place_near_parent: bool = True) -> None:
        if self._closed:
            return
        if self._dragger is not None:
            self._dragger.finish_or_cancel(commit=self._dragger.moved, force=True)
        # Restore any uncommitted live previews before reloading form defaults
        self._restore_uncommitted_previews_if_needed()
        self._load_from_config()
        self.win.deiconify()
        self.win.lift()
        self.win.update_idletasks()
        if place_near_parent:
            self._place_window()
        if self._apply_rounding_fn is not None:
            try:
                self._apply_rounding_fn(resolve_root_hwnd(self.win))
            except Exception:
                pass
        if self._dragger is not None and not getattr(self._dragger, "_proxy_warmed", False):
            try:
                aid = self.win.after(40, self._warm_up_drag_proxy)
                self._after_ids.append(aid)
            except tk.TclError:
                pass

    def _warm_up_drag_proxy(self) -> None:
        if not self._closed and self._dragger is not None:
            self._dragger.warm_up()

    def _get_header_height(self) -> int:
        try:
            return max(24, int(self._hdr.winfo_height()))
        except tk.TclError:
            return 28

    def _place_window(self) -> None:
        self.win.update_idletasks()
        w, h = self._get_window_size()
        if self._screen_bounds_fn is not None:
            vx, vy, vr, vb = self._screen_bounds_fn(self._parent)
        else:
            vx, vy, vr, vb = 0, 0, self._parent.winfo_screenwidth(), self._parent.winfo_screenheight()
        px = self._parent.winfo_x()
        py = self._parent.winfo_y()
        pw = self._parent.winfo_width()
        if px - w - 12 >= vx + 8:
            x = px - w - 12
        elif px + pw + 12 + w <= vr - 8:
            x = px + pw + 12
        else:
            x = max(vx + 12, min(vr - w - 12, (vx + vr - w) // 2))
        y = max(vy + 12, min(vb - h - 48, py))
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_focus_out(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self.win:
            return
        if self._dragger is None or self._dragger.proxy_swapping:
            return
        try:
            if self.win.focus_displayof() is not None:
                return
        except tk.TclError:
            pass
        self._dragger.finish_or_cancel(commit=self._dragger.moved)

    def _on_surface_drag_start(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self._surface:
            if int(getattr(event, "y", 999)) <= self._get_header_height() + 18:
                self._on_drag_start(event)

    def _on_surface_drag_move(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self._surface and self._dragger is not None and self._dragger.active:
            self._on_drag_move(event)

    def _on_surface_drag_end(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self._surface and self._dragger is not None and self._dragger.active:
            self._on_drag_end(event)

    def _on_drag_start(self, event: tk.Event) -> None:
        if self._dragger is not None:
            self._dragger.on_start(event)

    def _on_drag_move(self, event: tk.Event) -> None:
        if self._dragger is not None:
            self._dragger.on_move(event)

    def _on_drag_end(self, event: tk.Event) -> None:
        if self._dragger is not None:
            self._dragger.on_end(event)

    def _notify_drag_state(self, active: bool) -> None:
        if self._on_drag_state_changed is not None:
            try:
                self._on_drag_state_changed(bool(active))
            except Exception:
                pass

    def _on_dragger_started(self) -> None:
        self._dragging = True
        self._notify_drag_state(True)

    def _on_dragger_ended(self, _moved: bool) -> None:
        self._dragging = False
        self._notify_drag_state(False)

    def _on_dragger_cancelled(self) -> None:
        self._dragging = False
        self._notify_drag_state(False)

    def check_drag_watchdog(self) -> None:
        if not self._closed and self._dragger is not None:
            self._dragger.check_release_watchdog()

    def _restore_uncommitted_previews_if_needed(self) -> None:
        if self._committed:
            return
        if self._preview_dirty and self._on_preview_alpha is not None:
            self._preview_dirty = False
            try:
                self._on_preview_alpha(self._initial_alpha)
            except Exception:
                pass
        if self._theme_preview_dirty and self._on_preview_theme is not None:
            self._theme_preview_dirty = False
            try:
                self._on_preview_theme(self._initial_theme)
            except Exception:
                pass
        if self._topmost_preview_dirty:
            self._topmost_preview_dirty = False
            if self._on_preview_topmost is not None:
                try:
                    self._on_preview_topmost(self._initial_topmost)
                except Exception:
                    pass

    def _restore_preview_alpha_if_needed(self) -> None:
        self._restore_uncommitted_previews_if_needed()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        was_dragging = self._dragging
        self._dragging = False
        self._dialog_open = False

        dragger = self._dragger
        self._dragger = None
        if dragger is not None:
            dragger.destroy_proxy()

        for aid in list(self._after_ids):
            try:
                self.win.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_ids.clear()

        # Unify uncommitted preview rollback (alpha, theme, topmost) before releasing drag-deferred main renders
        self._restore_uncommitted_previews_if_needed()
        if was_dragging:
            self._notify_drag_state(False)

        self._cards.clear()
        self._card_titles.clear()
        self._sub_frames.clear()
        self._field_labels.clear()
        self._hint_labels.clear()
        self._entries.clear()
        self._checkbuttons.clear()
        self._theme_btns.clear()
        self._clean_mode_btns.clear()
        self._update_mode_btns.clear()
        self._small_btns.clear()
        self._act_btns.clear()

        try:
            self.win.destroy()
        except tk.TclError:
            pass

        cb = self._on_closed
        self._on_closed = None
        self._get_config = None  # type: ignore[assignment]
        self._on_save_patch = None  # type: ignore[assignment]
        self._on_preview_alpha = None
        self._on_preview_theme = None
        self._on_preview_topmost = None
        self._on_drag_state_changed = None
        self._apply_rounding_fn = None
        self._screen_bounds_fn = None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
