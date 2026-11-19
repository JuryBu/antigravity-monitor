"""Entry point for Antigravity Monitor with High-DPI awareness and Win32 single-instance wakeup."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

MUTEX_NAME = "Local\\AntigravityMonitorSingleInstanceMutex_v1"
WAKE_EVENT_NAME = "Local\\AntigravityMonitorWakeupEvent_v1"
ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF


def _make_kernel32() -> ctypes.WinDLL:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.WinDLL("user32", use_last_error=True).SetProcessDPIAware()
        except Exception:
            pass


class SingleInstanceGuard:
    """Win32 Named Mutex + Named Event so a second double-click wakes the running instance."""

    def __init__(self) -> None:
        self._k32: Optional[ctypes.WinDLL] = _make_kernel32() if os.name == "nt" else None
        self.mutex_handle: Optional[int] = None
        self.event_handle: Optional[int] = None
        self.already_running: bool = False

    def acquire(self) -> bool:
        if os.name != "nt" or self._k32 is None:
            return True
        ctypes.set_last_error(0)
        handle = self._k32.CreateMutexW(None, False, MUTEX_NAME)
        last_err = ctypes.get_last_error()
        if not handle:
            return True
        self.mutex_handle = int(handle)
        if last_err == ERROR_ALREADY_EXISTS:
            self.already_running = True
            self._signal_existing_instance()
            self.release()
            return False
        ev = self._k32.CreateEventW(None, False, False, WAKE_EVENT_NAME)
        self.event_handle = int(ev) if ev else None
        return True

    def _signal_existing_instance(self) -> None:
        if self._k32 is None:
            return
        try:
            EVENT_MODIFY_STATE = 0x0002
            ev = self._k32.OpenEventW(EVENT_MODIFY_STATE, False, WAKE_EVENT_NAME)
            if ev:
                try:
                    self._k32.SetEvent(ev)
                finally:
                    self._k32.CloseHandle(ev)
        except Exception:
            pass

    def signal_stop(self) -> None:
        """Immediately wake the INFINITE wait thread so shutdown releases the Mutex in 0ms."""
        if self._k32 is not None and self.event_handle:
            try:
                self._k32.SetEvent(wintypes.HANDLE(self.event_handle))
            except Exception:
                pass

    def start_wakeup_listener(self, stop_event: threading.Event, on_wake: Callable[[], None]) -> Optional[threading.Thread]:
        if os.name != "nt" or self._k32 is None or not self.event_handle:
            return None
        k32 = self._k32
        ev_handle = wintypes.HANDLE(self.event_handle)

        def _watch() -> None:
            WAIT_OBJECT_0 = 0
            while not stop_event.is_set():
                res = k32.WaitForSingleObject(ev_handle, INFINITE)
                if stop_event.is_set():
                    break
                if res == WAIT_OBJECT_0:
                    try:
                        on_wake()
                    except Exception:
                        pass

        t = threading.Thread(target=_watch, name="WakeupEventThread", daemon=True)
        t.start()
        return t

    def release(self) -> None:
        if os.name != "nt" or self._k32 is None:
            return
        if self.event_handle:
            try:
                self._k32.CloseHandle(wintypes.HANDLE(self.event_handle))
            except Exception:
                pass
            self.event_handle = None
        if self.mutex_handle:
            try:
                self._k32.CloseHandle(wintypes.HANDLE(self.mutex_handle))
            except Exception:
                pass
            self.mutex_handle = None


def _spawn_restarted_self(no_tray: bool = False) -> None:
    """Relaunch using the current interpreter's pythonw.exe to preserve active venv dependencies."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    launcher = str(pyw) if os.name == "nt" and pyw.is_file() else str(exe)
    cmd = [launcher, str(APP_ROOT / "main.py")]
    if no_tray:
        cmd.append("--no-tray")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(cmd, cwd=str(APP_ROOT), creationflags=flags)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Antigravity 实时缓存与健康度桌面监控器")
    parser.add_argument("--cli", action="store_true", help="仅运行单次命令行脱敏体检摘要并退出")
    parser.add_argument("--minimized", action="store_true", help="启动后静默最小化到系统托盘")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘（调试/截图模式）")
    args = parser.parse_args(argv)

    from core.config_store import ConfigStore
    from core.log_monitor import AntigravityMonitorEngine, format_text_summary

    config_store = ConfigStore(APP_ROOT / "config.json")
    cfg = config_store.load()
    engine = AntigravityMonitorEngine(config=cfg)

    if args.cli:
        snap = engine.poll(force_account=True)
        print(format_text_summary(snap))
        return 0

    _enable_high_dpi()
    guard = SingleInstanceGuard()
    if not guard.acquire():
        return 0

    from ui.capsule_app import AntigravityCapsuleApp

    restart_requested = False

    def _mark_restart() -> None:
        nonlocal restart_requested
        restart_requested = True

    wake_stop = threading.Event()
    app = AntigravityCapsuleApp(
        app_root=APP_ROOT,
        config_store=config_store,
        engine=engine,
        enable_tray=not args.no_tray,
        start_minimized_override=True if args.minimized else None,
        on_restart=_mark_restart,
    )
    wake_thread = guard.start_wakeup_listener(wake_stop, lambda: app.dispatch_to_ui(app.show_window))
    try:
        app.run()
    finally:
        wake_stop.set()
        guard.signal_stop()
        if wake_thread is not None and wake_thread.is_alive():
            wake_thread.join(timeout=1.0)
        guard.release()
        if restart_requested:
            _spawn_restarted_self(no_tray=args.no_tray)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
