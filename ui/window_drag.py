"""Low-latency, jitter-free window dragging helper with Win32 kernel-native SC_MOVE primary path."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import tkinter as tk
from typing import Callable, Optional, Tuple

GA_ROOT = 2
SM_SWAPBUTTON = 23
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

WM_CANCELMODE = 0x001F
WM_SYSCOMMAND = 0x0112
SC_MOVE_HTCAPTION = 0xF012
GUI_INMOVESIZE = 0x00000002

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_MOVE_FLAGS = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER

_WIN32_CURSOR_MATCH_TOLERANCE_PX = 128
_NATIVE_HANDOFF_GRACE_SEC = 0.16
_NATIVE_POLL_INTERVAL_MS = 16
_FALLBACK_CHASE_INTERVAL_MS = 8

_USER32_INITIALIZED: bool = False
_USER32_DLL: Optional[ctypes.WinDLL] = None


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _get_user32() -> Optional[ctypes.WinDLL]:
    """Return module-cached Win32 user32 handle with explicit prototypes (nt only)."""
    global _USER32_INITIALIZED, _USER32_DLL
    if os.name != "nt":
        return None
    if _USER32_INITIALIZED:
        return _USER32_DLL
    _USER32_INITIALIZED = True
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetAncestor.restype = wintypes.HWND

        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.IsWindow.restype = wintypes.BOOL

        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.GetWindowRect.restype = wintypes.BOOL

        u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u32.GetCursorPos.restype = wintypes.BOOL

        u32.GetSystemMetrics.argtypes = [ctypes.c_int]
        u32.GetSystemMetrics.restype = ctypes.c_int

        u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u32.GetAsyncKeyState.restype = ctypes.c_short

        u32.ReleaseCapture.argtypes = []
        u32.ReleaseCapture.restype = wintypes.BOOL

        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.PostMessageW.restype = wintypes.BOOL

        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD

        u32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
        u32.GetGUIThreadInfo.restype = wintypes.BOOL

        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        _USER32_DLL = u32
    except Exception:
        _USER32_DLL = None
    return _USER32_DLL


def _is_primary_button_pressed(u32: ctypes.WinDLL) -> bool:
    """Check whether the physical primary mouse button is currently held down."""
    try:
        vk = VK_RBUTTON if u32.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON
        return bool(u32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _is_hwnd_in_move_size_loop(u32: ctypes.WinDLL, hwnd: int) -> bool:
    """Return True while Windows' native modal move/size loop is actively moving hwnd."""
    if not hwnd:
        return False
    try:
        tid = int(u32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) or 0)
        if not tid:
            return False
        gti = _GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not u32.GetGUIThreadInfo(wintypes.DWORD(tid), ctypes.byref(gti)):
            return False
        if not (int(gti.flags) & GUI_INMOVESIZE):
            return False
        mv_hwnd = int(gti.hwndMoveSize or 0)
        return mv_hwnd in (0, int(hwnd))
    except Exception:
        return False


def _pack_screen_lparam(x: int, y: int) -> int:
    """Pack signed 16-bit screen coordinates into Win32 LPARAM (supports negative multi-monitor coords)."""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


