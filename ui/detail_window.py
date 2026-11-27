"""On-demand long-range single-conversation diagnostics modal (Context, TTFT, Cache)."""

from __future__ import annotations

import datetime
import math
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.log_monitor import (
    CACHE_REASON_CN_MAP,
    ConversationDetailPoint,
    ConversationDetailReport,
    MonitorSnapshot,
    compute_large_ctx_ttft_allowance,
    compute_weighted_avg_tps,
    format_countdown,
    format_delta_tokens,
    format_detail_text_report,
    format_media_size,
    format_tokens,
    is_severe_freshness_invalid,
    resolve_latest_same_family_tps,
    truncate_display_width,
)

try:
    from core.log_monitor import resolve_cache_visual_tier
except ImportError:
    def resolve_cache_visual_tier(
        classification: str,
        reason: str = "",
        *,
        is_compact: bool = False,
        is_revert: bool = False,
    ) -> str:
        """Return canonical visual color tier: 'red' | 'purple' | 'orange' | 'green'."""
        if classification == "abnormal_miss":
            return "red"
        if (is_compact or reason == "compact_reset") and not is_revert:
            return "purple"
        if is_revert or classification == "expected_cold":
            return "orange"
        return "green"

try:
    from core.log_monitor import FRESHNESS_RESCUE_WINDOW_SEC
except ImportError:
    FRESHNESS_RESCUE_WINDOW_SEC = 90

from ui.drag_proxy import LightweightProxyDragger, resolve_root_hwnd

_DATACLASS_SLOTS: Dict[str, Any] = {"slots": True} if sys.version_info >= (3, 10) else {}

_CACHE_REASON_CN_FALLBACK: Dict[str, str] = {
    "partial_miss": "部分失效",
    "cached_diluted": "命中·新输入稀释",
}

_CACHE_TIER_PRIORITY: Dict[str, int] = {"red": 3, "purple": 2, "orange": 1, "green": 0}
_VALID_WINDOW_MODES = ("60", "180", "abnormal", "all")

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"
_READOUT_WIDTH_PROBE = (
    "锁定 #9999 11-24 06:15:10 │ [Hydrogen XH] │ 上下文 512.0K (+99.9K) "
    "│ 缓存 100.0% [回溯·稀释命中] │ 首字 123.4s │ 999 t/s\n"
    "构成: 缓存命中 512.0K · 新输入 99.9K · 输出 99.9K (流 123.4s) "
    "· 图99(99.9M) · 合并 999 步（左键锁定 / ←→ 逐拍）"
)


@dataclass(frozen=True, **_DATACLASS_SLOTS)
class ChartBucket:
    """Aggregated pixel bucket preserving worst TTFT peak, worst cache tier, and true point indices."""

    start_idx: int
    end_idx: int
    count: int
    ctx_max_pt: ConversationDetailPoint
    ttft_worst_pt: Optional[ConversationDetailPoint]
    cache_worst_pt: ConversationDetailPoint
    cache_tier: str            # "red" | "purple" | "orange" | "green"
    dominant_model: str
    has_compact: bool
    has_revert: bool
    has_account_switch: bool
    ctx_max_idx: int = 0
    ttft_worst_idx: Optional[int] = None
    cache_worst_idx: int = 0


def _point_cache_tier(pt: ConversationDetailPoint) -> str:
    return resolve_cache_visual_tier(
        pt.classification,
        pt.reason,
        is_compact=bool(getattr(pt, "is_compact", False)),
        is_revert=bool(getattr(pt, "is_revert", False)),
    )


def _point_is_revert(pt: ConversationDetailPoint) -> bool:
    return bool(getattr(pt, "is_revert", False) or pt.reason == "revert_reset")


def _point_is_compact(pt: ConversationDetailPoint) -> bool:
    return (not _point_is_revert(pt)) and bool(getattr(pt, "is_compact", False) or pt.reason == "compact_reset")


def _point_is_account_switch(pt: ConversationDetailPoint) -> bool:
    return bool(getattr(pt, "is_account_switch", False) or pt.reason == "account_switch")


def aggregate_points_to_buckets(
    pts: List[ConversationDetailPoint],
    max_buckets: int,
) -> List[ChartBucket]:
    """Aggregate long step sequences into pixel buckets while preserving peaks and anomaly priorities."""
    n = len(pts)
    if n == 0:
        return []
    cap = max(1, int(max_buckets))
    if n <= cap:
        out: List[ChartBucket] = []
        for i, pt in enumerate(pts):
            tier = _point_cache_tier(pt)
            has_ttft = pt.ttft_sec is not None
            out.append(
                ChartBucket(
                    start_idx=i,
                    end_idx=i,
                    count=1,
                    ctx_max_pt=pt,
                    ttft_worst_pt=pt if has_ttft else None,
                    cache_worst_pt=pt,
                    cache_tier=tier,
                    dominant_model=pt.model or "",
                    has_compact=_point_is_compact(pt),
                    has_revert=_point_is_revert(pt),
                    has_account_switch=_point_is_account_switch(pt),
                    ctx_max_idx=i,
                    ttft_worst_idx=i if has_ttft else None,
                    cache_worst_idx=i,
                )
            )
        return out

    buckets: List[ChartBucket] = []
    for b in range(cap):
        b_start = (b * n) // cap
        b_end_excl = ((b + 1) * n) // cap
        if b_end_excl <= b_start:
            continue

        first_pt = pts[b_start]
        ctx_max_idx = b_start
        ctx_max_pt = first_pt

        ttft_worst_idx: Optional[int] = b_start if first_pt.ttft_sec is not None else None
        ttft_worst_pt: Optional[ConversationDetailPoint] = first_pt if first_pt.ttft_sec is not None else None

        cache_worst_idx = b_start
        cache_worst_pt = first_pt
        cache_worst_tier = _point_cache_tier(first_pt)
        cache_worst_prio = _CACHE_TIER_PRIORITY.get(cache_worst_tier, 0)

        model_counts: Dict[str, int] = {}
        has_compact = False
        has_revert = False
        has_account_switch = False

        for idx in range(b_start, b_end_excl):
            pt = pts[idx]
            if pt.total_tokens > ctx_max_pt.total_tokens:
                ctx_max_pt = pt
                ctx_max_idx = idx

            if pt.ttft_sec is not None:
                if ttft_worst_pt is None or float(pt.ttft_sec) > float(ttft_worst_pt.ttft_sec or -1.0):
                    ttft_worst_pt = pt
                    ttft_worst_idx = idx

            tier = _point_cache_tier(pt)
            prio = _CACHE_TIER_PRIORITY.get(tier, 0)
            if prio > cache_worst_prio or (
                prio == cache_worst_prio and float(pt.hit_rate) < float(cache_worst_pt.hit_rate)
            ):
                cache_worst_pt = pt
                cache_worst_idx = idx
                cache_worst_tier = tier
                cache_worst_prio = prio

            if pt.model:
                model_counts[pt.model] = model_counts.get(pt.model, 0) + 1
            if _point_is_revert(pt):
                has_revert = True
            elif _point_is_compact(pt):
                has_compact = True
            if _point_is_account_switch(pt):
                has_account_switch = True

        dominant_model = ""
        if model_counts:
            best_cnt = -1
            for m_name, cnt in model_counts.items():
                if cnt >= best_cnt:
                    best_cnt = cnt
                    dominant_model = m_name

        buckets.append(
            ChartBucket(
                start_idx=b_start,
                end_idx=b_end_excl - 1,
                count=b_end_excl - b_start,
                ctx_max_pt=ctx_max_pt,
                ttft_worst_pt=ttft_worst_pt,
                cache_worst_pt=cache_worst_pt,
                cache_tier=cache_worst_tier,
                dominant_model=dominant_model,
                has_compact=has_compact,
                has_revert=has_revert,
                has_account_switch=has_account_switch,
                ctx_max_idx=ctx_max_idx,
                ttft_worst_idx=ttft_worst_idx,
                cache_worst_idx=cache_worst_idx,
            )
        )
    return buckets


