"""Ultra-lightweight 2-HWND drag proxy shell for zero-lag Win32 native dragging of heavy Tk modals.

Windows 11 / 10 DWM and high-polling-rate mice (500-1000Hz) incur per-move cost proportional to
both visible Win32 child HWND count and window pixel area inside Win32's native modal drag loop
(``WM_SYSCOMMAND`` + ``SC_MOVE | HTCAPTION``). Heavy modals (settings: 68 HWNDs, detail: 53 HWNDs,
slots: 550+ HWNDs) cross the 1.0ms/packet threshold and back up the input queue.

``LightweightProxyDragger`` keeps the modal as a single unified window at rest (zero Z-order split,
zero form/focus breakage), and the instant a live Win32 drag crosses ``move_threshold`` it swaps in
a pre-warmed 2-HWND themed header bar proxy (``~42px`` tall, no ``WS_EX_LAYERED``, no inputs/IME)
to run the OS kernel modal drag loop (~0.65ms/move — faster than the 18-HWND main capsule bar).
On release, the real modal is clamped to visible screen bounds and restored in one frame (<8ms).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import tkinter as tk
from typing import Any, Callable, Dict, Optional, Tuple

from ui.window_drag import (
    GA_ROOT,
    SC_MOVE_HTCAPTION,
    SWP_MOVE_FLAGS,
    WM_CANCELMODE,
    WM_SYSCOMMAND,
    _FALLBACK_CHASE_INTERVAL_MS,
    _NATIVE_POLL_INTERVAL_MS,
    _WIN32_CURSOR_MATCH_TOLERANCE_PX,
    _get_user32,
    _is_hwnd_in_move_size_loop,
    _is_primary_button_pressed,
    _pack_screen_lparam,
)

UI_FONT = "Microsoft YaHei UI"
_PROXY_NATIVE_HANDOFF_GRACE_SEC = 0.12


def resolve_root_hwnd(win: tk.Misc) -> int:
    """Resolve the real Win32 top-level root HWND (TkTopLevel) for DWM attributes and Win32 calls."""
    if os.name != "nt" or win is None:
        return 0
    try:
        wid = int(win.winfo_id())
    except (tk.TclError, ValueError, TypeError):
        return 0
    if not wid:
        return 0
    u32 = _get_user32()
    if u32 is None:
        return wid
    try:
        root_hwnd = int(u32.GetAncestor(wintypes.HWND(wid), wintypes.UINT(GA_ROOT)) or 0)
        if root_hwnd and bool(u32.IsWindow(wintypes.HWND(root_hwnd))):
            return root_hwnd
    except Exception:
        pass
    return wid


class LightweightProxyDragger:
    """Drop-in SmoothWindowDragger-compatible dragger using a 2-HWND native proxy bar during live drags."""

    def __init__(
        self,
        win: tk.Misc,
        move_threshold: int = 3,
        on_drag_start: Optional[Callable[[], None]] = None,
        on_drag_move_start: Optional[Callable[[], None]] = None,
        on_drag_end: Optional[Callable[[bool], None]] = None,
        on_drag_cancel: Optional[Callable[[], None]] = None,
        size_provider: Optional[Callable[[], Tuple[int, int]]] = None,
        theme_provider: Optional[Callable[[], Dict[str, str]]] = None,
        title_provider: Optional[Callable[[], str]] = None,
        header_height_provider: Optional[Callable[[], int]] = None,
        screen_bounds_fn: Optional[Callable[[tk.Misc], Tuple[int, int, int, int]]] = None,
        flush_interval_ms: int = 8,
    ) -> None:
        self._win = win
        self._move_threshold: int = max(0, int(move_threshold))
        self._on_drag_start = on_drag_start
        self._on_drag_move_start = on_drag_move_start
        self._on_drag_end = on_drag_end
        self._on_drag_cancel = on_drag_cancel
        self._size_provider = size_provider
        self._theme_provider = theme_provider
        self._title_provider = title_provider
        self._header_height_provider = header_height_provider
        self._screen_bounds_fn = screen_bounds_fn
        self._flush_interval_ms: int = max(1, int(flush_interval_ms))
        self._min_win32_interval_sec: float = 0.004

        self._active: bool = False
        self._moved: bool = False
        self._hwnd: int = 0
        self._use_win32_cursor: bool = False

        self._native_drag_active: bool = False
        self._native_drag_seen_loop: bool = False
        self._native_drag_failed: bool = False
        self._native_drag_started_perf: float = 0.0
        self._fallback_chasing: bool = False

        self._cursor_origin_x: int = 0
        self._cursor_origin_y: int = 0
        self._win_origin_x: int = 0
        self._win_origin_y: int = 0
        self._orig_size: Tuple[int, int] = (780, 470)
        self._latest_event_x: int = 0
        self._latest_event_y: int = 0
        self._last_applied_pos: Optional[Tuple[int, int]] = None
        self._last_flush_perf: float = 0.0

        self._idle_id: Optional[str] = None
        self._timer_id: Optional[str] = None
        self._native_poll_id: Optional[str] = None

        # Pre-warmable 2-HWND proxy bar state
        self._proxy_win: Optional[tk.Toplevel] = None
        self._proxy_canvas: Optional[tk.Canvas] = None
        self._proxy_hwnd: int = 0
        self._proxy_active: bool = False
        self._proxy_swapping: bool = False
        self._proxy_warmed: bool = False
        self._proxy_bar_h: int = 42

    @property
    def active(self) -> bool:
        return self._active

    @property
    def moved(self) -> bool:
        return self._moved

    @property
    def use_win32_cursor(self) -> bool:
        return self._use_win32_cursor

    @property
    def native_drag_active(self) -> bool:
        return self._native_drag_active

    @property
    def proxy_active(self) -> bool:
        return self._proxy_active

    @property
    def proxy_swapping(self) -> bool:
        return self._proxy_swapping

    def _get_proxy_master(self) -> Optional[tk.Misc]:
        win = self._win
        if win is None:
            return None
        try:
            master = getattr(win, "master", None)
            if master is not None and master.winfo_exists():
                return master
            if win.winfo_exists():
                return win
        except tk.TclError:
            return None
        return None

    def _ensure_proxy_window(self) -> Optional[tk.Toplevel]:
        if self._proxy_win is not None:
            try:
                if self._proxy_win.winfo_exists():
                    return self._proxy_win
            except tk.TclError:
                pass
            self._proxy_win = None
            self._proxy_canvas = None
            self._proxy_hwnd = 0
            self._proxy_warmed = False

        master = self._get_proxy_master()
        if master is None:
            return None
        try:
            pw = tk.Toplevel(master)
            pw.overrideredirect(True)
            pw.resizable(False, False)
            # Keep opaque (no -alpha / WS_EX_LAYERED) for lowest possible DWM move latency
            cv = tk.Canvas(pw, highlightthickness=0, bd=0)
            cv.pack(fill="both", expand=True)
            pw.withdraw()
            self._proxy_win = pw
            self._proxy_canvas = cv
            return pw
        except tk.TclError:
            self._proxy_win = None
            self._proxy_canvas = None
            return None

    def _draw_proxy_bar(self, width: int, height: int) -> None:
        cv = self._proxy_canvas
        pw = self._proxy_win
        if cv is None or pw is None:
            return
        theme: Dict[str, str] = {}
        if self._theme_provider is not None:
            try:
                theme = dict(self._theme_provider() or {})
            except Exception:
                theme = {}
        bg = theme.get("panel_bg", "#0d1321")
        border = theme.get("cyan", "#38bdf8")
        fg = theme.get("text_primary", "#e2e8f0")
        muted = theme.get("text_muted", "#64748b")
        badge_bg = theme.get("card_alt_bg", "#131c2e")

        title = "拖动窗口定位"
        if self._title_provider is not None:
            try:
                t = str(self._title_provider() or "").strip()
                if t:
                    title = t
            except Exception:
                pass

        w = max(240, int(width))
        h = max(36, int(height))
        try:
            pw.configure(bg=border)
            cv.configure(bg=bg, width=w, height=h)
            cv.delete("all")
            # Outer themed frame + subtle inner highlight strip
            cv.create_rectangle(0, 0, w - 1, h - 1, fill=bg, outline=border, width=2)
            cv.create_rectangle(3, 3, w - 4, h - 4, fill=badge_bg, outline="")
            cy = h // 2
            cv.create_text(
                14,
                cy,
                text=title,
                anchor="w",
                fill=fg,
                font=(UI_FONT, 9, "bold"),
            )
            if w >= 420:
                cv.create_text(
                    w - 14,
                    cy,
                    text="拖动定位 · 松手恢复窗口",
                    anchor="e",
                    fill=muted,
                    font=(UI_FONT, 8),
                )
        except tk.TclError:
            pass

    def _compute_bar_height(self) -> int:
        if self._header_height_provider is not None:
            try:
                hh = int(self._header_height_provider())
                if hh > 0:
                    return max(38, min(54, hh + 16))
            except Exception:
                pass
        return 42

    def warm_up(self) -> None:
        """Pre-map the 2-HWND proxy bar once off-screen so first drag handoff completes in <10ms."""
        if os.name != "nt" or self._proxy_warmed or self._win is None:
            return
        if self._active or self._proxy_active:
            return
        try:
            if not self._win.winfo_exists():
                return
        except tk.TclError:
            return
        pw = self._ensure_proxy_window()
        if pw is None:
            return
        self._proxy_swapping = True
        try:
            w, _h = self._resolve_orig_size()
            bar_h = self._compute_bar_height()
            self._proxy_bar_h = bar_h
            self._draw_proxy_bar(w, bar_h)
            pw.geometry(f"{w}x{bar_h}+-32000+-32000")
            pw.deiconify()
            pw.update_idletasks()
            pw.withdraw()
            self._proxy_warmed = True
        except tk.TclError:
            pass
        finally:
            self._proxy_swapping = False

    def _resolve_orig_size(self) -> Tuple[int, int]:
        if self._size_provider is not None:
            try:
                w, h = self._size_provider()
                if int(w) > 80 and int(h) > 80:
                    return (int(w), int(h))
            except Exception:
                pass
        win = self._win
        if win is not None:
            try:
                w = int(win.winfo_width())
                h = int(win.winfo_height())
                if w > 80 and h > 80:
                    return (w, h)
                return (max(240, int(win.winfo_reqwidth())), max(120, int(win.winfo_reqheight())))
            except tk.TclError:
                pass
        return self._orig_size

    def _resolve_mapped_root_hwnd(self, win: Optional[tk.Misc], u32: Optional[ctypes.WinDLL]) -> int:
        if u32 is None or win is None:
            return 0
        try:
            if not win.winfo_ismapped():
                return 0
            if win.state() == "withdrawn":
                return 0
            wid = int(win.winfo_id())
        except (tk.TclError, ValueError, TypeError):
            return 0
        if not wid:
            return 0
        try:
            root_hwnd = int(u32.GetAncestor(wintypes.HWND(wid), wintypes.UINT(GA_ROOT)) or 0)
            if not root_hwnd:
                return 0
            if not bool(u32.IsWindow(wintypes.HWND(root_hwnd))):
                return 0
            return root_hwnd
        except Exception:
            return 0

    def _clamp_restored_pos(self, nx: int, ny: int, w: int, h: int) -> Tuple[int, int]:
        master = self._get_proxy_master() or self._win
        if master is None:
            return (int(nx), int(ny))
        try:
            if self._screen_bounds_fn is not None:
                vx, vy, vr, vb = self._screen_bounds_fn(master)
            else:
                vx, vy, vr, vb = 0, 0, master.winfo_screenwidth(), master.winfo_screenheight()
            max_x = max(vx + 4, vr - max(120, int(w)) - 4)
            max_y = max(vy + 4, vb - max(80, int(h)) - 40)
            cx = max(vx + 4, min(max_x, int(nx)))
            cy = max(vy + 4, min(max_y, int(ny)))
            return (cx, cy)
        except Exception:
            return (int(nx), int(ny))

    def _reset_state(self) -> None:
        self._active = False
        self._moved = False
        self._hwnd = 0
        self._proxy_hwnd = 0
        self._use_win32_cursor = False
        self._native_drag_active = False
        self._native_drag_seen_loop = False
        self._native_drag_failed = False
        self._native_drag_started_perf = 0.0
        self._fallback_chasing = False
        self._last_applied_pos = None
        self._last_flush_perf = 0.0

    def cancel_pending(self) -> None:
        win = self._win
        pw = self._proxy_win
        for attr in ("_idle_id", "_timer_id", "_native_poll_id"):
            aid = getattr(self, attr, None)
            setattr(self, attr, None)
            if aid is not None:
                for host in (win, pw):
                    if host is not None:
                        try:
                            host.after_cancel(aid)
                        except tk.TclError:
                            pass

    def _schedule_on_host(self, delay_ms: int, fn: Callable[[], None]) -> Optional[str]:
        host = self._win if self._win is not None else self._proxy_win
        if host is None:
            return None
        try:
            return host.after(max(1, int(delay_ms)), fn)
        except tk.TclError:
            return None

    def _apply_geometry(self, nx: int, ny: int) -> None:
        win = self._win
        if win is None:
            return
        try:
            if self._size_provider is not None:
                w, h = self._size_provider()
                win.geometry(f"{int(w)}x{int(h)}+{int(nx)}+{int(ny)}")
            else:
                win.geometry(f"+{int(nx)}+{int(ny)}")
        except tk.TclError:
            pass

    def _read_win32_cursor(self, u32: ctypes.WinDLL) -> Optional[Tuple[int, int]]:
        pt = wintypes.POINT()
        try:
            if u32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return None

    def _read_win32_window_origin(self, u32: ctypes.WinDLL, hwnd: int) -> Optional[Tuple[int, int]]:
        rect = wintypes.RECT()
        try:
            if hwnd and u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                return int(rect.left), int(rect.top)
        except Exception:
            pass
        return None

    def _compute_target_pos(self) -> Optional[Tuple[int, int]]:
        if self._use_win32_cursor:
            u32 = _get_user32()
            if u32 is None or not (self._proxy_hwnd or self._hwnd):
                return None
            cur = self._read_win32_cursor(u32)
            if cur is None:
                return None
            return (
                self._win_origin_x + (cur[0] - self._cursor_origin_x),
                self._win_origin_y + (cur[1] - self._cursor_origin_y),
            )
        return (
            self._win_origin_x + (self._latest_event_x - self._cursor_origin_x),
            self._win_origin_y + (self._latest_event_y - self._cursor_origin_y),
        )

    def _activate_proxy_at(self, nx: int, ny: int) -> int:
        """Show the 2-HWND proxy bar at (nx, ny), withdraw heavy target modal, and return proxy root HWND."""
        u32 = _get_user32()
        if u32 is None or self._win is None:
            return 0
        pw = self._ensure_proxy_window()
        if pw is None:
            return 0

        w, h = self._orig_size
        bar_h = self._compute_bar_height()
        self._proxy_bar_h = bar_h

        topmost = True
        try:
            topmost = bool(self._win.attributes("-topmost"))
        except tk.TclError:
            topmost = True

        self._proxy_swapping = True
        try:
            self._draw_proxy_bar(w, bar_h)
            try:
                pw.attributes("-topmost", topmost)
            except tk.TclError:
                pass
            pw.geometry(f"{w}x{bar_h}+{int(nx)}+{int(ny)}")
            pw.deiconify()
            pw.lift()
            pw.update_idletasks()

            proxy_hwnd = self._resolve_mapped_root_hwnd(pw, u32)
            if not proxy_hwnd:
                try:
                    pw.withdraw()
                except tk.TclError:
                    pass
                return 0

            # Hide the heavy multi-child modal immediately so only the 2-HWND bar moves
            self._win.withdraw()
            self._proxy_hwnd = proxy_hwnd
            self._proxy_active = True
            self._proxy_warmed = True
            return proxy_hwnd
        except tk.TclError:
            try:
                pw.withdraw()
            except tk.TclError:
                pass
            return 0
        finally:
            self._proxy_swapping = False

    def _restore_real_window_from_proxy(self, target_pos: Optional[Tuple[int, int]], restore_visible: bool = True) -> None:
        """Restore the real modal at target_pos (clamped to screen) before hiding the proxy bar (zero flicker)."""
        was_proxy = self._proxy_active
        self._proxy_active = False
        pw = self._proxy_win
        win = self._win

        self._proxy_swapping = True
        try:
            if restore_visible and win is not None:
                try:
                    if win.winfo_exists():
                        w, h = self._orig_size
                        if target_pos is not None:
                            cx, cy = self._clamp_restored_pos(target_pos[0], target_pos[1], w, h)
                            win.geometry(f"{w}x{h}+{cx}+{cy}")
                        if was_proxy or win.state() == "withdrawn":
                            win.deiconify()
                            win.lift()
                            win.update_idletasks()
                            if was_proxy:
                                try:
                                    win.focus_set()
                                except tk.TclError:
                                    pass
                except tk.TclError:
                    pass
            if pw is not None:
                try:
                    if pw.winfo_exists():
                        pw.withdraw()
                except tk.TclError:
                    pass
        finally:
            self._proxy_swapping = False

    def _active_move_hwnd(self) -> int:
        if self._proxy_active and self._proxy_hwnd:
            return self._proxy_hwnd
        return self._hwnd

    def _schedule_tail_flush(self, remaining_sec: float) -> None:
        if self._timer_id is not None or self._win is None:
            return
        delay_ms = max(1, int(round(max(0.001, remaining_sec) * 1000.0)))
        self._timer_id = self._schedule_on_host(delay_ms, self._on_flush_timer)

    def _schedule_flush(self) -> None:
        if self._idle_id is not None or self._timer_id is not None or self._win is None:
            return
        try:
            self._idle_id = self._win.after_idle(self._on_flush_idle)
        except tk.TclError:
            self._idle_id = None
        self._timer_id = self._schedule_on_host(self._flush_interval_ms, self._on_flush_timer)

    def _on_flush_idle(self) -> None:
        self._idle_id = None
        if self._timer_id is not None:
            tid = self._timer_id
            self._timer_id = None
            if self._win is not None:
                try:
                    self._win.after_cancel(tid)
                except tk.TclError:
                    pass
        if self._use_win32_cursor:
            elapsed = time.perf_counter() - self._last_flush_perf
            if elapsed < self._min_win32_interval_sec:
                self._schedule_tail_flush(self._min_win32_interval_sec - elapsed)
                return
        self._flush_move()

    def _on_flush_timer(self) -> None:
        self._timer_id = None
        if self._idle_id is not None:
            iid = self._idle_id
            self._idle_id = None
            if self._win is not None:
                try:
                    self._win.after_cancel(iid)
                except tk.TclError:
                    pass
        self._flush_move()

    def _flush_move(self) -> None:
        if not self._active or not self._moved:
            return
        pos = self._compute_target_pos()
        if pos is None or pos == self._last_applied_pos:
            return
        nx, ny = pos
        if self._use_win32_cursor:
            u32 = _get_user32()
            target_hwnd = self._active_move_hwnd()
            moved_ok = False
            if u32 is not None and target_hwnd:
                try:
                    if bool(u32.IsWindow(wintypes.HWND(target_hwnd))):
                        moved_ok = bool(
                            u32.SetWindowPos(
                                wintypes.HWND(target_hwnd),
                                wintypes.HWND(0),
                                int(nx),
                                int(ny),
                                0,
                                0,
                                wintypes.UINT(SWP_MOVE_FLAGS),
                            )
                        )
                except Exception:
                    moved_ok = False
            if moved_ok:
                self._last_applied_pos = (nx, ny)
                self._last_flush_perf = time.perf_counter()
                return
            if not self._proxy_active:
                self._apply_geometry(nx, ny)
            self._last_applied_pos = (nx, ny)
            self._last_flush_perf = time.perf_counter()
            return

        self._apply_geometry(nx, ny)
        self._last_applied_pos = (nx, ny)

    def _schedule_native_drag_poll(self, interval_ms: int = _NATIVE_POLL_INTERVAL_MS) -> None:
        if self._win is None or not self._active:
            return
        if self._native_poll_id is not None:
            try:
                self._win.after_cancel(self._native_poll_id)
            except tk.TclError:
                pass
            self._native_poll_id = None
        self._native_poll_id = self._schedule_on_host(max(4, int(interval_ms)), self._poll_native_drag_state)

    def _try_handoff_proxy_native_drag(self) -> bool:
        """Swap in 2-HWND proxy bar and hand off directly to Windows' kernel-native SC_MOVE modal loop."""
        if not self._active or not self._moved or not self._use_win32_cursor:
            return False
        if self._native_drag_active or self._native_drag_failed or not self._hwnd:
            return False
        u32 = _get_user32()
        if u32 is None or not _is_primary_button_pressed(u32):
            return False
        cur = self._read_win32_cursor(u32)
        if cur is None:
            return False

        nx = self._win_origin_x + (cur[0] - self._cursor_origin_x)
        ny = self._win_origin_y + (cur[1] - self._cursor_origin_y)

        # Prefer ultra-light 2-HWND proxy bar; gracefully fall back to real HWND if proxy creation fails
        move_hwnd = self._activate_proxy_at(nx, ny)
        if not move_hwnd:
            move_hwnd = self._hwnd
            self._flush_move()
        else:
            # Re-read live cursor after proxy swap so fast flicks stay locked 1:1 with zero jump
            cur_live = self._read_win32_cursor(u32)
            if cur_live is not None:
                cur = cur_live
                nx2 = self._win_origin_x + (cur[0] - self._cursor_origin_x)
                ny2 = self._win_origin_y + (cur[1] - self._cursor_origin_y)
                if (nx2, ny2) != (nx, ny):
                    try:
                        u32.SetWindowPos(
                            wintypes.HWND(move_hwnd),
                            wintypes.HWND(0),
                            int(nx2),
                            int(ny2),
                            0,
                            0,
                            wintypes.UINT(SWP_MOVE_FLAGS),
                        )
                    except Exception:
                        pass
                nx, ny = nx2, ny2
            self._last_applied_pos = (nx, ny)
            self._last_flush_perf = time.perf_counter()

        lparam = _pack_screen_lparam(cur[0], cur[1])
        try:
            u32.ReleaseCapture()
            posted = bool(
                u32.PostMessageW(
                    wintypes.HWND(move_hwnd),
                    wintypes.UINT(WM_SYSCOMMAND),
                    wintypes.WPARAM(SC_MOVE_HTCAPTION),
                    wintypes.LPARAM(lparam),
                )
            )
        except Exception:
            posted = False
        if not posted:
            self._native_drag_failed = True
            # If the real modal was already swapped out or capture released, seamlessly chase proxy_hwnd
            # so the window can NEVER stay withdrawn/frozen waiting for lost Tk mouse events!
            self.cancel_pending()
            self._fallback_chasing = True
            self._flush_move()
            self._schedule_native_drag_poll(_FALLBACK_CHASE_INTERVAL_MS)
            return True

        self.cancel_pending()
        self._native_drag_active = True
        self._native_drag_seen_loop = False
        self._native_drag_started_perf = time.perf_counter()
        self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
        return True

    def _complete_native_or_chase_drag(self) -> None:
        self.cancel_pending()
        if not self._active:
            return
        u32 = _get_user32()
        move_hwnd = self._active_move_hwnd()
        final_pos: Optional[Tuple[int, int]] = None
        if u32 is not None and move_hwnd:
            final_pos = self._read_win32_window_origin(u32, move_hwnd)
        if final_pos is None:
            final_pos = self._last_applied_pos

        was_proxy = self._proxy_active
        self._reset_state()

        if was_proxy:
            self._restore_real_window_from_proxy(final_pos, restore_visible=True)
        elif final_pos is not None:
            w, h = self._orig_size
            cx, cy = self._clamp_restored_pos(final_pos[0], final_pos[1], w, h)
            self._apply_geometry(cx, cy)
            if self._win is not None:
                try:
                    self._win.update_idletasks()
                except tk.TclError:
                    pass

        if self._on_drag_end is not None:
            self._on_drag_end(True)

    def _poll_native_drag_state(self) -> None:
        self._native_poll_id = None
        if not self._active:
            return
        u32 = _get_user32()
        move_hwnd = self._active_move_hwnd()
        if u32 is None or not move_hwnd:
            self._complete_native_or_chase_drag()
            return

        if self._fallback_chasing:
            if _is_hwnd_in_move_size_loop(u32, move_hwnd):
                # Late OS SC_MOVE took over; yield control cleanly instead of fighting SetWindowPos
                self._fallback_chasing = False
                self._native_drag_active = True
                self._native_drag_seen_loop = True
                self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
                return
            if _is_primary_button_pressed(u32):
                self._flush_move()
                self._schedule_native_drag_poll(_FALLBACK_CHASE_INTERVAL_MS)
            else:
                self._complete_native_or_chase_drag()
            return

        if not self._native_drag_active:
            return

        in_loop = _is_hwnd_in_move_size_loop(u32, move_hwnd)
        btn_down = _is_primary_button_pressed(u32)

        if in_loop:
            self._native_drag_seen_loop = True
            self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
            return

        if not self._native_drag_seen_loop and btn_down:
            if (time.perf_counter() - self._native_drag_started_perf) < _PROXY_NATIVE_HANDOFF_GRACE_SEC:
                self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
                return
            # Cancel any queued WM_SYSCOMMAND before entering fallback chase to avoid dual-writer fighting
            try:
                u32.PostMessageW(wintypes.HWND(move_hwnd), wintypes.UINT(WM_CANCELMODE), 0, 0)
            except Exception:
                pass
            # Fallback chases the 2-HWND proxy bar directly (~0.65ms/move)
            self._native_drag_active = False
            self._native_drag_failed = True
            self._fallback_chasing = True
            self._flush_move()
            self._schedule_native_drag_poll(_FALLBACK_CHASE_INTERVAL_MS)
            return

        self._complete_native_or_chase_drag()

    def on_start(self, event: tk.Event) -> None:
        if self._proxy_active:
            self._restore_real_window_from_proxy(None, restore_visible=True)
        self.cancel_pending()
        self._reset_state()

        ev_x = int(getattr(event, "x_root", 0))
        ev_y = int(getattr(event, "y_root", 0))
        self._latest_event_x = ev_x
        self._latest_event_y = ev_y
        self._orig_size = self._resolve_orig_size()
        self._active = True

        u32 = _get_user32()
        hwnd = self._resolve_mapped_root_hwnd(self._win, u32)
        if u32 is not None and hwnd:
            cur = self._read_win32_cursor(u32)
            win_orig = self._read_win32_window_origin(u32, hwnd)
            if (
                cur is not None
                and win_orig is not None
                and abs(cur[0] - ev_x) <= _WIN32_CURSOR_MATCH_TOLERANCE_PX
                and abs(cur[1] - ev_y) <= _WIN32_CURSOR_MATCH_TOLERANCE_PX
                and _is_primary_button_pressed(u32)
            ):
                self._hwnd = hwnd
                self._use_win32_cursor = True
                self._cursor_origin_x = cur[0]
                self._cursor_origin_y = cur[1]
                self._win_origin_x = win_orig[0]
                self._win_origin_y = win_orig[1]

        if not self._use_win32_cursor:
            self._cursor_origin_x = ev_x
            self._cursor_origin_y = ev_y
            try:
                self._win_origin_x = int(self._win.winfo_x())
                self._win_origin_y = int(self._win.winfo_y())
            except tk.TclError:
                self._win_origin_x = 0
                self._win_origin_y = 0

        if self._on_drag_start is not None:
            self._on_drag_start()

    def on_move(self, event: tk.Event) -> None:
        if not self._active:
            return
        if self._native_drag_active or self._fallback_chasing:
            return
        self._latest_event_x = int(getattr(event, "x_root", self._latest_event_x))
        self._latest_event_y = int(getattr(event, "y_root", self._latest_event_y))

        was_moved_before = self._moved
        if not self._moved:
            if self._move_threshold <= 0:
                if self._use_win32_cursor:
                    u32 = _get_user32()
                    cur = self._read_win32_cursor(u32) if u32 is not None else None
                    if cur is not None and (cur[0] != self._cursor_origin_x or cur[1] != self._cursor_origin_y):
                        self._moved = True
                    elif cur is None:
                        return
                else:
                    self._moved = True
            elif self._use_win32_cursor:
                u32 = _get_user32()
                cur = self._read_win32_cursor(u32) if u32 is not None else None
                if cur is None:
                    return
                if (
                    abs(cur[0] - self._cursor_origin_x) >= self._move_threshold
                    or abs(cur[1] - self._cursor_origin_y) >= self._move_threshold
                ):
                    self._moved = True
            else:
                if (
                    abs(self._latest_event_x - self._cursor_origin_x) >= self._move_threshold
                    or abs(self._latest_event_y - self._cursor_origin_y) >= self._move_threshold
                ):
                    self._moved = True

        if self._moved and not was_moved_before and self._on_drag_move_start is not None:
            self._on_drag_move_start()

        if self._moved:
            if self._use_win32_cursor:
                if not self._native_drag_failed and self._try_handoff_proxy_native_drag():
                    return
                now_p = time.perf_counter()
                elapsed = now_p - self._last_flush_perf
                if elapsed >= self._min_win32_interval_sec:
                    self.cancel_pending()
                    self._flush_move()
                else:
                    self._schedule_tail_flush(self._min_win32_interval_sec - elapsed)
            else:
                self._schedule_flush()

    def _resolve_final_pos_for_commit(self) -> Optional[Tuple[int, int]]:
        pos = self._compute_target_pos()
        if pos is not None:
            return pos
        move_hwnd = self._active_move_hwnd()
        if self._use_win32_cursor and move_hwnd:
            u32 = _get_user32()
            if u32 is not None:
                win_orig = self._read_win32_window_origin(u32, move_hwnd)
                if win_orig is not None:
                    return win_orig
        return self._last_applied_pos

    def on_end(self, event: Optional[tk.Event] = None) -> None:
        if self._native_drag_active:
            u32 = _get_user32()
            move_hwnd = self._active_move_hwnd()
            if u32 is not None and move_hwnd and _is_hwnd_in_move_size_loop(u32, move_hwnd):
                return
            self._complete_native_or_chase_drag()
            return
        if self._fallback_chasing:
            self._complete_native_or_chase_drag()
            return

        self.cancel_pending()
        if not self._active:
            return
        if event is not None and not self._use_win32_cursor:
            ev_x = getattr(event, "x_root", None)
            ev_y = getattr(event, "y_root", None)
            if ev_x is not None:
                self._latest_event_x = int(ev_x)
            if ev_y is not None:
                self._latest_event_y = int(ev_y)

        was_moved = self._moved
        was_proxy = self._proxy_active
        final_pos = self._resolve_final_pos_for_commit() if was_moved else None
        self._reset_state()

        if was_proxy:
            self._restore_real_window_from_proxy(final_pos, restore_visible=True)
        elif was_moved and final_pos is not None:
            self._apply_geometry(final_pos[0], final_pos[1])
            if self._win is not None:
                try:
                    self._win.update_idletasks()
                except tk.TclError:
                    pass

        if self._on_drag_end is not None:
            self._on_drag_end(was_moved)

    def check_release_watchdog(self) -> bool:
        if not self._active or not self._use_win32_cursor:
            return False
        u32 = _get_user32()
        if u32 is None:
            return False
        move_hwnd = self._active_move_hwnd()
        if self._native_drag_active:
            if move_hwnd and _is_hwnd_in_move_size_loop(u32, move_hwnd):
                self._native_drag_seen_loop = True
                return False
            if _is_primary_button_pressed(u32):
                return False
            self._complete_native_or_chase_drag()
            return True
        if _is_primary_button_pressed(u32):
            return False
        return self.finish_or_cancel(commit=self._moved)

    def _resolve_stationary_pos_for_commit(self) -> Optional[Tuple[int, int]]:
        move_hwnd = self._active_move_hwnd()
        if self._use_win32_cursor and move_hwnd:
            u32 = _get_user32()
            if u32 is not None:
                win_orig = self._read_win32_window_origin(u32, move_hwnd)
                if win_orig is not None:
                    return win_orig
        if self._last_applied_pos is not None:
            return self._last_applied_pos
        return (
            self._win_origin_x + (self._latest_event_x - self._cursor_origin_x),
            self._win_origin_y + (self._latest_event_y - self._cursor_origin_y),
        )

    def finish_or_cancel(self, commit: bool = False, force: bool = False) -> bool:
        if self._proxy_swapping and not force:
            return False
        if not self._active:
            self.cancel_pending()
            if force and self._proxy_active:
                self._restore_real_window_from_proxy(None, restore_visible=True)
            return False
        if self._native_drag_active:
            u32 = _get_user32()
            move_hwnd = self._active_move_hwnd()
            if force:
                if u32 is not None and move_hwnd:
                    try:
                        u32.PostMessageW(wintypes.HWND(move_hwnd), wintypes.UINT(WM_CANCELMODE), 0, 0)
                    except Exception:
                        pass
            elif u32 is not None and move_hwnd:
                if _is_hwnd_in_move_size_loop(u32, move_hwnd):
                    self._native_drag_seen_loop = True
                    return False
                if (
                    not self._native_drag_seen_loop
                    and _is_primary_button_pressed(u32)
                    and (time.perf_counter() - self._native_drag_started_perf) < _PROXY_NATIVE_HANDOFF_GRACE_SEC
                ):
                    return False
        elif self._fallback_chasing and not force:
            u32 = _get_user32()
            if u32 is not None and _is_primary_button_pressed(u32):
                return False

        self.cancel_pending()
        should_commit = bool(commit and self._moved)
        was_proxy = self._proxy_active
        final_pos = self._resolve_stationary_pos_for_commit() if should_commit else None
        self._reset_state()

        if was_proxy:
            self._restore_real_window_from_proxy(final_pos if should_commit else None, restore_visible=True)
        elif should_commit and final_pos is not None:
            self._apply_geometry(final_pos[0], final_pos[1])
            if self._win is not None:
                try:
                    self._win.update_idletasks()
                except tk.TclError:
                    pass

        if should_commit:
            if self._on_drag_end is not None:
                self._on_drag_end(True)
            return True

        if self._on_drag_cancel is not None:
            self._on_drag_cancel()
        return True

    def cancel(self, restore_real: bool = True) -> None:
        move_hwnd = self._active_move_hwnd()
        if self._native_drag_active and move_hwnd:
            u32 = _get_user32()
            if u32 is not None:
                try:
                    u32.PostMessageW(wintypes.HWND(move_hwnd), wintypes.UINT(WM_CANCELMODE), 0, 0)
                except Exception:
                    pass
        was_proxy = self._proxy_active
        self.cancel_pending()
        self._reset_state()
        if was_proxy:
            self._restore_real_window_from_proxy(None, restore_visible=restore_real)

    def destroy_proxy(self) -> None:
        """Deterministically destroy the proxy Toplevel and break all references on modal close."""
        self.cancel(restore_real=False)
        pw = self._proxy_win
        cv = self._proxy_canvas
        self._proxy_win = None
        self._proxy_canvas = None
        self._proxy_hwnd = 0
        self._proxy_active = False
        self._proxy_warmed = False
        if cv is not None:
            try:
                cv.delete("all")
            except tk.TclError:
                pass
        if pw is not None:
            try:
                pw.destroy()
            except tk.TclError:
                pass
        self._on_drag_start = None
        self._on_drag_move_start = None
        self._on_drag_end = None
        self._on_drag_cancel = None
        self._size_provider = None
        self._theme_provider = None
        self._title_provider = None
        self._header_height_provider = None
        self._screen_bounds_fn = None
        self._win = None  # type: ignore[assignment]