class SmoothWindowDragger:
    """Zero-latency window dragger with Win32 kernel-native SC_MOVE primary path.

    On Windows with a mapped top-level window and live physical mouse input:
    - Once pointer motion crosses ``move_threshold`` (preserving click-to-expand when
      released below threshold), the drag immediately hands off to Windows' native
      kernel modal move loop via ``ReleaseCapture() + PostMessageW(WM_SYSCOMMAND,
      SC_MOVE | HTCAPTION)`` at the top level of the message loop (zero Python
      ``<B1-Motion>`` callbacks, zero GIL contention, zero Tk event backlog, and
      zero nested ``SendMessage`` GIL re-entrancy crashes).
    - A lightweight 16ms monitor syncs final HWND coordinates back to Tk once via
      ``geometry()`` upon release, and transparently falls back to throttled Win32
      ``SetWindowPos`` tracking if a restricted/remote environment ever swallows
      ``WM_SYSCOMMAND``.

    In synthetic event tests, withdrawn windows, restricted sessions, or non-Windows
    environments, it cleanly falls back to Tk root-event coordinates and ``geometry()``
    without ever mixing coordinate spaces.
    """

    def __init__(
        self,
        win: tk.Misc,
        move_threshold: int = 0,
        on_drag_start: Optional[Callable[[], None]] = None,
        on_drag_move_start: Optional[Callable[[], None]] = None,
        on_drag_end: Optional[Callable[[bool], None]] = None,
        on_drag_cancel: Optional[Callable[[], None]] = None,
        size_provider: Optional[Callable[[], Tuple[int, int]]] = None,
        flush_interval_ms: int = 8,
    ) -> None:
        self._win = win
        self._move_threshold: int = max(0, int(move_threshold))
        self._on_drag_start = on_drag_start
        self._on_drag_move_start = on_drag_move_start
        self._on_drag_end = on_drag_end
        self._on_drag_cancel = on_drag_cancel
        self._size_provider = size_provider
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
        self._latest_event_x: int = 0
        self._latest_event_y: int = 0
        self._last_applied_pos: Optional[Tuple[int, int]] = None
        self._last_flush_perf: float = 0.0

        self._idle_id: Optional[str] = None
        self._timer_id: Optional[str] = None
        self._native_poll_id: Optional[str] = None

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

    def _resolve_mapped_root_hwnd(self, u32: Optional[ctypes.WinDLL]) -> int:
        """Resolve real top-level root HWND live at drag start; never cached at __init__."""
        if u32 is None:
            return 0
        try:
            if not self._win.winfo_ismapped():
                return 0
            if self._win.state() == "withdrawn":
                return 0
            wid = int(self._win.winfo_id())
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

    def _reset_state(self) -> None:
        self._active = False
        self._moved = False
        self._hwnd = 0
        self._use_win32_cursor = False
        self._native_drag_active = False
        self._native_drag_seen_loop = False
        self._native_drag_failed = False
        self._native_drag_started_perf = 0.0
        self._fallback_chasing = False
        self._last_applied_pos = None
        self._last_flush_perf = 0.0

    def cancel_pending(self) -> None:
        """Cancel any pending coalesced move or native-drag monitor timers."""
        win = self._win
        for attr in ("_idle_id", "_timer_id", "_native_poll_id"):
            aid = getattr(self, attr, None)
            setattr(self, attr, None)
            if aid is not None and win is not None:
                try:
                    win.after_cancel(aid)
                except tk.TclError:
                    pass

    def _apply_geometry(self, nx: int, ny: int) -> None:
        if self._win is None:
            return
        try:
            if self._size_provider is not None:
                w, h = self._size_provider()
                self._win.geometry(f"{int(w)}x{int(h)}+{int(nx)}+{int(ny)}")
            else:
                self._win.geometry(f"+{int(nx)}+{int(ny)}")
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
            if u32 is None or not self._hwnd:
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

    def _schedule_tail_flush(self, remaining_sec: float) -> None:
        """Schedule a single deferred flush honoring the Win32 minimum interval (never bypassing throttle)."""
        if self._timer_id is not None or self._win is None:
            return
        delay_ms = max(1, int(round(max(0.001, remaining_sec) * 1000.0)))
        try:
            self._timer_id = self._win.after(delay_ms, self._on_flush_timer)
        except tk.TclError:
            self._timer_id = None

    def _schedule_flush(self) -> None:
        if self._idle_id is not None or self._timer_id is not None or self._win is None:
            return
        try:
            self._idle_id = self._win.after_idle(self._on_flush_idle)
        except tk.TclError:
            self._idle_id = None
        try:
            self._timer_id = self._win.after(self._flush_interval_ms, self._on_flush_timer)
        except tk.TclError:
            self._timer_id = None

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
            moved_ok = False
            if u32 is not None and self._hwnd:
                try:
                    if bool(u32.IsWindow(wintypes.HWND(self._hwnd))):
                        moved_ok = bool(
                            u32.SetWindowPos(
                                wintypes.HWND(self._hwnd),
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
        try:
            self._native_poll_id = self._win.after(max(4, int(interval_ms)), self._poll_native_drag_state)
        except tk.TclError:
            self._native_poll_id = None

    def _try_handoff_native_drag(self) -> bool:
        """Hand off an active Win32 drag to Windows' kernel-native SC_MOVE modal loop."""
        if not self._active or not self._moved or not self._use_win32_cursor:
            return False
        if self._native_drag_active or self._native_drag_failed or not self._hwnd:
            return False
        u32 = _get_user32()
        if u32 is None or not _is_primary_button_pressed(u32):
            return False
        try:
            if not bool(u32.IsWindow(wintypes.HWND(self._hwnd))):
                return False
        except Exception:
            return False
        cur = self._read_win32_cursor(u32)
        if cur is None:
            return False
        # Snap the small initial threshold delta first so the grab pixel stays locked 1:1
        self._flush_move()
        lparam = _pack_screen_lparam(cur[0], cur[1])
        try:
            u32.ReleaseCapture()
            posted = bool(
                u32.PostMessageW(
                    wintypes.HWND(self._hwnd),
                    wintypes.UINT(WM_SYSCOMMAND),
                    wintypes.WPARAM(SC_MOVE_HTCAPTION),
                    wintypes.LPARAM(lparam),
                )
            )
        except Exception:
            posted = False
        if not posted:
            self._native_drag_failed = True
            return False
        self.cancel_pending()
        self._native_drag_active = True
        self._native_drag_seen_loop = False
        self._native_drag_started_perf = time.perf_counter()
        self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
        return True

    def _complete_native_or_chase_drag(self) -> None:
        """Commit final HWND coordinates after Windows kernel move loop (or fallback chase) exits."""
        self.cancel_pending()
        if not self._active:
            return
        u32 = _get_user32()
        final_pos: Optional[Tuple[int, int]] = None
        if u32 is not None and self._hwnd:
            final_pos = self._read_win32_window_origin(u32, self._hwnd)
        if final_pos is None:
            final_pos = self._last_applied_pos
        self._reset_state()

        if final_pos is not None:
            self._apply_geometry(final_pos[0], final_pos[1])
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
        if u32 is None or not self._hwnd:
            self._complete_native_or_chase_drag()
            return

        if self._fallback_chasing:
            if _is_primary_button_pressed(u32):
                self._flush_move()
                self._schedule_native_drag_poll(_FALLBACK_CHASE_INTERVAL_MS)
            else:
                self._complete_native_or_chase_drag()
            return

        if not self._native_drag_active:
            return

        in_loop = _is_hwnd_in_move_size_loop(u32, self._hwnd)
        btn_down = _is_primary_button_pressed(u32)

        if in_loop:
            self._native_drag_seen_loop = True
            self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
            return

        if not self._native_drag_seen_loop and btn_down:
            if (time.perf_counter() - self._native_drag_started_perf) < _NATIVE_HANDOFF_GRACE_SEC:
                self._schedule_native_drag_poll(_NATIVE_POLL_INTERVAL_MS)
                return
            # Restricted/remote environment swallowed WM_SYSCOMMAND: seamlessly chase cursor via SetWindowPos!
            self._native_drag_active = False
            self._native_drag_failed = True
            self._fallback_chasing = True
            self._flush_move()
            self._schedule_native_drag_poll(_FALLBACK_CHASE_INTERVAL_MS)
            return

        # Native OS move loop finished cleanly (or user released before message pump took over)
        self._complete_native_or_chase_drag()

    def on_start(self, event: tk.Event) -> None:
        self.cancel_pending()
        self._reset_state()

        ev_x = int(getattr(event, "x_root", 0))
        ev_y = int(getattr(event, "y_root", 0))
        self._latest_event_x = ev_x
        self._latest_event_y = ev_y
        self._active = True

        u32 = _get_user32()
        hwnd = self._resolve_mapped_root_hwnd(u32)
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
                dx = cur[0] - self._cursor_origin_x
                dy = cur[1] - self._cursor_origin_y
                if abs(dx) >= self._move_threshold or abs(dy) >= self._move_threshold:
                    self._moved = True
            else:
                dx = self._latest_event_x - self._cursor_origin_x
                dy = self._latest_event_y - self._cursor_origin_y
                if abs(dx) >= self._move_threshold or abs(dy) >= self._move_threshold:
                    self._moved = True

        if self._moved and not was_moved_before and self._on_drag_move_start is not None:
            self._on_drag_move_start()

        if self._moved:
            if self._use_win32_cursor:
                if not self._native_drag_failed and self._try_handoff_native_drag():
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
        if self._use_win32_cursor and self._hwnd:
            u32 = _get_user32()
            if u32 is not None:
                win_orig = self._read_win32_window_origin(u32, self._hwnd)
                if win_orig is not None:
                    return win_orig
        return self._last_applied_pos

    def on_end(self, event: Optional[tk.Event] = None) -> None:
        if self._native_drag_active:
            # Native OS drag owns button release; let _poll_native_drag_state or immediate check finish cleanly
            u32 = _get_user32()
            if u32 is not None and _is_hwnd_in_move_size_loop(u32, self._hwnd):
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
        final_pos = self._resolve_final_pos_for_commit() if was_moved else None
        self._reset_state()

        if was_moved and final_pos is not None:
            self._apply_geometry(final_pos[0], final_pos[1])
            if self._win is not None:
                try:
                    self._win.update_idletasks()
                except tk.TclError:
                    pass

        if self._on_drag_end is not None:
            self._on_drag_end(was_moved)

    def check_release_watchdog(self) -> bool:
        """Auto-finish drag if physical primary button was released while release event was lost."""
        if not self._active or not self._use_win32_cursor:
            return False
        u32 = _get_user32()
        if u32 is None:
            return False
        if self._native_drag_active:
            if _is_hwnd_in_move_size_loop(u32, self._hwnd):
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
        """Resolve current window position in-place without re-reading delayed live cursor (prevents jump on lost release)."""
        if self._use_win32_cursor and self._hwnd:
            u32 = _get_user32()
            if u32 is not None:
                win_orig = self._read_win32_window_origin(u32, self._hwnd)
                if win_orig is not None:
                    return win_orig
        if self._last_applied_pos is not None:
            return self._last_applied_pos
        return (
            self._win_origin_x + (self._latest_event_x - self._cursor_origin_x),
            self._win_origin_y + (self._latest_event_y - self._cursor_origin_y),
        )

    def finish_or_cancel(self, commit: bool = False) -> bool:
        """Safely close an in-flight drag on hide, context menu, focus loss, or watchdog."""
        if not self._active:
            self.cancel_pending()
            return False
        # Do NOT let transient FocusOut events abort an active OS kernel modal drag!
        if self._native_drag_active:
            u32 = _get_user32()
            if u32 is not None and self._hwnd:
                in_loop = _is_hwnd_in_move_size_loop(u32, self._hwnd)
                if in_loop:
                    self._native_drag_seen_loop = True
                    return False
                if (
                    not self._native_drag_seen_loop
                    and _is_primary_button_pressed(u32)
                    and (time.perf_counter() - self._native_drag_started_perf) < _NATIVE_HANDOFF_GRACE_SEC
                ):
                    return False

        self.cancel_pending()
        should_commit = bool(commit and self._moved)
        final_pos = self._resolve_stationary_pos_for_commit() if should_commit else None
        self._reset_state()

        if should_commit:
            if final_pos is not None:
                self._apply_geometry(final_pos[0], final_pos[1])
                if self._win is not None:
                    try:
                        self._win.update_idletasks()
                    except tk.TclError:
                        pass
            if self._on_drag_end is not None:
                self._on_drag_end(True)
            return True

        if self._on_drag_cancel is not None:
            self._on_drag_cancel()
        return True

    def cancel(self) -> None:
        """Hard-cancel all timers, exit any active OS modal drag loop, and clear state without UI callbacks."""
        if self._native_drag_active and self._hwnd:
            u32 = _get_user32()
            if u32 is not None:
                try:
                    u32.PostMessageW(wintypes.HWND(self._hwnd), wintypes.UINT(WM_CANCELMODE), 0, 0)
                except Exception:
                    pass
        self.cancel_pending()
        self._reset_state()