def _filter_points_for_mode(
    all_pts: List[ConversationDetailPoint],
    mode: str,
) -> List[ConversationDetailPoint]:
    if not all_pts:
        return []
    if mode == "60":
        return list(all_pts[-60:]) if len(all_pts) > 60 else list(all_pts)
    if mode == "180":
        return list(all_pts[-180:]) if len(all_pts) > 180 else list(all_pts)
    if mode == "abnormal":
        abn_indices = [i for i, p in enumerate(all_pts) if p.classification == "abnormal_miss"]
        if not abn_indices:
            return []
        keep = set()
        n = len(all_pts)
        for idx in abn_indices:
            for nb in (idx - 1, idx, idx + 1):
                if 0 <= nb < n:
                    keep.add(nb)
        return [all_pts[i] for i in sorted(keep)]
    return list(all_pts)


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
    """Compute window-scoped KPI aggregates matching the active chart range."""
    if not pts:
        return {
            "peak_context_tokens": 0,
            "compact_count": 0,
            "revert_count": 0,
            "cache_hit_steps": 0,
            "abnormal_miss_steps": 0,
            "expected_cold_steps": 0,
            "cacheable_hit_ratio": None,
            "cacheable_token_hit_ratio": None,
            "avg_ttft_sec": None,
            "p95_ttft_sec": None,
            "max_ttft_sec": None,
            "avg_tps": None,
            "latest_tps": None,
        }
    peak_ctx = max(p.total_tokens for p in pts)
    revert_count = sum(1 for p in pts if _point_is_revert(p))
    compact_count = sum(1 for p in pts if _point_is_compact(p))
    hit_steps = sum(1 for p in pts if p.classification == "hit")
    abn_steps = sum(1 for p in pts if p.classification == "abnormal_miss")
    exp_cold_steps = sum(1 for p in pts if p.classification == "expected_cold")
    cacheable_total = hit_steps + abn_steps
    hit_ratio = (hit_steps / cacheable_total) if cacheable_total > 0 else None
    cacheable_cr_sum = sum(
        int(p.cache_read_tokens)
        for p in pts
        if p.classification in ("hit", "abnormal_miss")
    )
    cacheable_tok_sum = sum(
        int(p.total_tokens)
        for p in pts
        if p.classification in ("hit", "abnormal_miss")
    )
    token_hit_ratio = (cacheable_cr_sum / float(cacheable_tok_sum) * 100.0) if cacheable_tok_sum > 0 else None

    ttfts = sorted(p.ttft_sec for p in pts if p.ttft_sec is not None)
    avg_ttft = (sum(ttfts) / len(ttfts)) if ttfts else None
    max_ttft = ttfts[-1] if ttfts else None
    p95_ttft = ttfts[min(len(ttfts) - 1, int(round(0.95 * (len(ttfts) - 1))))] if ttfts else None

    avg_tps: Optional[float] = compute_weighted_avg_tps(
        [(p.tps, p.streaming_sec, p.output_tokens) for p in pts]
    )
    latest_tps = resolve_latest_same_family_tps(pts)
    return {
        "peak_context_tokens": peak_ctx,
        "compact_count": compact_count,
        "revert_count": revert_count,
        "cache_hit_steps": hit_steps,
        "abnormal_miss_steps": abn_steps,
        "expected_cold_steps": exp_cold_steps,
        "cacheable_hit_ratio": hit_ratio,
        "cacheable_token_hit_ratio": token_hit_ratio,
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
        on_drag_state_changed: Optional[Callable[[bool], None]] = None,
        apply_rounding_fn: Optional[Callable[[int], None]] = None,
        screen_bounds_fn: Optional[Callable[[tk.Tk], Tuple[int, int, int, int]]] = None,
        slow_tps_threshold: float = 40.0,
        async_load: bool = False,
    ) -> None:
        self._parent = parent
        self.theme: Dict[str, str] = theme
        self._load_report = load_report
        self._on_toggle_pin = on_toggle_pin
        self._is_pinned_fn = is_pinned_fn
        self._on_closed = on_closed
        self._on_drag_state_changed = on_drag_state_changed
        self._apply_rounding_fn = apply_rounding_fn
        self._screen_bounds_fn = screen_bounds_fn
        self._slow_tps_threshold: float = max(1.0, float(slow_tps_threshold))
        self._async_load: bool = bool(async_load)

        self._conv_id: str = ""
        self._report: Optional[ConversationDetailReport] = None
        self._current_wstats: Dict[str, Any] = {}
        self._window_mode: str = "60"  # "60" | "180" | "abnormal" | "all"
        self._display_points: List[ConversationDetailPoint] = []
        self._display_buckets: List[ChartBucket] = []
        self._locked_point_idx: Optional[int] = None
        self._chart_meta: Dict[str, Dict[str, Any]] = {}
        self._last_sync_sig: Optional[Tuple[Any, ...]] = None
        self._is_historical_badge: bool = False
        self._hovering_chart: bool = False
        self._locked_size: Optional[Tuple[int, int]] = None
        self._last_canvas_sizes: Dict[str, Tuple[int, int]] = {}
        self._dragging: bool = False
        self._pending_sync_snap: Optional[MonitorSnapshot] = None
        self._load_gen: int = 0
        self._load_busy: bool = False
        self._pending_reload: bool = False
        self._pending_loaded_report: Optional[Tuple[int, str, Optional[ConversationDetailReport]]] = None
        self._closed: bool = False
        self._after_ids: List[str] = []

        self.win = tk.Toplevel(parent)
        self.win.title("单对话长线体检")
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.bind("<Escape>", self._on_escape_key)
        self.win.bind("<Left>", lambda _e: self._step_locked_point(-1))
        self.win.bind("<Right>", lambda _e: self._step_locked_point(1))
        self.win.bind("<FocusOut>", self._on_focus_out)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._dragger: Optional[LightweightProxyDragger] = LightweightProxyDragger(
            self.win,
            move_threshold=3,
            on_drag_start=self._on_dragger_started,
            on_drag_end=self._on_dragger_ended,
            on_drag_cancel=self._on_dragger_cancelled,
            size_provider=self._lock_window_size,
            theme_provider=lambda: self.theme,
            title_provider=self._get_drag_proxy_title,
            header_height_provider=self._get_header_height,
            screen_bounds_fn=self._screen_bounds_fn,
        )

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
        self._lbl_live_tag.pack(side="left", padx=(0, 4))
        self._range_btns: Dict[str, tk.Label] = {}
        for mode, label in (
            ("60", "近 60 步"),
            ("180", "近 180 步"),
            ("abnormal", "仅异常"),
            ("all", "全部步数"),
        ):
            b = tk.Label(self._range_bar, text=label, font=(UI_FONT, 8), padx=5, pady=2, cursor="hand2")
            b.pack(side="left", padx=(3, 0))
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
            w.bind("<ButtonRelease-1>", self._on_drag_end)

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
        chart_heights = {"ctx": 116, "ttft": 110, "cache": 80}
        for key, title, legend in self._chart_specs:
            frame = tk.Frame(self._surface, padx=8, pady=6, highlightthickness=1)
            frame.pack(fill="x", pady=(0, 7))
            top = tk.Frame(frame)
            top.pack(fill="x", pady=(0, 3))
            lt = tk.Label(top, text=title, font=(UI_FONT, 8, "bold"), anchor="w")
            lt.pack(side="left")
            ll = tk.Label(top, text=legend, font=(UI_FONT, 8), anchor="e")
            ll.pack(side="right")
            cv = tk.Canvas(frame, width=700, height=chart_heights[key], highlightthickness=0, bd=0, cursor="crosshair")
            cv.pack(fill="x")
            cv.bind("<Motion>", lambda e, k=key: self._on_chart_hover(k, e))
            cv.bind("<Button-1>", lambda e, k=key: self._on_chart_click(k, e))
            cv.bind("<Leave>", lambda _e: self._clear_chart_crosshair())
            cv.bind("<Configure>", lambda _e, k=key: self._on_chart_configure(k))
            self._charts[key] = {"frame": frame, "top": top, "title": lt, "legend": ll, "canvas": cv}

        # Live 2-line crosshair readout strip (single Label with '\n' measured with worst-case probe)
        self._readout_box = tk.Frame(self._surface, padx=8, pady=5, highlightthickness=1)
        self._readout_box.pack(fill="x", pady=(0, 8))
        self._lbl_readout = tk.Label(
            self._readout_box,
            text=_READOUT_WIDTH_PROBE,
            font=(MONO_FONT, 8),
            anchor="w",
            justify="left",
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

    def update_slow_tps_threshold(self, threshold: float) -> None:
        """Hot-reload configured slow TPS threshold from settings window."""
        if self._closed:
            return
        self._slow_tps_threshold = max(1.0, float(threshold))
        if self._report is not None:
            self._render_report()

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
        """Measure DPI-aware baseline dimensions once and permanently lock geometry to prevent width/height jitter."""
        if self._locked_size is not None:
            return self._locked_size
        self.win.update_idletasks()
        hdr_h = max(24, int(self._hdr.winfo_reqheight()))
        readout_h = max(40, int(self._readout_box.winfo_reqheight()))
        req_w = int(self.win.winfo_reqwidth())
        req_h = int(self.win.winfo_reqheight())

        if self._screen_bounds_fn is not None:
            vx, vy, vr, vb = self._screen_bounds_fn(self._parent)
            avail_w = max(680, (vr - vx) - 40)
            avail_h = max(500, (vb - vy) - 64)
        else:
            avail_w = max(680, self._parent.winfo_screenwidth() - 40)
            avail_h = max(500, self._parent.winfo_screenheight() - 64)

        locked_w = min(avail_w, max(760, req_w + 16))
        locked_h = min(avail_h, max(560, req_h))

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
            text="移动鼠标到任意图表查看单步上下文、首字等待、吞吐与缓存原因\n"
            "提示：左键点击图表可锁定单步，使用 ← / → 键逐拍精调，按 Esc 解锁"
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
        return (
            conv.conversation_id,
            getattr(conv, "full_step_count", conv.total_step_count),
            round(getattr(conv, "full_latest_timestamp", conv.latest_timestamp), 2),
            round(conv.ttft_sec, 2) if conv.ttft_sec is not None else None,
            round(conv.tps, 1) if conv.tps is not None else None,
            conv.title,
        )

    def _render_loading_placeholder(
        self,
        conv_id: str,
        initial_snap: Optional[MonitorSnapshot] = None,
    ) -> None:
        """Render instant lightweight loading placeholder (<50ms) before background report finishes."""
        t = self.theme
        self._report = None
        self._display_points = []
        self._display_buckets = []
        self._current_wstats = {}
        conv = None
        if initial_snap is not None:
            conv = next((c for c in initial_snap.active_conversations if c.conversation_id == conv_id), None)
        if conv is not None and conv.title:
            self._lbl_title.configure(text=truncate_display_width(conv.title, 18))
            self._lbl_sub.configure(text=f"{conv.short_id} · 后台加载中…")
            if conv.is_subagent:
                self._lbl_role.configure(text="子代理", bg=t["card_alt_bg"], fg=t["text_secondary"])
            else:
                self._lbl_role.configure(text="主对话", bg=t["green_bg"], fg=t["green"])
        else:
            self._lbl_title.configure(text=f"{conv_id[:6]} · 加载中…")
            self._lbl_sub.configure(text="后台读取完整轨迹")
        for kc in self._kpi_cards:
            kc["val"].configure(text="…", fg=t["text_muted"])
            kc["sub"].configure(text="读取中", fg=t["text_muted"])
        for ch in self._charts.values():
            cv = ch["canvas"]
            cv.delete("all")
            w = max(360, int(cv.winfo_width() if cv.winfo_width() > 20 else cv["width"]))
            h = max(60, int(cv.winfo_height() if cv.winfo_height() > 20 else cv["height"]))
            cv.create_text(w / 2, h / 2, text="正在后台读取完整轨迹与速度记录…", font=(UI_FONT, 8), fill=t["text_muted"])
        self._lbl_readout.configure(
            text="正在后台加载单对话长线体检数据，主界面保持零卡顿…\n加载完成后将自动绘制上下文、首字延迟与缓存命中曲线",
            fg=t["text_secondary"],
        )
        self._sync_pin_button()

    def show_conversation(
        self,
        conv_id: str,
        place_near_parent: bool = True,
        initial_snap: Optional[MonitorSnapshot] = None,
    ) -> None:
        if self._closed:
            return
        self._pending_sync_snap = None
        self._pending_loaded_report = None
        self._pending_reload = False
        if self._dragger is not None:
            self._dragger.finish_or_cancel(commit=self._dragger.moved, force=True)
        self._load_gen += 1
        self._conv_id = conv_id
        self._last_sync_sig = None
        self._hovering_chart = False
        self._locked_point_idx = None
        self._set_live_badge(False)
        self._lock_window_size()

        if initial_snap is not None:
            conv = next((c for c in initial_snap.active_conversations if c.conversation_id == conv_id), None)
            if conv is not None:
                self._last_sync_sig = self._build_sync_sig(conv, initial_snap)

        if self._async_load:
            self._render_loading_placeholder(conv_id, initial_snap)
            self.win.deiconify()
            self.win.lift()
            if place_near_parent:
                self._place_window()
            else:
                w, h = self._lock_window_size()
                self.win.geometry(f"{w}x{h}+{self.win.winfo_x()}+{self.win.winfo_y()}")
            if self._apply_rounding_fn is not None:
                try:
                    self._apply_rounding_fn(resolve_root_hwnd(self.win))
                except Exception:
                    pass
            self._schedule_warm_up_drag_proxy()
            self.refresh(blocking=False)
            return

        self.win.deiconify()
        self.win.lift()
        self.win.update_idletasks()
        self.refresh(blocking=True)
        self.win.update_idletasks()
        if place_near_parent:
            self._place_window()
        else:
            w, h = self._lock_window_size()
            self.win.geometry(f"{w}x{h}+{self.win.winfo_x()}+{self.win.winfo_y()}")
        if self._apply_rounding_fn is not None:
            try:
                self._apply_rounding_fn(resolve_root_hwnd(self.win))
            except Exception:
                pass
        self._schedule_warm_up_drag_proxy()

    def _schedule_warm_up_drag_proxy(self) -> None:
        if self._dragger is not None and not self._closed and not getattr(self._dragger, "_proxy_warmed", False):
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

    def _get_drag_proxy_title(self) -> str:
        try:
            t = str(self._lbl_title.cget("text") or "").strip()
            if t and t != "—":
                return f"单对话长线体检 · {truncate_display_width(t, 36)}"
        except tk.TclError:
            pass
        return "单对话长线体检"

    def _tick_own_freshness_kpi(self, now_ts: float) -> None:
        """Keep the detail window's own freshness countdown ticking even if left panel switches to another account."""
        rep = self._report
        if rep is None or not rep.points:
            return
        latest_pt = rep.points[-1]
        latest_tps = self._current_wstats.get("latest_tps") if self._current_wstats else rep.latest_tps
        if rep.freshness_invalid_reason:
            self._update_freshness_kpi(None, rep.freshness_invalid_reason, latest_tps, getattr(rep, "consec_miss", 0))
            return
        if rep.freshness_remaining_sec is not None and latest_pt.timestamp > 0:
            rem = int(round(rep.freshness_window_sec - (now_ts - latest_pt.timestamp)))
            self._update_freshness_kpi(rem, "", latest_tps, 0)

    def sync_from_snapshot(self, snap: MonitorSnapshot) -> None:
        """Keep the open detail modal dynamically in sync with the main 2.5s poll loop."""
        if self._closed or not self._conv_id:
            return
        if self._dragging:
            self._pending_sync_snap = snap
            return
        conv = next((c for c in snap.active_conversations if c.conversation_id == self._conv_id), None)
        if conv is None:
            self._tick_own_freshness_kpi(snap.generated_at)
            self._sync_pin_button()
            return
        sig = self._build_sync_sig(conv, snap)
        if sig != self._last_sync_sig:
            self._last_sync_sig = sig
            self.refresh()
        else:
            if self._current_wstats:
                latest_tps = self._current_wstats.get("latest_tps")
            else:
                latest_tps = self._report.latest_tps if self._report is not None else conv.tps
            if not (snap.is_custom_account_view or snap.history_range != "30m"):
                self._update_freshness_kpi(
                    conv.freshness_remaining_sec,
                    conv.freshness_invalid_reason,
                    latest_tps,
                    conv.consec_miss,
                )
            else:
                self._tick_own_freshness_kpi(snap.generated_at)
            self._sync_pin_button()

    def _compute_max_buckets(self, plot_w: int) -> int:
        return max(80, min(180, int(max(1, plot_w) // 3)))

    def _on_chart_configure(self, chart_key: str) -> None:
        if self._closed or not self._display_points:
            return
        cv = self._charts[chart_key]["canvas"]
        cur_sz = (int(cv.winfo_width()), int(cv.winfo_height()))
        if cur_sz[0] <= 20 or cur_sz[1] <= 20:
            return
        if self._last_canvas_sizes.get(chart_key) == cur_sz:
            return
        w, _h, pad_l, pad_r, _pad_t, _pad_b = self._chart_geometry(cv)
        plot_w = max(20, w - pad_l - pad_r)
        self._display_buckets = aggregate_points_to_buckets(self._display_points, self._compute_max_buckets(plot_w))
        if chart_key == "ctx":
            self._draw_context_chart(self._display_points, self._display_buckets)
        elif chart_key == "ttft":
            self._draw_ttft_chart(self._display_points, self._display_buckets)
        elif chart_key == "cache":
            self._draw_cache_chart(self._display_points, self._display_buckets)
        self._redraw_lock_lines()

    def _place_window(self) -> None:
        w, h = self._lock_window_size()
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

    def _flush_pending_sync(self) -> None:
        was_dragging = self._dragging
        self._dragging = False
        pending_loaded = self._pending_loaded_report
        self._pending_loaded_report = None
        pending = self._pending_sync_snap
        self._pending_sync_snap = None
        if not self._closed:
            if pending_loaded is not None:
                gen, cid, loaded = pending_loaded
                if gen == self._load_gen and cid == self._conv_id:
                    self._report = loaded
                    self._render_report()
            if pending is not None and not self._closed:
                self.sync_from_snapshot(pending)
        if was_dragging:
            self._notify_drag_state(False)

    def _on_dragger_ended(self, _moved: bool) -> None:
        self._flush_pending_sync()

    def _on_dragger_cancelled(self) -> None:
        self._flush_pending_sync()

    def check_drag_watchdog(self) -> None:
        if not self._closed and self._dragger is not None:
            self._dragger.check_release_watchdog()

    def _set_window_mode(self, mode: str) -> None:
        if mode not in _VALID_WINDOW_MODES:
            return
        self._window_mode = mode
        self._locked_point_idx = None
        self._hovering_chart = False
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
        if not self._conv_id or self._is_pinned_fn is None:
            return
        pinned = self._is_pinned_fn(self._conv_id)
        if pinned:
            self._act_btns["pin"].configure(text="📌 已聚焦 (点击取消)", fg=self.theme["cyan"])
        else:
            self._act_btns["pin"].configure(text="📌 聚焦到主面板", fg=self.theme["btn_text"])

    def _handle_toggle_pin(self) -> None:
        if not self._conv_id or self._on_toggle_pin is None:
            return
        self._on_toggle_pin(self._conv_id)
        self._sync_pin_button()

    def refresh(self, blocking: Optional[bool] = None) -> None:
        if self._closed or not self._conv_id:
            return
        loader = self._load_report
        if loader is None:
            return
        use_async = (not blocking) if blocking is not None else self._async_load
        if not use_async:
            self._load_gen += 1
            self._pending_reload = False
            self._pending_loaded_report = None
            self._report = loader(self._conv_id)
            self._render_report()
            return

        if self._load_busy:
            self._pending_reload = True
            return

        self._load_busy = True
        self._pending_reload = False
        gen = self._load_gen
        target_cid = self._conv_id

        def _bg_load() -> None:
            loaded: Optional[ConversationDetailReport] = None
            err_msg = ""
            try:
                loaded = loader(target_cid)
            except Exception as exc:
                err_msg = str(exc)

            def _apply() -> None:
                # Always release busy flag first regardless of stale generation so queued reload proceeds
                self._load_busy = False
                if self._closed:
                    self._pending_reload = False
                    self._pending_loaded_report = None
                    return
                is_current = (gen == self._load_gen and target_cid == self._conv_id)
                if is_current:
                    if err_msg:
                        self._lbl_readout.configure(
                            text=f"后台读取详情失败：{err_msg}\n请稍后点击「刷新数据」重试",
                            fg=self.theme["red"],
                        )
                    elif self._dragging:
                        self._pending_loaded_report = (gen, target_cid, loaded)
                    else:
                        self._report = loaded
                        self._render_report()
                if self._pending_reload:
                    self._pending_reload = False
                    if not self._closed and self._conv_id:
                        self.refresh(blocking=False)

            try:
                if not self._closed:
                    self.win.after(0, _apply)
                else:
                    self._load_busy = False
            except (tk.TclError, RuntimeError):
                self._load_busy = False

        threading.Thread(target=_bg_load, name="DetailReportLoadThread", daemon=True).start()

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
        prev_aid = getattr(self, "_copy_reset_aid", None)
        if prev_aid:
            try:
                self.win.after_cancel(prev_aid)
            except tk.TclError:
                pass
            if prev_aid in self._after_ids:
                self._after_ids.remove(prev_aid)
        aid = self.win.after(
            1500,
            lambda: btn.configure(text="复制长线详报", fg=self.theme["btn_text"]) if not self._closed else None,
        )
        self._copy_reset_aid = aid
        self._after_ids.append(aid)

    def _render_report(self) -> None:
        t = self.theme
        rep = self._report
        self._sync_pin_button()
        if rep is None:
            self._display_points = []
            self._display_buckets = []
            self._locked_point_idx = None
            self._current_wstats = {}
            self._lbl_title.configure(text=f"{self._conv_id[:6]} · 未找到轨迹文件")
            self._lbl_sub.configure(text="")
            for kc in self._kpi_cards:
                kc["val"].configure(text="—", fg=t["text_muted"])
                kc["sub"].configure(text="—", fg=t["text_muted"])
            self._lbl_readout.configure(
                text="未找到该对话的轨迹文件或当前视角暂无可展示数据\n请确认对话文件未被清理后点击「刷新数据」",
                fg=t["text_muted"],
            )
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
        self._set_live_badge(bool(getattr(rep, "scope_label", "")))
        if rep.title:
            disp_name = truncate_display_width(rep.title, 18)
            self._lbl_title.configure(text=disp_name)
            self._lbl_sub.configure(text=f"{rep.short_id} · {model_tag} · {rep.total_steps}步(今{rep.today_steps}){scope_tag}")
            self.win.title(f"{rep.title} ({rep.short_id}) - 长线体检")
        else:
            self._lbl_title.configure(text=f"{rep.short_id} · {model_tag}")
            self._lbl_sub.configure(text=f"总计 {rep.total_steps} 步 (今日 {rep.today_steps} 步){scope_tag}")
            self.win.title(f"{rep.short_id} - 长线体检")

        pts = _filter_points_for_mode(rep.points, self._window_mode)
        self._display_points = pts
        if self._locked_point_idx is not None and not (0 <= self._locked_point_idx < len(pts)):
            self._locked_point_idx = None

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

        # Recompute all 4 KPI cards strictly for the active window
        wstats = summarize_detail_points(pts)
        self._current_wstats = wstats
        large_ctx_allow = compute_large_ctx_ttft_allowance(rep.latest_context_tokens, rep.max_context_tokens)
        cal_title_tag = f" (+{large_ctx_allow:.0f}s校)" if large_ctx_allow >= 10.0 else ""
        is_sliced_60 = self._window_mode == "60" and len(rep.points) > 60
        is_sliced_180 = self._window_mode == "180" and len(rep.points) > 180
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
        elif is_sliced_180:
            self._kpi_cards[0]["title"].configure(text="请求体规模 (近180步)")
            self._kpi_cards[1]["title"].configure(text="近 180 步缓存命中")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT (近180步){cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜 (近180步)")
        elif self._window_mode == "180":
            self._kpi_cards[0]["title"].configure(text="请求体规模 / 变化")
            self._kpi_cards[1]["title"].configure(text=f"近 180 步命中 (全{len(rep.points)}步)")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT{cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜")
        elif self._window_mode == "abnormal":
            self._kpi_cards[0]["title"].configure(text="请求体规模 (仅异常)")
            self._kpi_cards[1]["title"].configure(text="异常邻域缓存命中")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT (仅异常){cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜 (仅异常)")
        else:
            self._kpi_cards[0]["title"].configure(text="请求体规模 / 变化")
            self._kpi_cards[1]["title"].configure(text="全程缓存有效命中")
            self._kpi_cards[2]["title"].configure(text=f"首字延迟 TTFT (全程){cal_title_tag}")
            self._kpi_cards[3]["title"].configure(text="生成吞吐 / 保鲜 (全程)")

        # KPI 1: Context size & delta
        delta_s = format_delta_tokens(rep.latest_context_delta)
        latest_is_revert = bool(pts) and _point_is_revert(pts[-1])
        latest_is_compact = bool(pts) and _point_is_compact(pts[-1])
        delta_col = t["orange"] if latest_is_revert else (t["purple"] if latest_is_compact else t["cyan"])
        self._kpi_cards[0]["val"].configure(text=f"{format_tokens(rep.latest_context_tokens)} ({delta_s})", fg=delta_col)
        rev_part = f" · 回溯 {wstats['revert_count']}" if wstats["revert_count"] > 0 else ""
        self._kpi_cards[0]["sub"].configure(
            text=f"峰值 {format_tokens(wstats['peak_context_tokens'])} · 压缩 {wstats['compact_count']}{rev_part}",
            fg=t["text_secondary"],
        )

        # KPI 2: Cache hit ratio
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
        tok_pct = wstats.get("cacheable_token_hit_ratio")
        if tok_pct is None and self._window_mode == "all":
            rep_tok = getattr(rep, "cacheable_token_hit_ratio", None)
            if rep_tok is not None:
                tok_pct = float(rep_tok)
        if tok_pct is not None:
            kpi2_sub = f"中{wstats['cache_hit_steps']} 异{abn} 冷{wstats['expected_cold_steps']} · Token {tok_pct:.1f}%"
            self._kpi_cards[1]["sub"].configure(text=kpi2_sub, fg=sub_col, font=(UI_FONT, 7))
        else:
            kpi2_sub = f"命中 {wstats['cache_hit_steps']} · 异常 {abn} · 冷 {wstats['expected_cold_steps']}"
            self._kpi_cards[1]["sub"].configure(text=kpi2_sub, fg=sub_col, font=(UI_FONT, 8))

        # KPI 3: TTFT
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

        # KPI 4: TPS & Freshness
        avg_tps = wstats["avg_tps"]
        if avg_tps is not None:
            tps_col = t["orange"] if avg_tps < self._slow_tps_threshold else t["cyan"]
            self._kpi_cards[3]["val"].configure(text=f"均 {avg_tps:.0f} t/s", fg=tps_col)
        else:
            self._kpi_cards[3]["val"].configure(text="— t/s", fg=t["text_muted"])
        if self._is_historical_badge:
            self._update_freshness_kpi(None, "", wstats["latest_tps"], 0)
        else:
            self._update_freshness_kpi(
                rep.freshness_remaining_sec,
                rep.freshness_invalid_reason,
                wstats["latest_tps"],
                getattr(rep, "consec_miss", 0),
            )

        self.win.update_idletasks()
        ctx_cv = self._charts["ctx"]["canvas"]
        w, _h, pad_l, pad_r, _pad_t, _pad_b = self._chart_geometry(ctx_cv)
        plot_w = max(20, w - pad_l - pad_r)
        buckets = aggregate_points_to_buckets(pts, self._compute_max_buckets(plot_w))
        self._display_buckets = buckets

        self._draw_context_chart(pts, buckets)
        self._draw_ttft_chart(pts, buckets)
        self._draw_cache_chart(pts, buckets)
        self._redraw_lock_lines()

        if self._locked_point_idx is not None and 0 <= self._locked_point_idx < len(pts):
            b_cnt = self._bucket_count_for_point_idx(self._locked_point_idx)
            self._update_readout_for_point(pts[self._locked_point_idx], prefix="锁定", bucket_count=b_cnt, locked=True)
        elif pts and not self._hovering_chart:
            self._update_readout_for_point(pts[-1], prefix="最新步")
        elif not pts and self._window_mode == "abnormal":
            self._lbl_readout.configure(
                text="当前对话无异常掉缓存步，全程缓存表现平稳\n可点击上方「近 60 步」或「全部步数」查看完整轨迹",
                fg=t["green"],
            )

    def _update_freshness_kpi(
        self,
        freshness_remaining_sec: Optional[int],
        freshness_invalid_reason: str,
        latest_tps: Optional[float],
        consec_miss: int = 0,
    ) -> None:
        t = self.theme
        if freshness_invalid_reason:
            is_severe = is_severe_freshness_invalid(freshness_invalid_reason, consec_miss)
            f_txt, f_col = freshness_invalid_reason, (t["red"] if is_severe else t["orange"])
        elif freshness_remaining_sec is not None:
            if freshness_remaining_sec > 60:
                f_txt, f_col = f"保鲜剩 {format_countdown(freshness_remaining_sec)}", t["green"]
            elif freshness_remaining_sec > 0:
                f_txt, f_col = f"即将冷却 剩 {format_countdown(freshness_remaining_sec)}", t["orange"]
            elif freshness_remaining_sec >= -FRESHNESS_RESCUE_WINDOW_SEC:
                rescue_left = max(0, FRESHNESS_RESCUE_WINDOW_SEC + freshness_remaining_sec)
                cd_s = format_countdown(rescue_left) if rescue_left > 0 else "0:00"
                f_txt, f_col = f"临界抢救 剩 {cd_s}", t["red"]
            else:
                f_txt, f_col = "大上下文保鲜: 已冷却", t["orange"]
        else:
            latest_p = f"新 {latest_tps:.0f} t/s" if latest_tps is not None else "常规上下文"
            f_txt, f_col = latest_p, t["text_secondary"]
        self._kpi_cards[3]["sub"].configure(text=f_txt, fg=f_col)

    def _draw_model_band_buckets(
        self,
        cv: tk.Canvas,
        buckets: List[ChartBucket],
        pad_l: int,
        plot_w: int,
        y0: float,
        y1: float,
        draw_switch_lines: bool = False,
        plot_top: float = 0.0,
        plot_bot: float = 0.0,
    ) -> None:
        """Draw a merged colored model strip along the top edge and optional model-switch markers."""
        n = len(buckets)
        if n == 0:
            return
        t = self.theme
        if n == 1:
            col = _model_band_color(buckets[0].dominant_model, t)
            cv.create_rectangle(pad_l, y0, pad_l + plot_w, y1, fill=col, outline="")
            return
        step_w = plot_w / float(n - 1)
        last_lbl_x = -999.0
        run_start = 0
        for idx in range(1, n + 1):
            is_end = idx == n
            changed = (not is_end) and (buckets[idx].dominant_model != buckets[run_start].dominant_model)
            if is_end or changed:
                x_left = pad_l if run_start == 0 else (self._x_for_idx(run_start, n, pad_l, plot_w) - step_w * 0.5)
                x_right = (pad_l + plot_w) if is_end else (self._x_for_idx(idx, n, pad_l, plot_w) - step_w * 0.5)
                col = _model_band_color(buckets[run_start].dominant_model, t)
                cv.create_rectangle(max(pad_l, x_left), y0, min(pad_l + plot_w, x_right), y1, fill=col, outline="")
                if changed and draw_switch_lines:
                    sx = self._x_for_idx(idx, n, pad_l, plot_w)
                    new_col = _model_band_color(buckets[idx].dominant_model, t)
                    cv.create_line(sx, plot_top, sx, plot_bot, fill=new_col, width=1.1, dash=(2, 3))
                    tag_txt = f"→{_short_model_tag(buckets[idx].dominant_model)}"
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

    def _draw_context_chart(
        self,
        pts: List[ConversationDetailPoint],
        buckets: Optional[List[ChartBucket]] = None,
    ) -> None:
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
            empty_msg = "当前对话无异常掉缓存步" if self._window_mode == "abnormal" else "暂无上下文轨迹数据"
            cv.create_text(w / 2, h / 2, text=empty_msg, font=(UI_FONT, 8), fill=t["green"] if self._window_mode == "abnormal" else t["text_muted"])
            return

        bks = buckets if buckets is not None else aggregate_points_to_buckets(pts, self._compute_max_buckets(plot_w))
        self._draw_model_band_buckets(cv, bks, pad_l, plot_w, y0=2, y1=6, draw_switch_lines=False)

        peak = max(b.ctx_max_pt.total_tokens for b in bks)
        y_max = max(64_000, int(peak * 1.12))

        for frac in (0.0, 0.5, 1.0):
            yy = pad_t + plot_h * (1.0 - frac)
            cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=t["divider"], dash=(2, 3))
            cv.create_text(pad_l - 6, yy, text=format_tokens(int(y_max * frac)), anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        m = len(bks)
        coords: List[float] = []
        last_lbl_x = -999.0
        for idx, bk in enumerate(bks):
            x = self._x_for_idx(idx, m, pad_l, plot_w)
            y = pad_t + plot_h * (1.0 - min(1.0, bk.ctx_max_pt.total_tokens / float(y_max)))
            coords.extend([x, y])
            mark_spec: Optional[Tuple[str, str, Tuple[int, int]]] = None
            if bk.has_revert:
                mark_spec = ("回溯", t["orange"], (2, 2))
            elif bk.has_compact:
                mark_spec = ("压缩", t["purple"], (3, 2))
            elif bk.has_account_switch:
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

        if m >= 2:
            poly = [coords[0], pad_t + plot_h] + coords + [coords[-2], pad_t + plot_h]
            cv.create_polygon(poly, fill=t["bar_track"], outline="")
            cv.create_line(coords, fill=t["cyan"], width=2.0)
        elif m == 1:
            cv.create_oval(coords[0] - 3, coords[1] - 3, coords[0] + 3, coords[1] + 3, fill=t["cyan"], outline="")

        peak_idx = max(range(m), key=lambda i: bks[i].ctx_max_pt.total_tokens)
        px = coords[peak_idx * 2]
        py = coords[peak_idx * 2 + 1]
        cv.create_oval(px - 3, py - 3, px + 3, py + 3, fill=t["text_primary"], outline=t["cyan"], width=1.5)
        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _draw_ttft_chart(
        self,
        pts: List[ConversationDetailPoint],
        buckets: Optional[List[ChartBucket]] = None,
    ) -> None:
        t = self.theme
        cv = self._charts["ttft"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        self._last_canvas_sizes["ttft"] = (w, h)
        pad_t = 22
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["ttft"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        if not pts:
            empty_msg = "当前对话无异常掉缓存步" if self._window_mode == "abnormal" else "当前范围无 SQLite 首字延迟记录"
            cv.create_text(w / 2, h / 2, text=empty_msg, font=(UI_FONT, 8), fill=t["green"] if self._window_mode == "abnormal" else t["text_muted"])
            return

        valid_vals = [p.ttft_sec for p in pts if p.ttft_sec is not None]
        if not valid_vals:
            cv.create_text(w / 2, h / 2, text="当前范围无 SQLite 首字延迟记录", font=(UI_FONT, 8), fill=t["text_muted"])
            return

        bks = buckets if buckets is not None else aggregate_points_to_buckets(pts, self._compute_max_buckets(plot_w))
        self._draw_model_band_buckets(
            cv,
            bks,
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

        m = len(bks)
        bar_w = max(2.0, min(8.0, (plot_w / float(max(1, m))) * 0.72))
        y_base = pad_t + plot_h
        for idx, bk in enumerate(bks):
            pt = bk.ttft_worst_pt
            if pt is None or pt.ttft_sec is None:
                continue
            x = self._x_for_idx(idx, m, pad_l, plot_w)
            sec = float(pt.ttft_sec)
            clipped = sec > y_max
            yy = pad_t if clipped else (pad_t + plot_h * (1.0 - sec / y_max))
            col = ttft_metric_color(sec, t)
            cv.create_rectangle(x - bar_w / 2.0, yy, x + bar_w / 2.0, y_base, fill=col, outline="")
            if clipped:
                cv.create_rectangle(x - bar_w / 2.0, pad_t, x + bar_w / 2.0, pad_t + 2.5, fill=t["text_primary"], outline="")

        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _draw_cache_chart(
        self,
        pts: List[ConversationDetailPoint],
        buckets: Optional[List[ChartBucket]] = None,
    ) -> None:
        t = self.theme
        cv = self._charts["cache"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        self._last_canvas_sizes["cache"] = (w, h)
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["cache"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        if not pts:
            empty_msg = "当前对话无异常掉缓存步" if self._window_mode == "abnormal" else "暂无缓存轨迹数据"
            cv.create_text(w / 2, h / 2, text=empty_msg, font=(UI_FONT, 8), fill=t["green"] if self._window_mode == "abnormal" else t["text_muted"])
            return

        bks = buckets if buckets is not None else aggregate_points_to_buckets(pts, self._compute_max_buckets(plot_w))

        for pct in (0, 50, 100):
            yy = pad_t + plot_h * (1.0 - pct / 100.0)
            cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=t["divider"], dash=(2, 3))
            cv.create_text(pad_l - 6, yy, text=f"{pct}%", anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        m = len(bks)
        y_base = pad_t + plot_h
        bar_w = max(2.0, min(9.0, (plot_w / float(max(1, m))) * 0.78))
        order = sorted(range(m), key=lambda i: _CACHE_TIER_PRIORITY.get(bks[i].cache_tier, 0))
        for idx in order:
            bk = bks[idx]
            pt = bk.cache_worst_pt
            tier = bk.cache_tier
            x = self._x_for_idx(idx, m, pad_l, plot_w)
            col = t[tier]
            has_cache_read = (int(getattr(pt, "cache_read_tokens", 0) or 0) > 0) or (pt.hit_rate > 0.05)
            if has_cache_read:
                ratio = max(0.16, min(1.0, pt.hit_rate / 100.0))
            elif tier == "red":
                ratio = 1.0
            elif tier == "purple":
                ratio = 0.74
            elif tier == "orange":
                ratio = 0.56
            else:
                ratio = max(0.16, min(1.0, pt.hit_rate / 100.0))
            yy = y_base - plot_h * ratio
            cv.create_rectangle(x - bar_w / 2.0, yy, x + bar_w / 2.0, y_base, fill=col, outline="")

        last_sw_x = -999.0
        for idx, bk in enumerate(bks):
            if bk.has_account_switch:
                sx = self._x_for_idx(idx, m, pad_l, plot_w)
                cv.create_line(sx, pad_t, sx, y_base, fill=t["cyan"], width=1.1, dash=(3, 2))
                if sx < pad_l + plot_w - 22.0:
                    if sx - last_sw_x >= 34.0:
                        cv.create_text(sx + 2, pad_t + 6, text="换号", anchor="w", font=(UI_FONT, 7, "bold"), fill=t["cyan"])
                        last_sw_x = sx
                elif sx - last_sw_x >= 34.0:
                    cv.create_text(sx - 2, pad_t + 6, text="换号", anchor="e", font=(UI_FONT, 7, "bold"), fill=t["cyan"])
                    last_sw_x = sx

        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _bucket_index_for_x(self, chart_key: str, event_x: float) -> Optional[int]:
        bks = self._display_buckets
        meta = self._chart_meta.get(chart_key)
        if not bks or not meta:
            return None
        pad_l = float(meta["pad_l"])
        plot_w = float(meta["plot_w"])
        if event_x < pad_l - 6 or event_x > pad_l + plot_w + 6:
            return None
        frac = max(0.0, min(1.0, (event_x - pad_l) / max(1.0, plot_w)))
        b_idx = int(round(frac * (len(bks) - 1))) if len(bks) > 1 else 0
        return max(0, min(len(bks) - 1, b_idx))

    @staticmethod
    def _bucket_rep_point_idx(bk: ChartBucket, chart_key: str) -> int:
        if chart_key == "ttft" and bk.ttft_worst_idx is not None:
            return bk.ttft_worst_idx
        if chart_key == "cache":
            return bk.cache_worst_idx
        return bk.ctx_max_idx

    def _bucket_index_for_point_idx(self, pt_idx: int) -> Optional[int]:
        bks = self._display_buckets
        if not bks:
            return None
        lo, hi = 0, len(bks) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            bk = bks[mid]
            if pt_idx < bk.start_idx:
                hi = mid - 1
            elif pt_idx > bk.end_idx:
                lo = mid + 1
            else:
                return mid
        return max(0, min(len(bks) - 1, lo))

    def _bucket_count_for_point_idx(self, pt_idx: int) -> int:
        b_idx = self._bucket_index_for_point_idx(pt_idx)
        if b_idx is None:
            return 1
        return self._display_buckets[b_idx].count

    def _redraw_lock_lines(self) -> None:
        for ch in self._charts.values():
            try:
                ch["canvas"].delete("lockline")
            except tk.TclError:
                pass
        if self._locked_point_idx is None or not self._display_buckets:
            return
        b_idx = self._bucket_index_for_point_idx(self._locked_point_idx)
        if b_idx is None:
            return
        m = len(self._display_buckets)
        for k, ch in self._charts.items():
            cv = ch["canvas"]
            meta = self._chart_meta.get(k)
            if not meta:
                continue
            cx = self._x_for_idx(b_idx, m, int(meta["pad_l"]), int(meta["plot_w"]))
            cv.create_line(
                cx,
                int(meta["pad_t"]),
                cx,
                int(meta["pad_t"]) + int(meta["plot_h"]),
                fill=self.theme["cyan"],
                width=1.6,
                tags="lockline",
            )

    def _on_chart_click(self, chart_key: str, event: tk.Event) -> None:
        if self._closed or not self._display_points or not self._display_buckets:
            return
        try:
            self.win.focus_set()
        except tk.TclError:
            pass
        b_idx = self._bucket_index_for_x(chart_key, float(event.x))
        if b_idx is None:
            return
        bk = self._display_buckets[b_idx]
        target_pt_idx = self._bucket_rep_point_idx(bk, chart_key)
        target_pt_idx = max(0, min(len(self._display_points) - 1, target_pt_idx))
        if self._locked_point_idx == target_pt_idx:
            self._unlock_point()
            return
        self._locked_point_idx = target_pt_idx
        self._redraw_lock_lines()
        self._update_readout_for_point(
            self._display_points[target_pt_idx],
            prefix="锁定",
            bucket_count=bk.count,
            locked=True,
        )

    def _unlock_point(self) -> None:
        self._locked_point_idx = None
        self._redraw_lock_lines()
        if self._display_points and not self._hovering_chart:
            self._update_readout_for_point(self._display_points[-1], prefix="最新步")

    def _step_locked_point(self, delta: int) -> None:
        if self._closed or not self._display_points:
            return
        n = len(self._display_points)
        if self._locked_point_idx is None:
            new_idx = (n - 1) if delta < 0 else 0
        else:
            new_idx = max(0, min(n - 1, self._locked_point_idx + int(delta)))
        self._locked_point_idx = new_idx
        self._redraw_lock_lines()
        b_cnt = self._bucket_count_for_point_idx(new_idx)
        self._update_readout_for_point(
            self._display_points[new_idx],
            prefix="锁定",
            bucket_count=b_cnt,
            locked=True,
        )

    def _on_escape_key(self, _event: tk.Event) -> Optional[str]:
        if self._locked_point_idx is not None:
            self._unlock_point()
            return "break"
        self.close()
        return "break"

    def _on_chart_hover(self, chart_key: str, event: tk.Event) -> None:
        pts = self._display_points
        bks = self._display_buckets
        if not pts or not bks:
            return
        b_idx = self._bucket_index_for_x(chart_key, float(event.x))
        if b_idx is None:
            self._clear_chart_crosshair()
            return
        self._hovering_chart = True
        bk = bks[b_idx]
        m = len(bks)

        for k, ch in self._charts.items():
            cv = ch["canvas"]
            cv.delete("crosshair")
            meta = self._chart_meta.get(k)
            if not meta:
                continue
            cx = self._x_for_idx(b_idx, m, int(meta["pad_l"]), int(meta["plot_w"]))
            cv.create_line(
                cx,
                int(meta["pad_t"]),
                cx,
                int(meta["pad_t"]) + int(meta["plot_h"]),
                fill=self.theme["text_secondary"],
                dash=(2, 2),
                tags="crosshair",
            )
        # Preserve locked step readout when user has explicitly locked a point
        if self._locked_point_idx is not None:
            return
        pt_idx = max(0, min(len(pts) - 1, self._bucket_rep_point_idx(bk, chart_key)))
        self._update_readout_for_point(pts[pt_idx], prefix="选定步", bucket_count=bk.count, locked=False)

    def _clear_chart_crosshair(self) -> None:
        self._hovering_chart = False
        for ch in self._charts.values():
            try:
                ch["canvas"].delete("crosshair")
            except tk.TclError:
                pass
        if self._locked_point_idx is not None and 0 <= self._locked_point_idx < len(self._display_points):
            b_cnt = self._bucket_count_for_point_idx(self._locked_point_idx)
            self._update_readout_for_point(
                self._display_points[self._locked_point_idx],
                prefix="锁定",
                bucket_count=b_cnt,
                locked=True,
            )
            return
        if self._display_points:
            self._update_readout_for_point(self._display_points[-1], prefix="最新步")

    def _update_readout_for_point(
        self,
        pt: ConversationDetailPoint,
        prefix: str = "选定步",
        bucket_count: int = 1,
        locked: bool = False,
    ) -> None:
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%m-%d %H:%M:%S") if pt.timestamp > 0 else "-- --:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f} t/s" if pt.tps is not None else "—"
        is_rev = bool(getattr(pt, "is_revert", False))
        is_cmp = bool(getattr(pt, "is_compact", False))
        is_acc_sw = bool(getattr(pt, "is_account_switch", False))
        base_tag = CACHE_REASON_CN_MAP.get(pt.reason, _CACHE_REASON_CN_FALLBACK.get(pt.reason, pt.reason))
        if pt.reason in ("cached", "cached_diluted"):
            hit_label = "命中" if pt.reason == "cached" else "稀释命中"
            if is_rev:
                tag = f"回溯·{hit_label}"
            elif is_cmp:
                tag = f"压缩·{hit_label}"
            elif is_acc_sw:
                tag = f"换号·{hit_label}"
            else:
                tag = base_tag
        else:
            tag = base_tag
        mod_s = truncate_display_width(pt.model or "未知模型", 14)
        disp_prefix = "选定" if prefix == "选定步" else ("最新" if prefix == "最新步" else prefix)

        line1 = (
            f"{disp_prefix} #{pt.step_index} {hm} │ [{mod_s}] │ 上下文 {format_tokens(pt.total_tokens)} "
            f"({format_delta_tokens(pt.context_delta)}) │ 缓存 {pt.hit_rate:.1f}% [{tag}] │ 首字 {t_s} │ {p_s}"
        )

        cr_s = format_tokens(int(getattr(pt, "cache_read_tokens", 0) or 0))
        in_s = format_tokens(int(getattr(pt, "input_tokens", 0) or 0))
        out_s = format_tokens(int(getattr(pt, "output_tokens", 0) or 0))
        strm_s = f" (流 {float(pt.streaming_sec):.1f}s)" if pt.streaming_sec is not None else ""
        m_cnt = int(getattr(pt, "active_media_count", 0) or 0)
        m_bytes = int(getattr(pt, "active_media_bytes", 0) or 0)
        m_seg = f" · 图{m_cnt}({format_media_size(m_bytes)})" if m_cnt > 0 else ""

        hint_seg = ""
        if bucket_count > 1:
            if locked:
                hint_seg = f" · 合并 {bucket_count} 步（←→ 逐拍 · Esc 解锁）"
            else:
                hint_seg = f" · 合并 {bucket_count} 步（左键锁定 / ←→ 逐拍）"
        elif locked:
            hint_seg = " · 已锁定（←→ 逐拍 · Esc 解锁）"

        line2 = f"构成: 缓存命中 {cr_s} · 新输入 {in_s} · 输出 {out_s}{strm_s}{m_seg}{hint_seg}"
        msg = f"{line1}\n{line2}"

        t = self.theme
        tier = resolve_cache_visual_tier(
            pt.classification,
            pt.reason,
            is_compact=is_cmp,
            is_revert=is_rev,
        )
        if tier == "green":
            fg = t["cyan"] if (is_acc_sw or pt.reason == "account_switch") else t["green"]
        else:
            fg = t[tier]
        self._lbl_readout.configure(text=msg, fg=fg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._load_gen += 1
        self._pending_reload = False
        self._pending_loaded_report = None
        was_dragging = self._dragging
        self._dragging = False
        self._pending_sync_snap = None
        self._locked_point_idx = None
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
        if was_dragging:
            self._notify_drag_state(False)
        for ch in self._charts.values():
            cv = ch.get("canvas")
            if cv is not None:
                try:
                    cv.delete("all")
                except tk.TclError:
                    pass
        self._charts.clear()
        self._kpi_cards.clear()
        self._range_btns.clear()
        self._act_btns.clear()
        self._report = None
        self._display_points.clear()
        self._display_buckets.clear()
        self._chart_meta.clear()
        self._current_wstats.clear()
        self._last_canvas_sizes.clear()
        self._last_sync_sig = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        cb = self._on_closed
        self._on_closed = None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        self._load_report = None  # type: ignore[assignment]
        self._on_toggle_pin = None  # type: ignore[assignment]
        self._is_pinned_fn = None  # type: ignore[assignment]
        self._apply_rounding_fn = None
        self._screen_bounds_fn = None
        self._on_drag_state_changed = None
