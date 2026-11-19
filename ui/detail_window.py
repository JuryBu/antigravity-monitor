"""On-demand long-range single-conversation diagnostics modal (Context, TTFT, Cache)."""

from __future__ import annotations

import datetime
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.log_monitor import (
    ConversationDetailPoint,
    ConversationDetailReport,
    format_countdown,
    format_delta_tokens,
    format_detail_text_report,
    format_tokens,
)

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


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
    ) -> None:
        self._parent = parent
        self.theme: Dict[str, str] = theme
        self._load_report = load_report
        self._on_toggle_pin = on_toggle_pin
        self._is_pinned_fn = is_pinned_fn
        self._on_closed = on_closed
        self._apply_rounding_fn = apply_rounding_fn
        self._screen_bounds_fn = screen_bounds_fn

        self._conv_id: str = ""
        self._report: Optional[ConversationDetailReport] = None
        self._window_mode: str = "60"  # "60" | "all"
        self._display_points: List[ConversationDetailPoint] = []
        self._chart_meta: Dict[str, Dict[str, Any]] = {}
        self._drag_ox: int = 0
        self._drag_oy: int = 0
        self._win_ox: int = 0
        self._win_oy: int = 0
        self._closed: bool = False
        self._after_ids: List[str] = []

        self.win = tk.Toplevel(parent)
        self.win.title("单对话长线体检")
        self.win.overrideredirect(True)
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

    def _build_ui(self) -> None:
        # Header bar (draggable)
        self._hdr = tk.Frame(self._surface)
        self._hdr.pack(fill="x", pady=(0, 8))

        self._lbl_role = tk.Label(self._hdr, text="主对话", font=(UI_FONT, 8, "bold"), padx=6, pady=1)
        self._lbl_role.pack(side="left", padx=(0, 6))
        self._lbl_title = tk.Label(self._hdr, text="—", font=(UI_FONT, 10, "bold"), anchor="w")
        self._lbl_title.pack(side="left")
        self._lbl_sub = tk.Label(self._hdr, text="", font=(MONO_FONT, 8), anchor="w")
        self._lbl_sub.pack(side="left", padx=(8, 0))

        self._btn_close = tk.Label(self._hdr, text="✕", font=(UI_FONT, 9, "bold"), padx=6, pady=1, cursor="hand2")
        self._btn_close.pack(side="right", padx=(6, 0))
        self._btn_close.bind("<Button-1>", lambda _e: self.close())

        self._range_bar = tk.Frame(self._hdr)
        self._range_bar.pack(side="right")
        self._range_btns: Dict[str, tk.Label] = {}
        for mode, label in (("60", "近 60 步"), ("all", "全部步数")):
            b = tk.Label(self._range_bar, text=label, font=(UI_FONT, 8), padx=7, pady=2, cursor="hand2")
            b.pack(side="left", padx=(4, 0))
            b.bind("<Button-1>", lambda _e, m=mode: self._set_window_mode(m))
            self._range_btns[mode] = b

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
            ("ctx", "请求体 / 上下文增长曲线", "青线=上下文规模 · 紫虚线=压缩裁剪"),
            ("ttft", "首字延迟 TTFT 长线趋势", "绿<15s · 青15-45s · 橙45-90s · 红≥90s"),
            ("cache", "缓存命中率长线轨迹", "绿命中 · 紫压缩重热 · 橙正常冷启 · 红异常掉缓存"),
        ]
        self._charts: Dict[str, Dict[str, Any]] = {}
        chart_heights = {"ctx": 116, "ttft": 100, "cache": 82}
        for key, title, legend in self._chart_specs:
            frame = tk.Frame(self._surface, padx=8, pady=6, highlightthickness=1)
            frame.pack(fill="x", pady=(0, 7))
            top = tk.Frame(frame)
            top.pack(fill="x", pady=(0, 3))
            lt = tk.Label(top, text=title, font=(UI_FONT, 8, "bold"), anchor="w")
            lt.pack(side="left")
            ll = tk.Label(top, text=legend, font=(UI_FONT, 8), anchor="e")
            ll.pack(side="right")
            cv = tk.Canvas(frame, width=640, height=chart_heights[key], highlightthickness=0, bd=0, cursor="crosshair")
            cv.pack(fill="x")
            cv.bind("<Motion>", lambda e, k=key: self._on_chart_hover(k, e))
            cv.bind("<Leave>", lambda _e: self._clear_chart_crosshair())
            cv.bind("<Configure>", lambda _e, k=key: self._on_chart_configure(k))
            self._charts[key] = {"frame": frame, "top": top, "title": lt, "legend": ll, "canvas": cv}

        # Live crosshair readout strip
        self._readout_box = tk.Frame(self._surface, padx=8, pady=5, highlightthickness=1)
        self._readout_box.pack(fill="x", pady=(0, 8))
        self._lbl_readout = tk.Label(
            self._readout_box,
            text="移动鼠标到任意图表上可精准查看对应步号的上下文变化、TTFT、吞吐与缓存原因",
            font=(MONO_FONT, 8),
            anchor="w",
        )
        self._lbl_readout.pack(fill="x")

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

    def show_conversation(self, conv_id: str, place_near_parent: bool = True) -> None:
        if self._closed:
            return
        self._conv_id = conv_id
        self.win.deiconify()
        self.win.lift()
        self.win.update_idletasks()
        self.refresh()
        self.win.update_idletasks()
        if place_near_parent:
            self._place_window()
        if self._apply_rounding_fn is not None:
            try:
                self._apply_rounding_fn(int(self.win.winfo_id()))
            except Exception:
                pass

    def _on_chart_configure(self, chart_key: str) -> None:
        if self._closed or not self._display_points:
            return
        if chart_key == "ctx":
            self._draw_context_chart(self._display_points)
        elif chart_key == "ttft":
            self._draw_ttft_chart(self._display_points)
        elif chart_key == "cache":
            self._draw_cache_chart(self._display_points)

    def _place_window(self) -> None:
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
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
        self.win.geometry(f"+{x}+{y}")

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_ox = event.x_root
        self._drag_oy = event.y_root
        self._win_ox = self.win.winfo_x()
        self._win_oy = self.win.winfo_y()

    def _on_drag_move(self, event: tk.Event) -> None:
        nx = self._win_ox + (event.x_root - self._drag_ox)
        ny = self._win_oy + (event.y_root - self._drag_oy)
        self.win.geometry(f"+{nx}+{ny}")

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
            self._lbl_title.configure(text=f"{self._conv_id[:6]} · 未找到轨迹文件")
            self._lbl_sub.configure(text="")
            for ch in self._charts.values():
                ch["canvas"].delete("all")
            return

        if rep.is_subagent:
            self._lbl_role.configure(text="子代理", bg=t["card_alt_bg"], fg=t["text_secondary"])
        else:
            self._lbl_role.configure(text="主对话", bg=t["green_bg"], fg=t["green"])
        self._lbl_title.configure(text=f"{rep.short_id} · {rep.model}")
        self._lbl_sub.configure(text=f"总计 {rep.total_steps} 步 (今日 {rep.today_steps} 步)")

        pts = rep.points[-60:] if (self._window_mode == "60" and len(rep.points) > 60) else list(rep.points)
        self._display_points = pts

        # KPI 1: Context size & delta
        delta_s = format_delta_tokens(rep.latest_context_delta)
        delta_col = t["purple"] if rep.latest_context_delta <= -32000 else t["cyan"]
        self._kpi_cards[0]["val"].configure(text=f"{format_tokens(rep.latest_context_tokens)} ({delta_s})", fg=delta_col)
        self._kpi_cards[0]["sub"].configure(
            text=f"峰值 {format_tokens(rep.peak_context_tokens)} · 压缩 {rep.compact_count} 次",
            fg=t["text_secondary"],
        )

        # KPI 2: Cache hit ratio
        if rep.overall_cacheable_hit_ratio is not None:
            pct = rep.overall_cacheable_hit_ratio * 100.0
            c_col = t["green"] if pct >= 85 else (t["orange"] if pct >= 55 else t["red"])
            self._kpi_cards[1]["val"].configure(text=f"{pct:.1f}%", fg=c_col)
        else:
            self._kpi_cards[1]["val"].configure(text="—", fg=t["text_muted"])
        self._kpi_cards[1]["sub"].configure(
            text=f"命中 {rep.cache_hit_steps} · 异常 {rep.abnormal_miss_steps} · 冷 {rep.expected_cold_steps}",
            fg=t["red"] if rep.abnormal_miss_steps > 0 else t["text_secondary"],
        )

        # KPI 3: TTFT
        if rep.avg_ttft_sec is not None and rep.max_ttft_sec is not None:
            t_col = t["red"] if rep.max_ttft_sec >= 90 else (t["orange"] if rep.avg_ttft_sec >= 45 else t["green"])
            self._kpi_cards[2]["val"].configure(text=f"均 {rep.avg_ttft_sec:.1f}s", fg=t_col)
            p95_s = f"{rep.p95_ttft_sec:.1f}s" if rep.p95_ttft_sec is not None else "—"
            self._kpi_cards[2]["sub"].configure(text=f"P95 {p95_s} · 峰 {rep.max_ttft_sec:.1f}s", fg=t["text_secondary"])
        else:
            self._kpi_cards[2]["val"].configure(text="—", fg=t["text_muted"])
            self._kpi_cards[2]["sub"].configure(text="暂无 SQLite 速度记录", fg=t["text_muted"])

        # KPI 4: TPS & Freshness
        if rep.avg_tps is not None:
            self._kpi_cards[3]["val"].configure(text=f"均 {rep.avg_tps:.0f} t/s", fg=t["cyan"])
        else:
            self._kpi_cards[3]["val"].configure(text="— t/s", fg=t["text_muted"])
        if rep.freshness_invalid_reason:
            f_txt, f_col = rep.freshness_invalid_reason, t["red"]
        elif rep.freshness_remaining_sec is not None:
            if rep.freshness_remaining_sec <= 0:
                f_txt, f_col = "大上下文保鲜: 已过期", t["orange"]
            else:
                f_txt = f"保鲜剩 {format_countdown(rep.freshness_remaining_sec)}"
                f_col = t["green"] if rep.freshness_remaining_sec > 60 else t["orange"]
        else:
            latest_p = f"新 {rep.latest_tps:.0f} t/s" if rep.latest_tps is not None else "常规上下文"
            f_txt, f_col = latest_p, t["text_secondary"]
        self._kpi_cards[3]["sub"].configure(text=f_txt, fg=f_col)

        self.win.update_idletasks()
        self._draw_context_chart(pts)
        self._draw_ttft_chart(pts)
        self._draw_cache_chart(pts)

        if pts:
            self._update_readout_for_point(pts[-1], prefix="最新步")

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
        t0 = datetime.datetime.fromtimestamp(first.timestamp).strftime("%H:%M") if first.timestamp > 0 else ""
        t1 = datetime.datetime.fromtimestamp(last.timestamp).strftime("%H:%M") if last.timestamp > 0 else ""
        cv.create_text(pad_l, y_lbl, text=f"#{first.step_index} {t0}".strip(), anchor="w", font=(MONO_FONT, 7), fill=t["text_muted"])
        if len(pts) >= 3:
            mid = pts[len(pts) // 2]
            tm = datetime.datetime.fromtimestamp(mid.timestamp).strftime("%H:%M") if mid.timestamp > 0 else ""
            cv.create_text(pad_l + plot_w / 2.0, y_lbl, text=f"#{mid.step_index} {tm}".strip(), anchor="center", font=(MONO_FONT, 7), fill=t["text_muted"])
        cv.create_text(pad_l + plot_w, y_lbl, text=f"#{last.step_index} {t1}".strip(), anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

    def _draw_context_chart(self, pts: List[ConversationDetailPoint]) -> None:
        t = self.theme
        cv = self._charts["ctx"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["ctx"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        if not pts:
            cv.create_text(w / 2, h / 2, text="暂无上下文轨迹数据", font=(UI_FONT, 8), fill=t["text_muted"])
            return

        peak = max(p.total_tokens for p in pts)
        y_max = max(64_000, int(peak * 1.12))

        for frac in (0.0, 0.5, 1.0):
            yy = pad_t + plot_h * (1.0 - frac)
            cv.create_line(pad_l, yy, pad_l + plot_w, yy, fill=t["divider"], dash=(2, 3))
            cv.create_text(pad_l - 6, yy, text=format_tokens(int(y_max * frac)), anchor="e", font=(MONO_FONT, 7), fill=t["text_muted"])

        # Highlight compaction drops with purple dashed verticals (space out labels to avoid overlap)
        n = len(pts)
        coords: List[float] = []
        last_lbl_x = -999.0
        for idx, pt in enumerate(pts):
            x = self._x_for_idx(idx, n, pad_l, plot_w)
            y = pad_t + plot_h * (1.0 - min(1.0, pt.total_tokens / float(y_max)))
            coords.extend([x, y])
            if pt.reason == "compact_reset":
                cv.create_line(x, pad_t, x, pad_t + plot_h, fill=t["purple"], width=1.2, dash=(3, 2))
                if x - last_lbl_x >= 34.0 and x < pad_l + plot_w - 22.0:
                    cv.create_text(x + 2, pad_t + 7, text="压缩", anchor="w", font=(UI_FONT, 7, "bold"), fill=t["purple"])
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
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        self._chart_meta["ttft"] = {"pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h}

        valid_vals = [p.ttft_sec for p in pts if p.ttft_sec is not None]
        if not valid_vals:
            cv.create_text(w / 2, h / 2, text="当前范围无 SQLite 首字延迟记录", font=(UI_FONT, 8), fill=t["text_muted"])
            return

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
            col = t["red"] if sec >= 90.0 else (t["orange"] if sec >= 45.0 else (t["cyan"] if sec >= 15.0 else t["green"]))
            cv.create_rectangle(x - bar_w / 2.0, yy, x + bar_w / 2.0, y_base, fill=col, outline="")
            if clipped:
                cv.create_rectangle(x - bar_w / 2.0, pad_t, x + bar_w / 2.0, pad_t + 2.5, fill=t["text_primary"], outline="")

        self._draw_axis_footer(cv, pts, pad_l, plot_w, h)

    def _draw_cache_chart(self, pts: List[ConversationDetailPoint]) -> None:
        t = self.theme
        cv = self._charts["cache"]["canvas"]
        cv.delete("all")
        w, h, pad_l, pad_r, pad_t, pad_b = self._chart_geometry(cv)
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
        for ch in self._charts.values():
            try:
                ch["canvas"].delete("crosshair")
            except tk.TclError:
                pass
        if self._display_points:
            self._update_readout_for_point(self._display_points[-1], prefix="最新步")

    def _update_readout_for_point(self, pt: ConversationDetailPoint, prefix: str = "选定步") -> None:
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
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%H:%M:%S") if pt.timestamp > 0 else "--:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f} t/s" if pt.tps is not None else "—"
        tag = reason_map.get(pt.reason, pt.reason)
        msg = (
            f"{prefix} #{pt.step_index} ({hm}) │ 请求体 {format_tokens(pt.total_tokens)} "
            f"({format_delta_tokens(pt.context_delta)}) │ 缓存 {pt.hit_rate:.1f}% [{tag}] │ 首字 {t_s} │ {p_s}"
        )
        t = self.theme
        fg = (
            t["red"]
            if pt.classification == "abnormal_miss"
            else (t["purple"] if pt.reason == "compact_reset" else (t["orange"] if pt.classification == "expected_cold" else t["text_primary"]))
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
