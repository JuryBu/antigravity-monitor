"""On-demand long-range single-conversation diagnostics modal (Context, TTFT, Cache)."""

from __future__ import annotations

import datetime
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.log_monitor import (
    CACHE_REASON_CN_MAP,
    ConversationDetailPoint,
    ConversationDetailReport,
    MonitorSnapshot,
    compute_large_ctx_ttft_allowance,
    format_countdown,
    format_delta_tokens,
    format_detail_text_report,
    format_media_size,
    format_tokens,
    truncate_display_width,
)

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"
_READOUT_WIDTH_PROBE = (
    "选定步 #9999 (11-21 01:50:10) │ [Hydrogen XH] │ 请求体 512.0K (+99.9K) "
    "│ 缓存 100.0% [压缩重热] │ 首字 123.4s │ 999 t/s"
)


def _model_band_color(model_name: str, theme: Dict[str, str]) -> str:
    low = (model_name or "").lower()
    if "neon" in low:
        return theme["cyan"]
    if "carbon" in low:
        return "#F59E0B"
    if "argon" in low:
        return theme["pink_accent"]
    if "hydrogen" in low:
        return theme["green"]
    if any(k in low for k in ("opus", "sonnet", "fable", "claude", "gpt")):
        return "#FB923C"
    return theme["text_muted"]


def _short_model_tag(model_name: str) -> str:
    m = (model_name or "").strip()
    if not m:
        return "?"
    for full, abbr in (
        ("Neon XHigh", "Neon XH"),
        ("Neon High", "Neon H"),
        ("Carbon High", "Carbon H"),
        ("Argon High", "Argon H"),
        ("Hydrogen XH", "Hydro XH"),
        ("Hydrogen H", "Hydro H"),
    ):
        if m == full:
            return abbr
    return m[:10]


def ttft_metric_color(sec: Optional[float], theme: Dict[str, str]) -> str:
    """Unified 4-tier TTFT color scale: green <15s, cyan 15-45s, orange 45-90s, red >=90s."""
    if sec is None:
        return theme["text_muted"]
    if sec >= 90.0:
        return theme["red"]
    if sec >= 45.0:
        return theme["orange"]
    if sec >= 15.0:
        return theme["cyan"]
    return theme["green"]


def summarize_detail_points(pts: List[ConversationDetailPoint]) -> Dict[str, Any]:
    """Compute window-scoped KPI aggregates matching the active chart range ('60' vs 'all')."""
    if not pts:
        return {
            "peak_context_tokens": 0,
            "compact_count": 0,
            "revert_count": 0,
            "cache_hit_steps": 0,
            "abnormal_miss_steps": 0,
            "expected_cold_steps": 0,
            "cacheable_hit_ratio": None,
            "avg_ttft_sec": None,
            "p95_ttft_sec": None,
            "max_ttft_sec": None,
            "avg_tps": None,
            "latest_tps": None,
        }
    peak_ctx = max(p.total_tokens for p in pts)
    revert_count = sum(1 for p in pts if getattr(p, "is_revert", False) or p.reason == "revert_reset")
    compact_count = sum(
        1
        for p in pts
        if (not getattr(p, "is_revert", False) and p.reason != "revert_reset")
        and (
            getattr(p, "is_compact", False)
            or p.reason == "compact_reset"
            or (p.context_delta <= -32000 and p.total_tokens < (p.total_tokens - p.context_delta) * 0.75)
        )
    )
    hit_steps = sum(1 for p in pts if p.classification == "hit")
    abn_steps = sum(1 for p in pts if p.classification == "abnormal_miss")
    exp_cold_steps = sum(1 for p in pts if p.classification == "expected_cold")
    cacheable_total = hit_steps + abn_steps
    hit_ratio = (hit_steps / cacheable_total) if cacheable_total > 0 else None

    ttfts = sorted(p.ttft_sec for p in pts if p.ttft_sec is not None)
    avg_ttft = (sum(ttfts) / len(ttfts)) if ttfts else None
    max_ttft = ttfts[-1] if ttfts else None
    p95_ttft = ttfts[min(len(ttfts) - 1, int(round(0.95 * (len(ttfts) - 1))))] if ttfts else None

    weighted_out = 0
    weighted_strm = 0.0
    for p in pts:
        if p.streaming_sec is not None and p.streaming_sec >= 0.5 and p.output_tokens > 0:
            weighted_out += int(p.output_tokens)
            weighted_strm += float(p.streaming_sec)
    if weighted_strm > 0.0 and weighted_out > 0:
        avg_tps: Optional[float] = weighted_out / weighted_strm
    else:
        tpss = [p.tps for p in pts if p.tps is not None]
        avg_tps = (sum(tpss) / len(tpss)) if tpss else None
    latest_tps = next((p.tps for p in reversed(pts) if p.tps is not None), None)
    return {
        "peak_context_tokens": peak_ctx,
        "compact_count": compact_count,
        "revert_count": revert_count,
        "cache_hit_steps": hit_steps,
        "abnormal_miss_steps": abn_steps,
        "expected_cold_steps": exp_cold_steps,
        "cacheable_hit_ratio": hit_ratio,
        "avg_ttft_sec": avg_ttft,
        "p95_ttft_sec": p95_ttft,
        "max_ttft_sec": max_ttft,
        "avg_tps": avg_tps,
        "latest_tps": latest_tps,
    }


