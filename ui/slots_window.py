"""On-demand themed modal inspecting Antigravity's local conversation pool & subagent cleanup."""

from __future__ import annotations

import datetime
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.log_monitor import format_relative_age, truncate_display_width
from core.slot_manager import (
    MIN_PROTECT_AGE_MIN,
    SlotCleanupResult,
    SlotConversationItem,
    SlotInventorySnapshot,
    format_bytes_human,
)
from ui.drag_proxy import LightweightProxyDragger, resolve_root_hwnd

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"
_VALID_SORT_MODES = ("time", "size", "steps")


class ConversationSlotsWindow:
    """Borderless themed Toplevel listing local main/subagent slots and safe one-click cleanup."""

    def __init__(
        self,
        parent: tk.Tk,
        theme: Dict[str, str],
        load_inventory: Callable[[], SlotInventorySnapshot],
        on_clean_now: Callable[[Callable[[SlotCleanupResult], None]], None],
        is_auto_clean_enabled: Callable[[], bool],
        on_toggle_auto_clean: Callable[[], None],
        get_protect_age_min: Callable[[], int],
        get_clean_mode: Callable[[], str],
        on_closed: Optional[Callable[[], None]] = None,
        on_drag_state_changed: Optional[Callable[[bool], None]] = None,
        apply_rounding_fn: Optional[Callable[[int], None]] = None,
        screen_bounds_fn: Optional[Callable[[tk.Tk], Tuple[int, int, int, int]]] = None,
        async_refresh: bool = False,
    ) -> None:
        self._parent = parent
        self.theme = theme
        self._load_inventory = load_inventory
        self._on_clean_now = on_clean_now
        self._is_auto_clean_enabled = is_auto_clean_enabled
        self._on_toggle_auto_clean = on_toggle_auto_clean
        self._get_protect_age_min = get_protect_age_min
        self._get_clean_mode = get_clean_mode
        self._on_closed = on_closed
        self._on_drag_state_changed = on_drag_state_changed
        self._apply_rounding_fn = apply_rounding_fn
        self._screen_bounds_fn = screen_bounds_fn
        self._async_refresh = bool(async_refresh)
        self._refresh_busy: bool = False
        self._pending_refresh: bool = False
        self._last_summary_sig: Optional[Tuple[Any, ...]] = None

        self._snapshot: Optional[SlotInventorySnapshot] = None
        self._active_tab: str = "cleanable_sub"  # "cleanable_sub" | "protected_sub" | "main" | "unknown"
        self._sort_mode: str = "time"  # "time" | "size" | "steps"
        self._search_query: str = ""
        self._search_after_id: Optional[str] = None
        self._search_trace_id: Optional[str] = None
        self._cleaning_busy: bool = False
        self._closed: bool = False
        self._after_ids: List[str] = []
        self._dragging: bool = False
        self._pending_sync_summary: Optional[SlotInventorySnapshot] = None
        self._pending_loaded_snapshot: Optional[SlotInventorySnapshot] = None
        self._scroll_drag_origin_y: int = 0
        self._scroll_drag_origin_top: float = 0.0

        self.win = tk.Toplevel(parent)
        self.win.title("本地会话分布与子代理清理")
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.bind("<Escape>", self._on_escape_key)
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
            title_provider=lambda: (
                f"{self._snapshot.cap} 槽位守护 · 本机对话分布"
                if (self._snapshot is not None and self._snapshot.cap > 0)
                else "本地会话清理 · 本机对话分布"
            ),
            header_height_provider=self._get_header_height,
            screen_bounds_fn=self._screen_bounds_fn,
        )

        self._outer = tk.Frame(self.win, padx=1, pady=1)
        self._outer.pack(fill="both", expand=True)
        self._surface = tk.Frame(self._outer, padx=12, pady=10)
        self._surface.pack(fill="both", expand=True)

        self._build_ui()
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
                max(840, int(self.win.winfo_reqwidth())),
                max(480, int(self.win.winfo_reqheight())),
            )
        except tk.TclError:
            return (840, 480)

    def _build_ui(self) -> None:
        # Header bar
        self._hdr = tk.Frame(self._surface)
        self._hdr.pack(fill="x", pady=(0, 8))

        self._btn_close = tk.Label(self._hdr, text="✕", font=(UI_FONT, 9, "bold"), padx=6, pady=1, cursor="hand2")
        self._btn_close.pack(side="right", padx=(6, 0))
        self._btn_close.bind("<Button-1>", lambda _e: self.close())

        self._btn_auto = tk.Label(self._hdr, text="自动清理:关", font=(UI_FONT, 8, "bold"), padx=7, pady=2, cursor="hand2")
        self._btn_auto.pack(side="right", padx=(6, 0))
        self._btn_auto.bind("<Button-1>", lambda _e: self._handle_toggle_auto())

        self._lbl_badge = tk.Label(self._hdr, text="本地会话清理", font=(UI_FONT, 8, "bold"), padx=6, pady=1)
        self._lbl_badge.pack(side="left", padx=(0, 6))
        self._lbl_title = tk.Label(self._hdr, text="本机对话分布", font=(UI_FONT, 10, "bold"), anchor="w")
        self._lbl_title.pack(side="left")
        self._lbl_sub = tk.Label(self._hdr, text="", font=(MONO_FONT, 8), anchor="w")
        self._lbl_sub.pack(side="left", padx=(8, 0))

        for w in (self._hdr, self._lbl_badge, self._lbl_title, self._lbl_sub):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)
            w.bind("<ButtonRelease-1>", self._on_drag_end)

        # Red/orange risk alert banner (packed dynamically when warn/danger/main_at_risk)
        self._risk_frame = tk.Frame(self._surface, padx=9, pady=6, highlightthickness=1)
        self._lbl_risk = tk.Label(self._risk_frame, text="", font=(UI_FONT, 8, "bold"), anchor="w", justify="left", wraplength=640)
        self._lbl_risk.pack(fill="x")

        # 4 KPI cards
        self._kpi_row = tk.Frame(self._surface)
        self._kpi_row.pack(fill="x", pady=(0, 8))
        self._kpi_cards: List[Dict[str, tk.Widget]] = []
        kpi_titles = ("本地会话总数 / 体积", "现存主对话 (永久保护)", "可清理老旧子代理", "保护期 / 未知会话")
        for i, title in enumerate(kpi_titles):
            self._kpi_row.grid_columnconfigure(i, weight=1, uniform="skpi")
            card = tk.Frame(self._kpi_row, padx=8, pady=6, highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0 if i == 3 else 4))
            lt = tk.Label(card, text=title, font=(UI_FONT, 8), anchor="w")
            lt.pack(fill="x")
            lv = tk.Label(card, text="—", font=(MONO_FONT, 11, "bold"), anchor="w")
            lv.pack(fill="x", pady=(2, 1))
            ls = tk.Label(card, text="—", font=(MONO_FONT, 8), anchor="w")
            ls.pack(fill="x")
            self._kpi_cards.append({"card": card, "title": lt, "val": lv, "sub": ls})

        # Tab bar + toolbar + fixed column header + scrollable table
        self._list_card = tk.Frame(self._surface, padx=8, pady=7, highlightthickness=1)
        self._list_card.pack(fill="both", expand=True, pady=(0, 8))

        self._tab_row = tk.Frame(self._list_card)
        self._tab_row.pack(fill="x", pady=(0, 5))
        self._tab_btns: Dict[str, tk.Label] = {}
        tab_specs = (
            ("cleanable_sub", "可清理子代理"),
            ("protected_sub", "保护中子代理"),
            ("main", "现存主对话"),
            ("unknown", "未知 .db"),
        )
        for key, label in tab_specs:
            b = tk.Label(self._tab_row, text=label, font=(UI_FONT, 8), padx=7, pady=2, cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda _e, k=key: self._set_tab(k))
            self._tab_btns[key] = b

        self._lbl_policy_hint = tk.Label(self._tab_row, text="", font=(UI_FONT, 7), anchor="e")
        self._lbl_policy_hint.pack(side="right")

        # Compact sort & search toolbar below tab bar
        self._tool_row = tk.Frame(self._list_card)
        self._tool_row.pack(fill="x", pady=(0, 5))

        self._lbl_sort_prefix = tk.Label(self._tool_row, text="排序:", font=(UI_FONT, 8), anchor="w")
        self._lbl_sort_prefix.pack(side="left", padx=(0, 4))
        self._sort_btns: Dict[str, tk.Label] = {}
        for skey, slbl in (("time", "时间"), ("size", "体积↓"), ("steps", "步数↓")):
            sb = tk.Label(self._tool_row, text=slbl, font=(UI_FONT, 8), padx=6, pady=1, cursor="hand2")
            sb.pack(side="left", padx=(0, 4))
            sb.bind("<Button-1>", lambda _e, m=skey: self._set_sort_mode(m))
            self._sort_btns[skey] = sb

        self._search_wrap = tk.Frame(self._tool_row, highlightthickness=1, padx=4, pady=1)
        self._search_wrap.pack(side="right")
        self._lbl_search_icon = tk.Label(self._search_wrap, text="🔍", font=(UI_FONT, 8))
        self._lbl_search_icon.pack(side="left", padx=(0, 3))
        self._search_var = tk.StringVar(master=self.win, value="")
        self._search_trace_id = self._search_var.trace_add("write", self._on_search_var_changed)
        self._search_entry = tk.Entry(
            self._search_wrap,
            textvariable=self._search_var,
            font=(UI_FONT, 8),
            width=24,
            bd=0,
            highlightthickness=0,
        )
        self._search_entry.pack(side="left")
        self._btn_clear_search = tk.Label(self._search_wrap, text="×", font=(UI_FONT, 9, "bold"), padx=4, cursor="hand2")
        self._btn_clear_search.pack(side="left", padx=(2, 0))
        self._btn_clear_search.bind("<Button-1>", lambda _e: self._clear_search())

        # Fixed table column header
        self._hdr_row = tk.Frame(self._list_card, pady=3)
        self._hdr_row.pack(fill="x", pady=(0, 2))
        self._hdr_row.grid_columnconfigure(1, weight=1)
        self._col_hdr_lbls: List[tk.Label] = []
        hdr_specs = (
            (0, "ID", "w", 8, (6, 6)),
            (1, "对话名称", "w", 0, (0, 8)),
            (2, "步数", "e", 6, (0, 8)),
            (3, "体积 · 最近活跃", "e", 19, (0, 8)),
            (4, "状态", "e", 18, (0, 17)),
        )
        for col_i, col_txt, anc, col_w, pdx in hdr_specs:
            kw: Dict[str, Any] = {"text": col_txt, "font": (UI_FONT, 7, "bold"), "anchor": anc}
            if col_w > 0:
                kw["width"] = col_w
            hl = tk.Label(self._hdr_row, **kw)
            hl.grid(row=0, column=col_i, sticky="w" if anc == "w" else "e", padx=pdx)
            self._col_hdr_lbls.append(hl)

        # Scrollable rows viewport
        self._vp_wrap = tk.Frame(self._list_card)
        self._vp_wrap.pack(fill="both", expand=True)
        self._viewport = tk.Canvas(self._vp_wrap, width=760, height=245, highlightthickness=0, bd=0)
        self._viewport.pack(side="left", fill="both", expand=True)
        self._scrollbar = tk.Canvas(self._vp_wrap, width=7, height=245, highlightthickness=0, bd=0, cursor="hand2")
        self._scrollbar.pack(side="right", fill="y", padx=(4, 0))
        self._scrollbar.bind("<ButtonPress-1>", self._on_scrollbar_press)
        self._scrollbar.bind("<B1-Motion>", self._on_scrollbar_drag)
        self._scrollbar.bind("<Configure>", lambda _e: self._sync_scrollbar())

        self._table = tk.Frame(self._viewport)
        self._table.grid_columnconfigure(1, weight=1)
        self._win_id = self._viewport.create_window((0, 0), window=self._table, anchor="nw")
        self._viewport.bind(
            "<Configure>",
            lambda e: (self._viewport.itemconfigure(self._win_id, width=e.width), self._sync_scrollbar()),
        )
        self._table.bind("<Configure>", lambda _e: self._update_scrollregion())
        for sc_w in (self._list_card, self._vp_wrap, self._viewport, self._table, self._scrollbar):
            sc_w.bind("<MouseWheel>", self._on_mousewheel)

        # Status strip
        self._status_box = tk.Frame(self._surface, padx=8, pady=5, highlightthickness=1)
        self._status_box.pack(fill="x", pady=(0, 8))
        self._lbl_status = tk.Label(
            self._status_box,
            text="纯只读盘点就绪 · 仅在显式触发或自动清理开启时安全清理超期已结束子代理",
            font=(UI_FONT, 8),
            anchor="w",
        )
        self._lbl_status.pack(fill="x")

        # Bottom action buttons
        self._act_row = tk.Frame(self._surface)
        self._act_row.pack(fill="x")
        self._act_btns: Dict[str, tk.Label] = {}
        actions = [
            ("clean", "🧹 一键清理 2h 外子代理", self._handle_clean_click),
            ("refresh", "刷新盘点", self.refresh),
            ("close", "关闭窗口", self.close),
        ]
        for idx, (k, txt, cmd) in enumerate(actions):
            self._act_row.grid_columnconfigure(idx, weight=2 if k == "clean" else 1, uniform="sact")
            btn = tk.Label(
                self._act_row,
                text=txt,
                font=(UI_FONT, 8, "bold" if k == "clean" else "normal"),
                padx=8,
                pady=5,
                cursor="hand2",
                highlightthickness=1,
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 4, 0 if idx == 2 else 4))
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
        self._lbl_badge.configure(bg=t["btn_hover"], fg=t["cyan"])
        self._lbl_title.configure(bg=t["panel_bg"], fg=t["text_primary"])
        self._lbl_sub.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._btn_close.configure(bg=t["panel_bg"], fg=t["text_secondary"])
        self._kpi_row.configure(bg=t["panel_bg"])
        for kc in self._kpi_cards:
            kc["card"].configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
            kc["title"].configure(bg=t["card_bg"], fg=t["text_secondary"])
            kc["val"].configure(bg=t["card_bg"], fg=t["text_primary"])
            kc["sub"].configure(bg=t["card_bg"], fg=t["text_muted"])
        self._list_card.configure(bg=t["card_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._tab_row.configure(bg=t["card_bg"])
        self._lbl_policy_hint.configure(bg=t["card_bg"], fg=t["text_muted"])
        self._tool_row.configure(bg=t["card_bg"])
        self._lbl_sort_prefix.configure(bg=t["card_bg"], fg=t["text_muted"])
        self._search_wrap.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"], highlightcolor=t["cyan"])
        self._lbl_search_icon.configure(bg=t["card_alt_bg"], fg=t["text_muted"])
        self._search_entry.configure(
            bg=t["card_alt_bg"],
            fg=t["text_primary"],
            insertbackground=t["cyan"],
            selectbackground=t["btn_active"],
            selectforeground=t["text_primary"],
        )
        self._btn_clear_search.configure(bg=t["card_alt_bg"], fg=t["text_muted"])
        self._hdr_row.configure(bg=t["card_bg"])
        for hl in self._col_hdr_lbls:
            hl.configure(bg=t["card_bg"], fg=t["text_muted"])
        self._vp_wrap.configure(bg=t["card_bg"])
        self._viewport.configure(bg=t["card_alt_bg"])
        self._scrollbar.configure(bg=t["card_bg"])
        self._table.configure(bg=t["card_alt_bg"])
        self._status_box.configure(bg=t["card_alt_bg"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._lbl_status.configure(bg=t["card_alt_bg"], fg=t["text_secondary"])
        self._act_row.configure(bg=t["panel_bg"])
        for btn in self._act_btns.values():
            btn.configure(bg=t["btn_bg"], fg=t["btn_text"], highlightbackground=t["border_soft"], highlightcolor=t["border_soft"])
        self._sync_auto_button()
        self._refresh_tab_buttons()
        self._refresh_sort_buttons()
        if self._snapshot is not None:
            self._render_snapshot()

    def show(self, place_near_parent: bool = True) -> None:
        if self._closed:
            return
        if self._dragger is not None:
            self._dragger.finish_or_cancel(commit=self._dragger.moved, force=True)
        self.win.deiconify()
        self.win.lift()
        self.refresh()
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
        w = max(840, int(self.win.winfo_reqwidth()))
        h = max(480, int(self.win.winfo_reqheight()))
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

    def _on_escape_key(self, _event: tk.Event) -> Optional[str]:
        if self._search_var.get().strip():
            self._clear_search()
            return "break"
        self.close()
        return "break"

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
        pending_loaded = self._pending_loaded_snapshot
        self._pending_loaded_snapshot = None
        pending_summary = self._pending_sync_summary
        self._pending_sync_summary = None
        if not self._closed:
            if pending_loaded is not None:
                self._apply_loaded_snapshot(pending_loaded)
            if pending_summary is not None:
                self.sync_from_summary(pending_summary)
        if was_dragging:
            self._notify_drag_state(False)

    def _on_dragger_ended(self, _moved: bool) -> None:
        self._flush_pending_sync()

    def _on_dragger_cancelled(self) -> None:
        self._flush_pending_sync()

    def check_drag_watchdog(self) -> None:
        if not self._closed and self._dragger is not None:
            self._dragger.check_release_watchdog()

    def _sync_auto_button(self) -> None:
        t = self.theme
        enabled = bool(self._is_auto_clean_enabled())
        if enabled:
            self._btn_auto.configure(text="自动清理:开", bg=t["green_bg"], fg=t["green"])
        else:
            self._btn_auto.configure(text="自动清理:关", bg=t["card_alt_bg"], fg=t["text_muted"])

    def _handle_toggle_auto(self) -> None:
        self._on_toggle_auto_clean()
        self._sync_auto_button()

    def _set_tab(self, tab_key: str) -> None:
        if tab_key not in self._tab_btns:
            return
        self._active_tab = tab_key
        self._viewport.yview_moveto(0.0)
        self._refresh_tab_buttons()
        self._render_table()

    def _set_sort_mode(self, mode: str) -> None:
        if mode not in _VALID_SORT_MODES:
            return
        self._sort_mode = mode
        self._viewport.yview_moveto(0.0)
        self._refresh_sort_buttons()
        self._render_table()

    def _refresh_sort_buttons(self) -> None:
        t = self.theme
        for k, btn in self._sort_btns.items():
            active = k == self._sort_mode
            btn.configure(
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )

    def _cancel_search_debounce(self) -> None:
        if self._search_after_id is not None:
            aid = self._search_after_id
            self._search_after_id = None
            try:
                self.win.after_cancel(aid)
            except tk.TclError:
                pass

    def _on_search_var_changed(self, *_args: Any) -> None:
        if self._closed:
            return
        self._cancel_search_debounce()
        try:
            self._search_after_id = self.win.after(120, self._apply_search_filter)
        except tk.TclError:
            self._search_after_id = None

    def _apply_search_filter(self) -> None:
        self._search_after_id = None
        if self._closed:
            return
        new_q = self._search_var.get().strip().lower()
        if new_q == self._search_query:
            return
        self._search_query = new_q
        self._viewport.yview_moveto(0.0)
        self._render_table()

    def _clear_search(self) -> None:
        if self._closed:
            return
        self._cancel_search_debounce()
        if self._search_var.get():
            self._search_var.set("")
            self._cancel_search_debounce()
        if self._search_query != "":
            self._search_query = ""
            self._viewport.yview_moveto(0.0)
            self._render_table()

    def _refresh_tab_buttons(self) -> None:
        t = self.theme
        snap = self._snapshot
        counts = {
            "cleanable_sub": snap.cleanable_sub_count if snap else 0,
            "protected_sub": snap.protected_sub_count if snap else 0,
            "main": snap.main_count if snap else 0,
            "unknown": snap.unknown_count if snap else 0,
        }
        names = {
            "cleanable_sub": "可清理子代理",
            "protected_sub": "保护中子代理",
            "main": "现存主对话",
            "unknown": "未知 .db",
        }
        for k, btn in self._tab_btns.items():
            active = k == self._active_tab
            btn.configure(
                text=f"{names[k]}({counts[k]})",
                bg=t["btn_hover"] if active else t["card_alt_bg"],
                fg=t["cyan"] if active else t["text_muted"],
                font=(UI_FONT, 8, "bold") if active else (UI_FONT, 8),
            )

    def sync_auto_button(self) -> None:
        if not self._closed:
            self._sync_auto_button()

    def set_status_message(self, text: str, color_key: str = "text_secondary") -> None:
        if self._closed:
            return
        self._lbl_status.configure(text=text, fg=self.theme.get(color_key, self.theme["text_secondary"]))

    def _apply_loaded_snapshot(self, inv: SlotInventorySnapshot) -> None:
        if self._closed:
            return
        if self._dragging:
            self._pending_loaded_snapshot = inv
            return
        self._snapshot = inv
        self._last_summary_sig = (
            inv.cap,
            inv.total_db,
            inv.total_bytes,
            inv.protect_age_min,
            inv.cleanable_sub_count,
            inv.cleanable_30m_count,
            inv.main_at_risk,
            inv.status,
            inv.last_auto_clean_summary,
        )
        # Default to main tab if nothing cleanable on first open so user sees concrete rows immediately
        if self._snapshot.cleanable_sub_count == 0 and self._active_tab == "cleanable_sub" and self._snapshot.main_count > 0:
            self._active_tab = "main"
        self._render_snapshot()

    def refresh(self, blocking: Optional[bool] = None) -> None:
        if self._closed:
            return
        loader = self._load_inventory
        if loader is None:
            return
        use_async = (not blocking) if blocking is not None else self._async_refresh
        if not use_async:
            self._apply_loaded_snapshot(loader())
            return
        if self._refresh_busy:
            self._pending_refresh = True
            return
        self._refresh_busy = True
        self._pending_refresh = False
        if self._snapshot is None:
            self.set_status_message("正在后台盘点本机对话目录与磁盘占用…", "text_secondary")

        def _bg_load() -> None:
            loaded: Optional[SlotInventorySnapshot] = None
            err = ""
            try:
                loaded = loader()
            except Exception as exc:
                err = str(exc)

            def _apply() -> None:
                self._refresh_busy = False
                if self._closed:
                    self._pending_refresh = False
                    return
                if loaded is not None:
                    self._apply_loaded_snapshot(loaded)
                elif err:
                    self.set_status_message(f"盘点失败: {err}", "red")
                if self._pending_refresh and not self._closed:
                    self._pending_refresh = False
                    self.refresh()

            try:
                if not self._closed:
                    self.win.after(0, _apply)
                else:
                    self._refresh_busy = False
                    self._pending_refresh = False
            except (tk.TclError, RuntimeError):
                self._refresh_busy = False
                self._pending_refresh = False

        threading.Thread(target=_bg_load, name="SlotsInventoryLoadThread", daemon=True).start()

    def sync_from_summary(self, inv: Optional[SlotInventorySnapshot]) -> None:
        if self._closed or inv is None:
            return
        if self._dragging:
            self._pending_sync_summary = inv
            return
        self._sync_auto_button()
        sig = (
            inv.cap,
            inv.total_db,
            inv.total_bytes,
            inv.protect_age_min,
            inv.cleanable_sub_count,
            inv.cleanable_30m_count,
            inv.main_at_risk,
            inv.status,
            inv.last_auto_clean_summary,
        )
        if self._snapshot is None or sig != self._last_summary_sig:
            self.refresh()

    def _render_snapshot(self) -> None:
        t = self.theme
        snap = self._snapshot
        if snap is None:
            return
        self._sync_auto_button()
        protect_min = self._get_protect_age_min()
        mode_map = {"recycle": "移入系统回收站", "archive": "先归档SHA-256校验再删", "delete": "直接删除"}
        mode_txt = mode_map.get(self._get_clean_mode(), "移入系统回收站")
        self._lbl_sub.configure(text=f"更新时间 {datetime.datetime.fromtimestamp(snap.observed_at).strftime('%H:%M:%S')}")
        self._lbl_policy_hint.configure(text=f"策略: {mode_txt} · 保护 {protect_min}m（硬下限 {MIN_PROTECT_AGE_MIN}m）")

        if snap.cap > 0 and snap.main_at_risk:
            self._risk_frame.configure(bg=t["red_bg"], highlightbackground=t["red"], highlightcolor=t["red"])
            self._lbl_risk.configure(bg=t["red_bg"], fg=t["red"], text=f"⚠️ {snap.risk_message}")
            self._risk_frame.pack(fill="x", pady=(0, 8), before=self._kpi_row)
        elif snap.cap > 0 and snap.status in ("danger", "warn") and snap.risk_message:
            bg_c = t["red_bg"] if snap.status == "danger" else t["orange_bg"]
            fg_c = t["red"] if snap.status == "danger" else t["orange"]
            self._risk_frame.configure(bg=bg_c, highlightbackground=fg_c, highlightcolor=fg_c)
            self._lbl_risk.configure(bg=bg_c, fg=fg_c, text=f"⚠️ {snap.risk_message}")
            self._risk_frame.pack(fill="x", pady=(0, 8), before=self._kpi_row)
        else:
            self._risk_frame.pack_forget()

        # KPI 1: Unlimited local history (cap <= 0) vs legacy capped slot pool (cap > 0)
        if snap.cap <= 0:
            self._lbl_badge.configure(text="本地会话清理")
            self._kpi_cards[0]["title"].configure(text="本地会话总数 / 体积")
            self._kpi_cards[0]["val"].configure(text=f"{snap.total_db} 条", fg=t["green"])
            self._kpi_cards[0]["sub"].configure(
                text=f"共 {format_bytes_human(snap.total_bytes)} · 无限历史",
                fg=t["text_secondary"],
            )
        else:
            self._lbl_badge.configure(text=f"{snap.cap} 槽位守护")
            self._kpi_cards[0]["title"].configure(text="总槽位占用 / 余量")
            pct = int(round((snap.total_db / max(1, snap.cap)) * 100))
            tot_col = t["red"] if snap.status in ("danger", "main_at_risk") else (t["orange"] if snap.status == "warn" else t["green"])
            self._kpi_cards[0]["val"].configure(text=f"{snap.total_db} / {snap.cap} ({pct}%)", fg=tot_col)
            self._kpi_cards[0]["sub"].configure(
                text=f"余 {snap.remaining_slots} 槽 · 基底 {snap.pinned_floor_count}",
                fg=t["text_secondary"],
            )

        # KPI 2: Main conversations
        self._kpi_cards[1]["val"].configure(text=f"{snap.main_count} 条", fg=t["cyan"])
        self._kpi_cards[1]["sub"].configure(text="永久保护 · 绝不清理", fg=t["text_secondary"])

        # KPI 3: Cleanable subagents
        c_col = t["orange"] if snap.cleanable_sub_count >= 50 else t["green"]
        self._kpi_cards[2]["val"].configure(text=f"{snap.cleanable_sub_count} 条", fg=c_col)
        self._kpi_cards[2]["sub"].configure(
            text=f"{format_bytes_human(snap.cleanable_bytes)} · 30m可清 {snap.cleanable_30m_count}",
            fg=t["text_secondary"],
        )

        # KPI 4: Protected / unknown
        p_col = t["orange"] if snap.unknown_count > 0 else t["text_primary"]
        self._kpi_cards[3]["val"].configure(text=f"{snap.protected_sub_count + snap.unknown_count} 条", fg=p_col)
        self._kpi_cards[3]["sub"].configure(
            text=f"保护 {snap.protected_sub_count} · 未知 {snap.unknown_count}",
            fg=t["text_secondary"],
        )

        age_lbl = f"{protect_min // 60}h" if protect_min % 60 == 0 else f"{protect_min}m"
        if not self._cleaning_busy:
            self._act_btns["clean"].configure(
                text=f"🧹 一键清理 {age_lbl} 外子代理 ({snap.cleanable_sub_count} 条)",
                fg=t["cyan"] if snap.cleanable_sub_count > 0 else t["text_muted"],
            )
            cur_st = str(self._lbl_status.cget("text") or "")
            if (not cur_st) or cur_st.startswith("正在后台盘点") or cur_st.startswith("提示："):
                if snap.last_auto_clean_summary:
                    self._lbl_status.configure(text=f"最近清理：{snap.last_auto_clean_summary}", fg=t["text_secondary"])
                else:
                    self._lbl_status.configure(
                        text=f"提示：当前方式为「{mode_txt}」，仅清理超保护期已结束子代理，主对话与 30m 硬下限绝对保护",
                        fg=t["text_secondary"],
                    )
        self._refresh_tab_buttons()
        self._refresh_sort_buttons()
        self._render_table()

    def _get_filtered_sorted_rows(self) -> List[SlotConversationItem]:
        snap = self._snapshot
        if snap is None:
            return []
        rows = [it for it in snap.items if it.category == self._active_tab]
        q = self._search_query
        if q:
            rows = [
                it
                for it in rows
                if (
                    q in (it.title or "").lower()
                    or q in (it.short_id or "").lower()
                    or q in (it.agent_name or "").lower()
                    or q in (it.conversation_id or "").lower()
                )
            ]
        if self._sort_mode == "size":
            rows.sort(key=lambda x: (int(x.disk_bytes), float(x.last_active_ts)), reverse=True)
        elif self._sort_mode == "steps":
            rows.sort(key=lambda x: (int(x.step_count), int(x.disk_bytes)), reverse=True)
        else:
            if self._active_tab == "cleanable_sub":
                rows.sort(key=lambda x: x.last_active_ts)
            else:
                rows.sort(key=lambda x: x.last_active_ts, reverse=True)
        return rows

    def _render_table(self) -> None:
        t = self.theme
        for child in self._table.winfo_children():
            child.destroy()
        snap = self._snapshot
        if snap is None:
            return
        rows = self._get_filtered_sorted_rows()
        if not rows:
            if self._search_query:
                empty_txt = f"未找到匹配「{self._search_query}」的对话，可点击 × 清空搜索条件"
            else:
                clean_empty = (
                    "当前没有超过保护期的已结束子代理，本地目录很干净"
                    if snap.cap <= 0
                    else "当前没有超过保护期的已结束子代理，槽位很干净"
                )
                empty_txt = {
                    "cleanable_sub": clean_empty,
                    "protected_sub": "当前没有运行中或保护期内的子代理",
                    "main": "当前未检测到本地主对话 .db 文件",
                    "unknown": "无未登记的未知 .db 文件（全部已在摘要库核验）",
                }.get(self._active_tab, "暂无记录")
            lbl = tk.Label(self._table, text=empty_txt, font=(UI_FONT, 8), bg=t["card_alt_bg"], fg=t["text_muted"], padx=8, pady=12, anchor="w")
            lbl.grid(row=0, column=0, columnspan=5, sticky="w")
            self._update_scrollregion()
            return

        shown = rows[:160]
        now = snap.observed_at
        for idx, it in enumerate(shown):
            if it.title:
                name = truncate_display_width(it.title, 26)
            elif it.agent_name:
                name = f"[{truncate_display_width(it.agent_name, 16)}]"
            else:
                name = "未命名子代理" if it.category != "main" else "未命名主对话"
            age_s = format_relative_age(max(0.0, now - it.last_active_ts))
            sz_s = format_bytes_human(it.disk_bytes)
            st_s = truncate_display_width(it.protect_reason, 22)
            st_col = (
                t["green"]
                if it.category == "cleanable_sub"
                else (t["cyan"] if it.category == "main" else (t["orange"] if it.category == "unknown" else t["text_secondary"]))
            )
            bg = t["card_alt_bg"]
            tk.Label(self._table, text=it.short_id, font=(MONO_FONT, 8, "bold"), bg=bg, fg=t["text_primary"], anchor="w", width=8).grid(row=idx, column=0, sticky="w", padx=(6, 6), pady=2)
            tk.Label(self._table, text=name, font=(UI_FONT, 8), bg=bg, fg=t["text_primary"], anchor="w").grid(row=idx, column=1, sticky="w", padx=(0, 8), pady=2)
            tk.Label(self._table, text=f"{it.step_count}步" if it.step_count > 0 else "—", font=(MONO_FONT, 8), bg=bg, fg=t["text_muted"], anchor="e", width=6).grid(row=idx, column=2, sticky="e", padx=(0, 8), pady=2)
            tk.Label(self._table, text=f"{sz_s:>8} · {age_s}", font=(MONO_FONT, 8), bg=bg, fg=t["text_secondary"], anchor="e", width=19).grid(row=idx, column=3, sticky="e", padx=(0, 8), pady=2)
            tk.Label(self._table, text=st_s, font=(UI_FONT, 8), bg=bg, fg=st_col, anchor="e", width=18).grid(row=idx, column=4, sticky="e", padx=(0, 6), pady=2)

        if len(rows) > len(shown):
            tk.Label(
                self._table,
                text=f"… 其余 {len(rows) - len(shown)} 条已省略显示（清理时会按保护规则全部处理）",
                font=(UI_FONT, 7),
                bg=t["card_alt_bg"],
                fg=t["text_muted"],
                anchor="w",
            ).grid(row=len(shown), column=0, columnspan=5, sticky="w", padx=6, pady=4)

        for child in self._table.winfo_children():
            child.bind("<MouseWheel>", self._on_mousewheel)
        self._update_scrollregion()

    def _format_cleanup_done_message(self, res: SlotCleanupResult) -> Tuple[str, str]:
        if res.error:
            return (f"清理中止: {res.error}", "red")
        skip_parts: List[str] = []
        rec_unavail = int(getattr(res, "skipped_recycle_unavailable_count", 0) or 0)
        if rec_unavail > 0:
            skip_parts.append(f"回收站不可用跳过 {rec_unavail} 条（可在设置切换为归档模式）")
        if res.skipped_locked_count:
            skip_parts.append(f"文件占用跳过 {res.skipped_locked_count} 条")
        if res.skipped_changed_count:
            skip_parts.append(f"变动跳过 {res.skipped_changed_count} 条")
        skip_part = f"（{'；'.join(skip_parts)}）" if skip_parts else ""
        msg = (
            f"已完成清理：移出 {res.removed_count} 条子代理，释放 {format_bytes_human(res.freed_bytes)}，"
            f"当前剩余 {res.remaining_db_count} 条会话{skip_part}"
        )
        col_key = "orange" if (rec_unavail > 0 and res.removed_count == 0) else "green"
        return (msg, col_key)

    def _handle_clean_click(self) -> None:
        if self._closed or self._cleaning_busy:
            return
        snap = self._snapshot
        if snap is None or snap.cleanable_sub_count <= 0:
            self._lbl_status.configure(text="当前没有满足条件的老旧子代理需要清理", fg=self.theme["text_secondary"])
            return
        protect_min = self._get_protect_age_min()
        mode_map = {"recycle": "移入 Windows 系统回收站（可随时还原）", "archive": "先打 tar + SHA-256 复验再删除", "delete": "直接删除"}
        mode_desc = mode_map.get(self._get_clean_mode(), "移入 Windows 系统回收站")
        ok = messagebox.askyesno(
            "确认清理历史子代理",
            (
                f"准备安全清理 {snap.cleanable_sub_count} 条已结束老旧子代理（超过 {protect_min} 分钟，约 {format_bytes_human(snap.cleanable_bytes)}）。\n\n"
                f"• 清理方式：{mode_desc}\n"
                f"• 永久保护：{snap.main_count} 条主对话、{snap.protected_sub_count} 条保护中子代理、{snap.unknown_count} 条未知文件（0 受影响）\n"
                f"• 遇进程句柄占用将自动跳过，绝不强杀 Antigravity\n\n"
                "确认立即执行吗？"
            ),
            parent=self.win,
        )
        if not ok or self._closed:
            return
        self._cleaning_busy = True
        self._act_btns["clean"].configure(text="清理中，请稍候…", fg=self.theme["orange"])
        self._lbl_status.configure(text="正在二次核验并安全移出老旧子代理…", fg=self.theme["orange"])

        def _on_done(res: SlotCleanupResult) -> None:
            if self._closed:
                return
            self._cleaning_busy = False
            msg, col_key = self._format_cleanup_done_message(res)
            self._lbl_status.configure(text=msg, fg=self.theme.get(col_key, self.theme["green"]))
            self.refresh()

        self._on_clean_now(_on_done)

    def _update_scrollregion(self) -> None:
        try:
            bbox = self._viewport.bbox("all")
            if bbox:
                self._viewport.configure(scrollregion=bbox)
        except tk.TclError:
            pass
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        sb = self._scrollbar
        t = self.theme
        try:
            sb.delete("all")
            h = max(16, sb.winfo_height())
            w = max(6, sb.winfo_width())
            first, last = self._viewport.yview()
            if last - first >= 0.995:
                return
            sb.create_rectangle(1, 2, w - 1, h - 2, fill=t["bar_track"], outline="")
            y0 = max(2, int(round(first * (h - 4))) + 2)
            y1 = min(h - 2, max(y0 + 18, int(round(last * (h - 4))) + 2))
            sb.create_rectangle(1, y0, w - 1, y1, fill=t["cyan"], outline="")
        except tk.TclError:
            pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        first, last = self._viewport.yview()
        if last - first >= 0.995:
            return
        step = -1 if event.delta > 0 else 1
        self._viewport.yview_scroll(step, "units")
        self._sync_scrollbar()

    def _on_scrollbar_press(self, event: tk.Event) -> None:
        first, last = self._viewport.yview()
        if last - first >= 0.995:
            return
        self._scroll_drag_origin_y = event.y
        self._scroll_drag_origin_top = first
        h = max(16, self._scrollbar.winfo_height())
        thumb = max(0.05, last - first)
        target = max(0.0, min(1.0 - thumb, (event.y / float(h)) - thumb / 2.0))
        self._viewport.yview_moveto(target)
        self._sync_scrollbar()

    def _on_scrollbar_drag(self, event: tk.Event) -> None:
        first, last = self._viewport.yview()
        if last - first >= 0.995:
            return
        h = max(16, self._scrollbar.winfo_height())
        dy = event.y - self._scroll_drag_origin_y
        target = max(0.0, min(1.0 - (last - first), self._scroll_drag_origin_top + dy / float(h)))
        self._viewport.yview_moveto(target)
        self._sync_scrollbar()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        was_dragging = self._dragging
        self._dragging = False
        self._snapshot = None
        self._pending_sync_summary = None
        self._pending_loaded_snapshot = None
        self._last_summary_sig = None
        self._cancel_search_debounce()
        if self._search_trace_id is not None:
            try:
                self._search_var.trace_remove("write", self._search_trace_id)
            except tk.TclError:
                pass
            self._search_trace_id = None
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
        try:
            for child in self._table.winfo_children():
                child.destroy()
        except tk.TclError:
            pass
        for cv in (self._viewport, self._scrollbar):
            try:
                cv.delete("all")
            except tk.TclError:
                pass
        self._kpi_cards.clear()
        self._tab_btns.clear()
        self._sort_btns.clear()
        self._col_hdr_lbls.clear()
        self._act_btns.clear()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        cb = self._on_closed
        self._on_closed = None
        self._load_inventory = None  # type: ignore[assignment]
        self._on_clean_now = None  # type: ignore[assignment]
        self._is_auto_clean_enabled = None  # type: ignore[assignment]
        self._on_toggle_auto_clean = None  # type: ignore[assignment]
        self._get_protect_age_min = None  # type: ignore[assignment]
        self._get_clean_mode = None  # type: ignore[assignment]
        self._apply_rounding_fn = None
        self._screen_bounds_fn = None
        self._on_drag_state_changed = None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