class ConversationDetailWindow:
    """Borderless themed Toplevel displaying full-history Context, TTFT, and Cache charts."""

    def __init__(
        self,
        parent: tk.Tk,
        theme: Dict[str, str],
        load_report: Callable[[str], Optional[ConversationDetailReport]],
        on_toggle_pin: Callable[[str], None],
        is_pinned_fn: Callable[[str], bool],
        on_closed: Optional[Callable[[], None]] = None,
        apply_rounding_fn: Optional[Callable[[int], None]] = None,
        screen_bounds_fn: Optional[Callable[[tk.Tk], Tuple[int, int, int, int]]] = None,
        slow_tps_threshold: float = 35.0,
    ) -> None:
        self._parent = parent
        self.theme: Dict[str, str] = theme
        self._load_report = load_report
        self._on_toggle_pin = on_toggle_pin
        self._is_pinned_fn = is_pinned_fn
        self._on_closed = on_closed
        self._apply_rounding_fn = apply_rounding_fn
        self._screen_bounds_fn = screen_bounds_fn
        self._slow_tps_threshold: float = max(1.0, float(slow_tps_threshold))

        self._conv_id: str = ""
        self._report: Optional[ConversationDetailReport] = None
        self._current_wstats: Dict[str, Any] = {}
        self._window_mode: str = "60"  # "60" | "all"
        self._display_points: List[ConversationDetailPoint] = []
        self._chart_meta: Dict[str, Dict[str, Any]] = {}
        self._last_sync_sig: Optional[Tuple[Any, ...]] = None
        self._is_historical_badge: bool = False
        self._hovering_chart: bool = False
        self._locked_size: Optional[Tuple[int, int]] = None
        self._last_canvas_sizes: Dict[str, Tuple[int, int]] = {}
        self._drag_ox: int = 0
        self._drag_oy: int = 0
        self._win_ox: int = 0
        self._win_oy: int = 0
        self._closed: bool = False
        self._after_ids: List[str] = []

        self.win = tk.Toplevel(parent)
        self.win.title("单对话长线体检")
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.97)
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._outer = tk.Frame(self.win, padx=1, pady=1)
        self._outer.pack(fill="both", expand=True)
        self._surface = tk.Frame(self._outer, padx=12, pady=10)
        self._surface.pack(fill="both", expand=True)

        self._build_ui()
        self.apply_theme(theme)
        self._lock_window_size()

    def _build_ui(self) -> None:
        # Header bar (draggable): pack right-side controls first so long titles can never push them out
        self._hdr = tk.Frame(self._surface)
        self._hdr.pack(fill="x", pady=(0, 8))

        self._btn_close = tk.Label(self._hdr, text="✕", font=(UI_FONT, 9, "bold"), padx=6, pady=1, cursor="hand2")
        self._btn_close.pack(side="right", padx=(6, 0))
        self._btn_close.bind("<Button-1>", lambda _e: self.close())

        self._range_bar = tk.Frame(self._hdr)
        self._range_bar.pack(side="right")
        self._lbl_live_tag = tk.Label(self._range_bar, text="● 实时", font=(UI_FONT, 7, "bold"), padx=5, pady=2)
        self._lbl_live_tag.pack(side="left", padx=(0, 6))
        self._range_btns: Dict[str, tk.Label] = {}
        for mode, label in (("60", "近 60 步"), ("all", "全部步数")):
            b = tk.Label(self._range_bar, text=label, font=(UI_FONT, 8), padx=7, pady=2, cursor="hand2")
            b.pack(side="left", padx=(4, 0))
            b.bind("<Button-1>", lambda _e, m=mode: self._set_window_mode(m))
            self._range_btns[mode] = b

        self._lbl_role = tk.Label(self._hdr, text="主对话", font=(UI_FONT, 8, "bold"), padx=6, pady=1)
        self._lbl_role.pack(side="left", padx=(0, 6))
        self._lbl_title = tk.Label(self._hdr, text="—", font=(UI_FONT, 10, "bold"), anchor="w")
        self._lbl_title.pack(side="left")
        self._lbl_sub = tk.Label(self._hdr, text="", font=(MONO_FONT, 8), anchor="w")
        self._lbl_sub.pack(side="left", padx=(8, 0))

        for w in (self._hdr, self._lbl_role, self._lbl_title, self._lbl_sub):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)

        # 4-column KPI cards
        self._kpi_row = tk.Frame(self._surface)
        self._kpi_row.pack(fill="x", pady=(0, 8))
        self._kpi_cards: List[Dict[str, tk.Widget]] = []
        kpi_titles = ("请求体规模 / 变化", "全程缓存有效命中", "首字延迟 TTFT", "生成吞吐 / 保鲜")
        for i, title in enumerate(kpi_titles):
            self._kpi_row.grid_columnconfigure(i, weight=1, uniform="kpi")
            card = tk.Frame(self._kpi_row, padx=8, pady=6, highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0 if i == 3 else 4))
            lt = tk.Label(card, text=title, font=(UI_FONT, 8), anchor="w")
            lt.pack(fill="x")
            lv = tk.Label(card, text="—", font=(MONO_FONT, 11, "bold"), anchor="w")
            lv.pack(fill="x", pady=(2, 1))
            ls = tk.Label(card, text="—", font=(MONO_FONT, 8), anchor="w")
            ls.pack(fill="x")
            self._kpi_cards.append({"card": card, "title": lt, "val": lv, "sub": ls})

        # 3 long-range Canvas chart cards
        self._chart_specs = [
            ("ctx", "请求体 / 上下文增长曲线", "顶带=每步模型 · 青线=上下文 · 紫虚=压缩 · 橙虚=回溯 · 蓝虚=换号"),
            ("ttft", "首字延迟 TTFT 与模型切换趋势", "绿<15s · 青15-45s · 橙45-90s · 红≥90s"),
            ("cache", "缓存命中率长线轨迹", "绿命中 · 紫压缩重热 · 橙正常冷启/回溯 · 红异常掉缓存 · 蓝虚=换号"),
        ]
        self._charts: Dict[str, Dict[str, Any]] = {}
        chart_heights = {"ctx": 118, "ttft": 112, "cache": 82}
        for key, title, legend in self._chart_specs:
            frame = tk.Frame(self._surface, padx=8, pady=6, highlightthickness=1)
            frame.pack(fill="x", pady=(0, 7))
            top = tk.Frame(frame)
            top.pack(fill="x", pady=(0, 3))
            lt = tk.Label(top, text=title, font=(UI_FONT, 8, "bold"), anchor="w")
            lt.pack(side="left")
            ll = tk.Label(top, text=legend, font=(UI_FONT, 8), anchor="e")
            ll.pack(side="right")
            cv = tk.Canvas(frame, width=680, height=chart_heights[key], highlightthickness=0, bd=0, cursor="crosshair")
            cv.pack(fill="x")
            cv.bind("<Motion>", lambda e, k=key: self._on_chart_hover(k, e))
            cv.bind("<Leave>", lambda _e: self._clear_chart_crosshair())
            cv.bind("<Configure>", lambda _e, k=key: self._on_chart_configure(k))
            self._charts[key] = {"frame": frame, "top": top, "title": lt, "legend": ll, "canvas": cv}

        # Live crosshair readout strip (measure with max-width probe first so locked width fits longest row)
        self._readout_box = tk.Frame(self._surface, padx=8, pady=5, highlightthickness=1)
        self._readout_box.pack(fill="x", pady=(0, 8))
        self._lbl_readout = tk.Label(
            self._readout_box,
            text=_READOUT_WIDTH_PROBE,
            font=(MONO_FONT, 8),
            anchor="w",
        )
        self._lbl_readout.pack(fill="both", expand=True)

        # Bottom action buttons
        self._act_row = tk.Frame(self._surface)
        self._act_row.pack(fill="x")
        self._act_btns: Dict[str, tk.Label] = {}
        actions = [
            ("pin", "📌 聚焦到主面板", self._handle_toggle_pin),
            ("copy", "复制长线详报", self._copy_report),
            ("refresh", "刷新数据", self.refresh),
            ("close", "关闭窗口", self.close),
        ]
        for idx, (k, txt, cmd) in enumerate(actions):
            self._act_row.grid_columnconfigure(idx, weight=1, uniform="dact")
            btn = tk.Label(self._act_row, text=txt, font=(UI_FONT, 8), padx=8, pady=5, cursor="hand2", highlightthickness=1)
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 4, 0 if idx == 3 else 4))
            btn.bind("<Button-1>", lambda _e, fn=cmd: fn())
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=self.theme["btn_hover"]))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=self.theme["btn_bg"]))
            self._act_btns[k] = btn

    def apply_theme(self, theme: Dict[str, str]) -> None:
        self.theme = theme
        t = theme
        self.win.configure(bg=t["border"])
        self._outer.configure(bg=t["border"])
        self._surface.configure(bg=t["panel_bg"])
        self._hdr.configure(bg=t["panel_bg"])
        self._lbl_title.configure(bg=t["panel_bg"], fg=t["text_primary"])
        self._lbl_sub.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._btn_close.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._range_bar.configure(bg=t["panel_bg"])
        self._lbl_live_tag.configure(
            bg=t["btn_hover"] if self._is_historical_badge else t["green_bg"],
            fg=t["cyan"] if self._is_historical_badge else t["green"],
        )
        self._kpi_row.configure(bg=t["panel_bg"])
        for kc in self._kpi_cards:
            kc["card"].configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
            kc["title"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            kc["val"].configure(bg=t["card_bg"], fg=t["text_primary"])
            kc["sub"].configure(bg=t["card_bg"], fg=t["text_muted"])
        for ch in self._charts.values():
            ch["frame"].configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
            ch["top"].configure(bg=t["card_bg"])
            ch["title"].configure(bg=t["card_bg"], fg=t["text_primary"])
            ch["legend"].configure(bg=t["card_bg"], fg=t["text_muted"])
            ch["canvas"].configure(bg=t["card_alt_bg"])
        self._readout_box.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._lbl_readout.configure(bg=t["card_alt_bg"], fg=t["text_secondary"])
        self._act_row.configure(bg=t["panel_bg"])
        for btn in self._act_btns.values():
            btn.configure(bg=t["btn_bg"], fg=t["btn_text"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._refresh_range_buttons()
        if self._report is not None:
            self._render_report()

    def _lock_window_size(self) -> Tuple[int, int]:
        """Measure DPI-aware baseline dimensions once and permanently lock geometry to prevent width jitter."""
        if self._locked_size is not None:
            return self._locked_size
        self.win.update_idletasks()
        hdr_h = max(24, int(self._hdr.winfo_reqheight()))
        readout_h = max(26, int(self._readout_box.winfo_reqheight()))
        req_w = int(self.win.winfo_reqwidth())
        req_h = int(self.win.winfo_reqheight())

        if self._screen_bounds_fn is not None:
            vx, vy, vr, vb = self._screen_bounds_fn(self._parent)
            avail_w = max(640, (vr - vx) - 40)
            avail_h = max(480, (vb - vy) - 64)
        else:
            avail_w = max(640, self._parent.winfo_screenwidth() - 40)
            avail_h = max(480, self._parent.winfo_screenheight() - 64)

        locked_w = min(avail_w, max(728, req_w + 16))
        locked_h = min(avail_h, max(540, req_h))

        # Freeze inner containers that carry variable-length text so label updates never resize the window
        self._hdr.configure(height=hdr_h)
        self._hdr.pack_propagate(False)
        self._readout_box.configure(height=readout_h)
        self._readout_box.pack_propagate(False)
        self._outer.pack_propagate(False)

        self._locked_size = (locked_w, locked_h)
        self.win.minsize(locked_w, locked_h)
        self.win.maxsize(locked_w, locked_h)
        self.win.geometry(f"{locked_w}x{locked_h}")
        self._lbl_readout.configure(
            text="移动鼠标到任意图表上可精准查看对应步号的上下文变化、TTFT、吞吐与缓存原因"
        )
        self.win.update_idletasks()
        return self._locked_size

    def _set_live_badge(self, is_historical: bool) -> None:
        self._is_historical_badge = bool(is_historical)
        t = self.theme
        if self._is_historical_badge:
            self._lbl_live_tag.configure(text="● 历史回看", bg=t["btn_hover"], fg=t["cyan"])
        else:
            self._lbl_live_tag.configure(text="● 实时", bg=t["green_bg"], fg=t["green"])

    @staticmethod
    def _build_sync_sig(conv: Any, _snap: MonitorSnapshot) -> Tuple[Any, ...]:
        # Track ONLY view-independent full-conversation identity + latest TTFT/TPS so switching
        # left-panel account/range filters never causes redundant reloads, yet delayed SQLite WAL
        # TTFT flushes still update the open detail window.
        return (
            conv.conversation_id,
            getattr(conv, "full_step_count", conv.total_step_count),
            round(getattr(conv, "full_latest_timestamp", conv.latest_timestamp), 2),
            round(conv.ttft_sec, 2) if conv.ttft_sec is not None else None,
            round(conv.tps, 1) if conv.tps is not None else None,
            conv.title,
        )

    def show_conversation(
        self,
        conv_id: str,
        place_near_parent: bool = True,
        initial_snap: Optional[MonitorSnapshot] = None,
    ) -> None:
        if self._closed:
            return
        self._conv_id = conv_id
        self._last_sync_sig = None
        self._hovering_chart = False
        self._set_live_badge(False)
        self._lock_window_size()
        self.win.deiconify()
        self.win.lift()
        self.win.update_idletasks()
        self.refresh()
        if initial_snap is not None:
            conv = next((c for c in initial_snap.active_conversations if c.conversation_id == conv_id), None)
            if conv is not None:
                self._last_sync_sig = self._build_sync_sig(conv, initial_snap)
        self.win.update_idletasks()
        if place_near_parent:
            self._place_window()
        else:
            w, h = self._lock_window_size()
            self.win.geometry(f"{w}x{h}+{self.win.winfo_x()}+{self.win.winfo_y()}")
        if self._apply_rounding_fn is not None:
            try:
                self._apply_rounding_fn(int(self.win.winfo_id()))
            except Exception:
                pass

    def _tick_own_freshness_kpi(self, now_ts: float) -> None:
        """Keep the detail window's own freshness countdown ticking even if left panel switches to another account."""
        rep = self._report
        if rep is None or not rep.points:
            return
        latest_pt = rep.points[-1]
        latest_tps = self._current_wstats.get("latest_tps")
        if latest_tps is None:
            latest_tps = rep.latest_tps
        if rep.freshness_invalid_reason:
            self._update_freshness_kpi(None, rep.freshness_invalid_reason, latest_tps)
            return
        if rep.freshness_remaining_sec is not None and latest_pt.timestamp > 0:
            rem = int(round(rep.freshness_window_sec - (now_ts - latest_pt.timestamp)))
            self._update_freshness_kpi(max(0, rem), "", latest_tps)

    def sync_from_snapshot(self, snap: MonitorSnapshot) -> None:
        """Keep the open detail modal dynamically in sync with the main 2.5s poll loop."""
        if self._closed or not self._conv_id:
            return
        conv = next((c for c in snap.active_conversations if c.conversation_id == self._conv_id), None)
        if conv is None:
            # Left panel may be filtered to another account; still keep this conversation's own freshness ticking!
            self._tick_own_freshness_kpi(snap.generated_at)
            self._sync_pin_button()
            return
        sig = self._build_sync_sig(conv, snap)
        if sig != self._last_sync_sig:
            self._last_sync_sig = sig
            self.refresh()
        else:
            # Zero-IO fast path: smoothly tick the freshness countdown without redrawing canvases
            latest_tps = self._current_wstats.get("latest_tps")
            if latest_tps is None:
                latest_tps = self._report.latest_tps if self._report is not None else conv.tps
            if not (snap.is_custom_account_view or snap.history_range != "30m"):
                self._update_freshness_kpi(conv.freshness_remaining_sec, conv.freshness_invalid_reason, latest_tps)
            else:
                self._tick_own_freshness_kpi(snap.generated_at)
            self._sync_pin_button()

    def _on_chart_configure(self, chart_key: str) -> None:
        if self._closed or not self._display_points:
            return
        cv = self._charts[chart_key]["canvas"]
        cur_sz = (int(cv.winfo_width()), int(cv.winfo_height()))
        if cur_sz[0] <= 20 or cur_sz[1] <= 20:
            return
        if self._last_canvas_sizes.get(chart_key) == cur_sz:
            return
        if chart_key == "ctx":
            self._draw_context_chart(self._display_points)
        elif chart_key == "ttft":
            self._draw_ttft_chart(self._display_points)
        elif chart_key == "cache":
            self._draw_cache_chart(self._display_points)

    def _place_window(self) -> None:
        w, h = self._lock_window_size()
        if self._screen_bounds_fn is not None:
            vx, vy, vr, vb = self._screen_bounds_fn(self._parent)
        else:
            vx, vy, vr, vb = 0, 0, self._parent.winfo_screenwidth(), self._parent.winfo_screenheight()
        px = self._parent.winfo_x()
        py = self._parent.winfo_y()
        pw = self._parent.winfo_width()
        # Try placing cleanly to the left or right of the main dashboard
        if px - w - 12 >= vx + 8:
            x = px - w - 12
        elif px + pw + 12 + w <= vr - 8:
            x = px + pw + 12
        else:
            x = max(vx + 12, min(vr - w - 12, (vx + vr - w) // 2))
        y = max(vy + 12, min(vb - h - 48, py))
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_ox = event.x_root
        self._drag_oy = event.y_root
        self._win_ox = self.win.winfo_x()
        self._win_oy = self.win.winfo_y()

    def _on_drag_move(self, event: tk.Event) -> None:
        nx = self._win_ox + (event.x_root - self._drag_ox)
        ny = self._win_oy + (event.y_root - self._drag_oy)
        w, h = self._lock_window_size()
        self.win.geometry(f"{w}x{h}+{nx}+{ny}")

    def _set_window_mode(self, mode: str) -> None:
        if mode not in ("60", "all"):
            return
        self._window_mode = mode
        self._refresh_range_buttons()
        if self._report is not None:
            self._render_report()

    def _refresh_range_buttons(self) -> None:
        t = self.theme
        for mode, btn in self._range_btns.items():
            active = mode == self._window_mode
            btn.configure(
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )

    def _sync_pin_button(self) -> None:
        if not self._conv_id:
            return
        pinned = self._is_pinned_fn(self._conv_id)
        if pinned:
            self._act_btns["pin"].configure(text="📌 已聚焦 (点击取消)", fg=self.theme["cyan"])
        else:
            self._act_btns["pin"].configure(text="📌 聚焦到主面板", fg=self.theme["btn_text"])

    def _handle_toggle_pin(self) -> None:
        if not self._conv_id:
            return
        self._on_toggle_pin(self._conv_id)
        self._sync_pin_button()

    def refresh(self) -> None:
        if self._closed or not self._conv_id:
            return
        self._report = self._load_report(self._conv_id)
        self._render_report()

    def _copy_report(self) -> None:
        if self._report is None:
            return
        txt = format_detail_text_report(self._report)
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(txt)
            self.win.update()
        except tk.TclError:
            return
        btn = self._act_btns["copy"]
        btn.configure(text="已复制详报 ✓", fg=self.theme["green"])
        aid = self.win.after(
            1500,
            lambda: btn.configure(text="复制长线详报", fg=self.theme["btn_text"]) if not self._closed else None,
        )
        self._after_ids.append(aid)

    def _render_report(self) -> None:
        t = self.theme
        rep = self._report
        self._sync_pin_button()
        if rep is None:
            self._display_points = []
            self._current_wstats = {}
            self._lbl_title.configure(text=f"{self._conv_id[:6]} · 未找到轨迹文件")
            self._lbl_sub.configure(text="")
            for kc in self._kpi_cards:
                kc["val"].configure(text="—", fg=t["text_muted"])
                kc["sub"].configure(text="—", fg=t["text_muted"])
            self._lbl_readout.configure(text="未找到该对话的轨迹文件或当前视角暂无可展示数据", fg=t["text_muted"])
            for ch in self._charts.values():
                ch["canvas"].delete("all")
            return

        if rep.is_subagent:
            self._lbl_role.configure(text="子代理", bg=t["card_alt_bg"], fg=t["text_secondary"])
        else:
            self._lbl_role.configure(text="主对话", bg=t["green_bg"], fg=t["green"])
        distinct_models = list(dict.fromkeys(p.model for p in rep.points if p.model))
        model_tag = f"{rep.model} (含{len(distinct_models)}模)" if len(distinct_models) >= 2 else rep.model
        scope_tag = f" · {rep.scope_label}" if getattr(rep, "scope_label", "") else ""
        if getattr(rep, "scope_label", ""):
            self._set_live_badge(True)
        if rep.title:
            disp_name = truncate_display_width(rep.title, 20)
            self._lbl_title.configure(text=disp_name)
            self._lbl_sub.configure(text=f"{rep.short_id} · {model_tag} · {rep.total_steps}步(今{rep.today_steps}){scope_tag}")
            self.win.title(f"{rep.title} ({rep.short_id}) - 长线体检")
        else:
            self._lbl_title.configure(text=f"{rep.short_id} · {model_tag}")
            self._lbl_sub.configure(text=f"总计 {rep.total_steps} 步 (今日 {rep.today_steps} 步){scope_tag}")
            self.win.title(f"{rep.short_id} - 长线体检")

        pts = rep.points[-60:] if (self._window_mode == "60" and len(rep.points) > 60) else list(rep.points)
        self._display_points = pts

        # Update dynamic model legend on TTFT chart header
        vis_models = list(dict.fromkeys(p.model for p in pts if p.model))
        if vis_models:
            tags = [_short_model_tag(m) for m in vis_models[:3]]
            if len(vis_models) > 3:
                tags.append(f"+{len(vis_models) - 3}")
            self._charts["ttft"]["legend"].configure(
                text="顶带模型: " + " / ".join(tags) + " │ 绿<15s 青<45s 橙<90s 红≥90s"
            )
        else:
            self._charts["ttft"]["legend"].configure(text="绿<15s · 青15-45s · 橙45-90s · 红≥90s")

        # Recompute all 4 KPI cards strictly for the active window ('60' vs 'all')
        wstats = summarize_detail_points(pts)
        self._current_wstats = wstats
        large_ctx_allow = compute_large_ctx_ttft_allowance(rep.latest_context_tokens, rep.max_context_tokens)
        cal_title_tag = f" (+{large_ctx_allow:.0f}s校)" if large_ctx_allow >= 10.0 else ""
        is_sliced_60 = self._window_mode == "60" and len(rep.points) > 60
        if is_sliced_60:
            self._kpi_cards[0]["title"].configure(text="请求体规模 (近60步)")
            self._kpi_cards[1]["title"].configure(text="近 60 步缓存命中")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT (近60步){cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜 (近60步)")
        elif self._window_mode == "60":
            self._kpi_cards[0]["title"].configure(text="请求体规模 / 变化")
            self._kpi_cards[1]["title"].configure(text=f"近 60 步命中 (全{len(rep.points)}步)")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT{cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜")
        else:
            self._kpi_cards[0]["title"].configure(text="请求体规模 / 变化")
            self._kpi_cards[1]["title"].configure(text="全程缓存有效命中")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT (全程){cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜 (全程)")

        # KPI 1: Context size & delta (window-scoped peak, compaction count & revert count)
        delta_s = format_delta_tokens(rep.latest_context_delta)
        latest_is_revert = bool(pts) and (getattr(pts[-1], "is_revert", False) or pts[-1].reason == "revert_reset")
        latest_is_compact = (not latest_is_revert) and bool(pts) and (
            getattr(pts[-1], "is_compact", False) or pts[-1].reason == "compact_reset"
        )
        delta_col = t["orange"] if latest_is_revert else (t["purple"] if latest_is_compact else t["cyan"])
        self._kpi_cards[0]["val"].configure(text=f"{format_tokens(rep.latest_context_tokens)} ({delta_s})", fg=delta_col)
        rev_part = f" · 回溯 {wstats['revert_count']}" if wstats["revert_count"] > 0 else ""
        self._kpi_cards[0]["sub"].configure(
            text=f"峰值 {format_tokens(wstats['peak_context_tokens'])} · 压缩 {wstats['compact_count']}{rev_part}",
            fg=t["text_secondary"],
        )

        # KPI 2: Cache hit ratio (window-scoped; sub-row escalates orange for isolated miss, red for severe)
        hit_ratio = wstats["cacheable_hit_ratio"]
        abn = wstats["abnormal_miss_steps"]
        if hit_ratio is not None:
            pct = hit_ratio * 100.0
            c_col = t["green"] if pct >= 85 else (t["orange"] if pct >= 55 else t["red"])
            self._kpi_cards[1]["val"].configure(text=f"{pct:.1f}%", fg=c_col)
        else:
            self._kpi_cards[1]["val"].configure(text="—", fg=t["text_muted"])
        sub_col = (
            t["red"]
            if (abn >= 4 or (hit_ratio is not None and hit_ratio < 0.85 and abn > 0))
            else (t["orange"] if abn > 0 else t["text_secondary"])
        )
        self._kpi_cards[1]["sub"].configure(
            text=f"命中 {wstats['cache_hit_steps']} · 异常 {abn} · 冷 {wstats['expected_cold_steps']}",
            fg=sub_col,
        )

        # KPI 3: TTFT (mean colored strictly by mean on unified 4-tier scale; tail P95/max colored separately)
        avg_ttft = wstats["avg_ttft_sec"]
        p95_ttft = wstats["p95_ttft_sec"]
        max_ttft = wstats["max_ttft_sec"]
        if avg_ttft is not None and max_ttft is not None:
            t_col = ttft_metric_color(avg_ttft, t)
            self._kpi_cards[2]["val"].configure(text=f"均 {avg_ttft:.1f}s", fg=t_col)
            p95_s = f"{p95_ttft:.1f}s" if p95_ttft is not None else "—"
            tail_col = (
                t["red"]
                if (p95_ttft is not None and p95_ttft >= 90.0)
                else (
                    t["orange"]
                    if (max_ttft >= 90.0 or (p95_ttft is not None and p95_ttft >= 45.0))
                    else t["text_secondary"]
                )
            )
            self._kpi_cards[2]["sub"].configure(text=f"P95 {p95_s} · 峰 {max_ttft:.1f}s", fg=tail_col)
        else:
            self._kpi_cards[2]["val"].configure(text="—", fg=t["text_muted"])
            self._kpi_cards[2]["sub"].configure(text="暂无 SQLite 速度记录", fg=t["text_muted"])

        # KPI 4: TPS & Freshness (mean TPS window-scoped; orange alert uses configured slow threshold)
        avg_tps = wstats["avg_tps"]
        if avg_tps is not None:
            tps_col = t["orange"] if avg_tps < self._slow_tps_threshold else t["cyan"]
            self._kpi_cards[3]["val"].configure(text=f"均 {avg_tps:.0f} t/s", fg=tps_col)
        else:
            self._kpi_cards[3]["val"].configure(text="— t/s", fg=t["text_muted"])
        if self._is_historical_badge:
            self._update_freshness_kpi(None, "", wstats["latest_tps"])
        else:
            self._update_freshness_kpi(rep.freshness_remaining_sec, rep.freshness_invalid_reason, wstats["latest_tps"])

        self.win.update_idletasks()
        self._draw_context_chart(pts)
        self._draw_ttft_chart(pts)
        self._draw_cache_chart(pts)

        if pts and not self._hovering_chart:
            self._update_readout_for_point(pts[-1], prefix="最新步")

    def _update_freshness_kpi(
        self,
        freshness_remaining_sec: Optional[int],
        freshness_invalid_reason: str,
        latest_tps: Optional[float],
    ) -> None:
        t = self.theme
        if freshness_invalid_reason:
            is_severe = "连掉" in freshness_invalid_reason
            f_txt, f_col = freshness_invalid_reason, (t["red"] if is_severe else t["orange"])
        elif freshness_remaining_sec is not None:
            if freshness_remaining_sec <= 0:
                f_txt, f_col = "大上下文保鲜: 已过期", t["orange"]
            else:
                f_txt = f"保鲜剩 {format_countdown(freshness_remaining_sec)}"
                f_col = t["green"] if freshness_remaining_sec > 60 else t["orange"]
        else:
            latest_p = f"新 {latest_tps:.0f} t/s" if latest_tps is not None else "常规上下文"
            f_txt, f_col = latest_p, t["text_secondary"]
        self._kpi_cards[3]["sub"].configure(text=f_txt, fg=f_col)

    def _draw_model_band(
        self,
        cv: tk.Canvas,
        pts: List[ConversationDetailPoint],
        pad_l: int,
        plot_w: int,
        y0: float,
        y1: float,
        draw_switch_lines: bool = False,
        plot_top: float = 0.0,
        plot_bot: float = 0.0,
    ) -> None:
        """Draw a per-step colored model strip along the top edge and optional model-switch markers."""
        n = len(pts)
        if n == 0:
            return
        t = self.theme
        if n == 1:
            col = _model_band_color(pts[0].model, t)
            cv.create_rectangle(pad_l, y0, pad_l + plot_w, y1, fill=col, outline="")
            return
        step_w = plot_w / float(n - 1)
        last_lbl_x = -999.0
        # Merge contiguous runs of the same model for crisp rendering
        run_start = 0
        for idx in range(1, n + 1):
            is_end = idx == n
            changed = (not is_end) and (pts[idx].model != pts[run_start].model)
            if is_end or changed:
                x_left = pad_l if run_start == 0 else (self._x_for_idx(run_start, n, pad_l, plot_w) - step_w * 0.5)
                x_right = (pad_l + plot_w) if is_end else (self._x_for_idx(idx, n, pad_l, plot_w) - step_w * 0.5)
                col = _model_band_color(pts[run_start].model, t)
                cv.create_rectangle(max(pad_l, x_left), y0, min(pad_l + plot_w, x_right), y1, fill=col, outline="")
                if changed and draw_switch_lines:
                    sx = self._x_for_idx(idx, n, pad_l, plot_w)
                    new_col = _model_band_color(pts[idx].model, t)
                    cv.create_line(sx, plot_top, sx, plot_bot, fill=new_col, width=1.1, dash=(2, 3))
                    tag_txt = f"→{_short_model_tag(pts[idx].model)}"
                    if sx < pad_l + plot_w - 42.0:
                        if sx - last_lbl_x >= 54.0:
                            cv.create_text(
                                sx + 2,
                                y1 + 6,
                                text=tag_txt,
                                anchor="w",
                                font=(MONO_FONT, 7, "bold"),
                                fill=new_col,
                            )
                            last_lbl_x = sx
                    elif sx - last_lbl_x >= 56.0:
                        cv.create_text(
                            sx - 2,
                            y1 + 6,
                            text=tag_txt,
                            anchor="e",
                            font=(MONO_FONT, 7, "bold"),
                            fill=new_col,
                        )
                        last_lbl_x = sx
                run_start = idx

    def _chart_geometry(self, cv: tk.Canvas) -> Tuple[int, int, int, int, int, int]:
        w = max(360, int(cv.winfo_width() if cv.winfo_width() > 20 else cv["width"]))
        h = max(60, int(cv.winfo_height() if cv.winfo_height() > 20 else cv["height"]))
        pad_l, pad_r, pad_t, pad_b = 62, 14, 12, 20
        return w, h, pad_l, pad_r, pad_t, pad_b

    def _x_for_idx(self, idx: int, n: int, pad_l: int, plot_w: int) -> float:
        if n <= 1:
            return pad_l + plot_w * 0.5
        return pad_l + (idx / float(n - 1)) * plot_w

    def _draw_axis_footer(self, cv: tk.Canvas, pts: List[ConversationDetailPoint], pad_l: int, plot_w: int, h: int) -> None:
        t = self.theme
        if not pts:
            return
        y_lbl = h - 9
        first, last = pts[0], pts[-1]
        t0 = datetime.datetime.fromtimestamp(first.timestamp).strftime("%m-%d %H:%M") if first.timestamp > 0 else ""
        t1 = datetime.datetime.fromtimestamp(last.timestamp).strftime("%m-%d %H:%M") if last.timestamp > 0 else ""
        cv.create_text(pad_l, y_lbl, text=f"#{first.step_index} {t0}".strip(), anchor="w", font=(MONO_FONT, 7), fill=t["text_muted"])
        if len(pts) >= 3:
            mid = pts[len(pts) // 2]
            tm = datetime.datetime.fromtimestamp(mid.timestamp).strftime("%m-%d %H:%M") if mid.timestamp > 0 else ""
            cv.create_text(pad_l + plot_w / 2.0, y_lbl, text=f"#{mid.step_index} {tm}".strip(), anchor="center", font=(MONO_FONT, 7), fill=t["text_muted"])
        cv.create_text(pad_l + plot_w, y_lbl, text=f"#{last.step_index} {t1}".strip(), anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

    def _draw_context_chart(self, pts: List[ConversationDetailPoint]) -> None:
        t = self.theme
        cv = self._charts["ctx"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        self._last_canvas_sizes["ctx"] = (w, h)
        pad_t = 16
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["ctx"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        if not pts:
            cv.create_text(w / 2, h / 2, text="暂无上下文轨迹数据", font=(UI_FONT, 8), fill=t["text_muted"])
            return

        self._draw_model_band(cv, pts, pad_l, plot_w, y0=2, y1=6, draw_switch_lines=False)

        peak = max(p.total_tokens for p in pts)
        y_max = max(64_000, int(peak * 1.12))

        for frac in (0.0, 0.5, 1.0):
            yy = pad_t + plot_h * (1.0 - frac)
            cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=t["divider"], dash=(2, 3))
            cv.create_text(pad_l - 6, yy, text=format_tokens(int(y_max * frac)), anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        # Highlight compaction drops (purple), user Undo/revert branch points (orange), & account switches (cyan)
        n = len(pts)
        coords: List[float] = []
        last_lbl_x = -999.0
        for idx, pt in enumerate(pts):
            x = self._x_for_idx(idx, n, pad_l, plot_w)
            y = pad_t + plot_h * (1.0 - min(1.0, pt.total_tokens / float(y_max)))
            coords.extend([x, y])
            is_acc_sw = bool(getattr(pt, "is_account_switch", False) or pt.reason == "account_switch")
            mark_spec: Optional[Tuple[str, str, Tuple[int, int]]] = None
            if getattr(pt, "is_revert", False) or pt.reason == "revert_reset":
                mark_spec = ("回溯", t["orange"], (2, 2))
            elif getattr(pt, "is_compact", False) or pt.reason == "compact_reset":
                mark_spec = ("压缩", t["purple"], (3, 2))
            elif is_acc_sw:
                mark_spec = ("换号", t["cyan"], (4, 2))
            if mark_spec is not None:
                m_txt, m_col, m_dash = mark_spec
                cv.create_line(x, pad_t, x, pad_t + plot_h, fill=m_col, width=1.2, dash=m_dash)
                if x < pad_l + plot_w - 22.0:
                    if x - last_lbl_x >= 34.0:
                        cv.create_text(x + 2, pad_t + 7, text=m_txt, anchor="w", font=(UI_FONT, 7, "bold"), fill=m_col)
                        last_lbl_x = x
                elif x - last_lbl_x >= 34.0:
                    cv.create_text(x - 2, pad_t + 7, text=m_txt, anchor="e", font=(UI_FONT, 7, "bold"), fill=m_col)
                    last_lbl_x = x

        # Subtle shaded polygon + crisp cyan polyline
        if n >= 2:
            poly = [coords[0], pad_t + plot_h] + coords + [coords[-2], pad_t + plot_h]
            cv.create_polygon(poly, fill=t["bar_track"], outline="")
            cv.create_line(coords, fill=t["cyan"], width=2.0)
        elif n == 1:
            cv.create_oval(coords[0] - 3, coords[1] - 3, coords[0] + 3, coords[1] + 3, fill=t["cyan"], outline="")

        # Mark peak point
        peak_idx = max(range(n), key=lambda i: pts[i].total_tokens)
        px = coords[peak_idx * 2]
        py = coords[peak_idx * 2 + 1]
        cv.create_oval(px - 3, py - 3, px + 3, py + 3, fill=t["text_primary"], outline=t["cyan"], width=1.5)
        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _draw_ttft_chart(self, pts: List[ConversationDetailPoint]) -> None:
        t = self.theme
        cv = self._charts["ttft"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        self._last_canvas_sizes["ttft"] = (w, h)
        pad_t = 22
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["ttft"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        valid_vals = [p.ttft_sec for p in pts if p.ttft_sec is not None]
        if not valid_vals:
            cv.create_text(w / 2, h / 2, text="当前范围无 SQLite 首字延迟记录", font=(UI_FONT, 8), fill=t["text_muted"])
            return

        self._draw_model_band(
            cv,
            pts,
            pad_l,
            plot_w,
            y0=2,
            y1=7,
            draw_switch_lines=True,
            plot_top=float(pad_t),
            plot_bot=float(pad_t + plot_h),
        )

        sorted_vals = sorted(valid_vals)
        raw_max = sorted_vals[-1]
        p95_val = sorted_vals[min(len(sorted_vals) - 1, int(round(0.95 * (len(sorted_vals) - 1))))]
        # Soft-cap extreme outliers (e.g. a single 450s stall) so normal 5s-110s latency stays legible
        if raw_max > 180.0 and p95_val < raw_max * 0.65:
            y_max = max(120.0, p95_val * 1.75)
        else:
            y_max = max(55.0, raw_max * 1.12)

        drawn_label_ys: List[float] = []

        def _can_draw_y_lbl(yy: float) -> bool:
            if any(abs(yy - prev_y) < 11.5 for prev_y in drawn_label_ys):
                return False
            drawn_label_ys.append(yy)
            return True

        top_lbl = f"≥{int(round(y_max))}s" if raw_max > y_max else f"{int(round(y_max))}s"
        if _can_draw_y_lbl(float(pad_t)):
            cv.create_text(pad_l - 6, pad_t, text=top_lbl, anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        for sec, col, dash in ((0.0, t["divider"], (2, 3)), (45.0, t["orange"], (3, 3)), (90.0, t["red"], (3, 3))):
            if sec <= y_max:
                yy = pad_t + plot_h * (1.0 - sec / y_max)
                cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=col, dash=dash)
                if _can_draw_y_lbl(yy):
                    cv.create_text(pad_l - 6, yy, text=f"{int(sec)}s", anchor="e", font=(MONO_FONT, 7), fill=col if sec > 0 else t["text_muted"])

        n = len(pts)
        bar_w = max(2.0, min(8.0, (plot_w / float(max(1, n))) * 0.72))
        y_base = pad_t + plot_h
        for idx, pt in enumerate(pts):
            if pt.ttft_sec is None:
                continue
            x = self._x_for_idx(idx, n, pad_l, plot_w)
            sec = float(pt.ttft_sec)
            clipped = sec > y_max
            yy = pad_t if clipped else (pad_t + plot_h * (1.0 - sec / y_max))
            col = ttft_metric_color(sec, t)
            cv.create_rectangle(x - bar_w / 2.0, yy, x + bar_w / 2.0, y_base, fill=col, outline="")
            if clipped:
                cv.create_rectangle(x - bar_w / 2.0, pad_t, x + bar_w / 2.0, pad_t + 2.5, fill=t["text_primary"], outline="")

        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _draw_cache_chart(self, pts: List[ConversationDetailPoint]) -> None:
        t = self.theme
        cv = self._charts["cache"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        self._last_canvas_sizes["cache"] = (w, h)
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["cache"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        if not pts:
            cv.create_text(w / 2, h / 2, text="暂无缓存轨迹数据", font=(UI_FONT, 8), fill=t["text_muted"])
            return

        for pct in (0, 50, 100):
            yy = pad_t + plot_h * (1.0 - pct / 100.0)
            cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=t["divider"], dash=(2, 3))
            cv.create_text(pad_l - 6, yy, text=f"{pct}%", anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        n = len(pts)
        y_base = pad_t + plot_h
        # Draw in priority order when very dense so red misses & purple compactions stay crisp on top
        bar_w = max(2.0, min(9.0, (plot_w / float(max(1, n))) * 0.78))
        order = sorted(
            range(n),
            key=lambda i: 3 if pts[i].classification == "abnormal_miss" else (2 if pts[i].reason == "compact_reset" else (1 if pts[i].classification == "expected_cold" else 0)),
        )
        for idx in order:
            pt = pts[idx]
            x = self._x_for_idx(idx, n, pad_l, plot_w)
            if pt.classification == "hit":
                col = t["green"]
                ratio = max(0.2, min(1.0, pt.hit_rate / 100.0))
            elif pt.reason == "compact_reset":
                col = t["purple"]
                ratio = 0.74
            elif pt.classification == "expected_cold":
                col = t["orange"]
                ratio = 0.56
            else:
                col = t["red"]
                ratio = 1.0
            yy = y_base - plot_h * ratio
            cv.create_rectangle(x - bar_w / 2.0, yy, x + bar_w / 2.0, y_base, fill=col, outline="")

        last_sw_x = -999.0
        for idx, pt in enumerate(pts):
            if getattr(pt, "is_account_switch", False) or pt.reason == "account_switch":
                sx = self._x_for_idx(idx, n, pad_l, plot_w)
                cv.create_line(sx, pad_t, sx, y_base, fill=t["cyan"], width=1.1, dash=(3, 2))
                if sx < pad_l + plot_w - 22.0:
                    if sx - last_sw_x >= 34.0:
                        cv.create_text(sx + 2, pad_t + 6, text="换号", anchor="w", font=(UI_FONT, 7, "bold"), fill=t["cyan"])
                        last_sw_x = sx
                elif sx - last_sw_x >= 34.0:
                    cv.create_text(sx - 2, pad_t + 6, text="换号", anchor="e", font=(UI_FONT, 7, "bold"), fill=t["cyan"])
                    last_sw_x = sx

        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _on_chart_hover(self, chart_key: str, event: tk.Event) -> None:
        pts = self._display_points
        meta = self._chart_meta.get(chart_key)
        if not pts or not meta:
            return
        pad_l = float(meta["pad_l"])
        plot_w = float(meta["plot_w"])
        if event.x < pad_l - 6 or event.x > pad_l + plot_w + 6:
            self._clear_chart_crosshair()
            return
        self._hovering_chart = True
        frac = max(0.0, min(1.0, (event.x - pad_l) / max(1.0, plot_w)))
        idx = int(round(frac * (len(pts) - 1))) if len(pts) > 1 else 0
        idx = max(0, min(len(pts) - 1, idx))
        pt = pts[idx]

        # Synchronize vertical crosshair across all 3 charts
        for k, ch in self._charts.items():
            cv = ch["canvas"]
            cv.delete("crosshair")
            m = self._chart_meta.get(k)
            if not m:
                continue
            cx = self._x_for_idx(idx, len(pts), int(m["pad_l"]), int(m["plot_w"]))
            cv.create_line(
                cx,
                int(m["pad_t"]),
                cx,
                int(m["pad_t"]) + int(m["plot_h"]),
                fill=self.theme["text_secondary"],
                dash=(2, 2),
                tags="crosshair",
            )
        self._update_readout_for_point(pt)

    def _clear_chart_crosshair(self) -> None:
        self._hovering_chart = False
        for ch in self._charts.values():
            try:
                ch["canvas"].delete("crosshair")
            except tk.TclError:
                pass
        if self._display_points:
            self._update_readout_for_point(self._display_points[-1], prefix="最新步")

    def _update_readout_for_point(self, pt: ConversationDetailPoint, prefix: str = "选定步") -> None:
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%m-%d %H:%M:%S") if pt.timestamp > 0 else "-- --:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f} t/s" if pt.tps is not None else "—"
        if getattr(pt, "is_revert", False) and pt.reason == "cached":
            tag = "回溯·命中"
        elif getattr(pt, "is_compact", False) and pt.reason == "cached":
            tag = "压缩·命中"
        elif getattr(pt, "is_account_switch", False) and pt.reason == "cached":
            tag = "换号·命中"
        else:
            tag = CACHE_REASON_CN_MAP.get(pt.reason, pt.reason)
        mod_s = truncate_display_width(pt.model or "未知模型", 14)
        disp_prefix = "选定" if prefix == "选定步" else ("最新" if prefix == "最新步" else prefix)
        m_cnt = int(getattr(pt, "active_media_count", 0) or 0)
        m_bytes = int(getattr(pt, "active_media_bytes", 0) or 0)
        m_seg = f" │ 图{m_cnt}({format_media_size(m_bytes)})" if m_cnt > 0 else ""
        msg = (
            f"{disp_prefix}#{pt.step_index} {hm} │ [{mod_s}] │ {format_tokens(pt.total_tokens)} "
            f"({format_delta_tokens(pt.context_delta)}){m_seg} │ {pt.hit_rate:.1f}% [{tag}] │ 首字 {t_s} │ {p_s}"
        )
        t = self.theme
        fg = (
            t["red"]
            if pt.classification == "abnormal_miss"
            else (
                t["orange"]
                if (getattr(pt, "is_revert", False) or pt.reason == "revert_reset")
                else (
                    t["purple"]
                    if (getattr(pt, "is_compact", False) or pt.reason == "compact_reset")
                    else (
                        t["cyan"]
                        if (getattr(pt, "is_account_switch", False) or pt.reason == "account_switch")
                        else (t["orange"] if pt.classification == "expected_cold" else t["text_primary"])
                    )
                )
            )
        )
        self._lbl_readout.configure(text=msg, fg=fg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for aid in list(self._after_ids):
            try:
                self.win.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self._on_closed is not None:
            try:
                self._on_closed()
            except Exception:
                pass
