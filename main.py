"""Entry point for Antigravity Monitor with High-DPI awareness and Win32 single-instance wakeup."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def _configure_stdio_utf8() -> None:
    """Prevent Windows GBK console encoding crashes on Unicode symbols (e.g. '▒', '█')."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio_utf8()

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


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Antigravity 实时缓存与健康度桌面监控器")
    parser.add_argument("--cli", action="store_true", help="仅运行单次命令行脱敏体检摘要并退出（默认零写盘）")
    parser.add_argument("--json", action="store_true", help="输出结构化脱敏 JSON 体检快照并退出（默认零写盘）")
    parser.add_argument("--readonly", action="store_true", help="严格零写盘模式（不更新 account_ledger.json，不执行任何清理）")
    parser.add_argument("--conversation-id", default="", help="定向附带指定对话（完整 UUID 或前缀）的详细状态")
    parser.add_argument("--account", default="", help="指定查看的脱敏账号标签（如 al***1、be***2，或 __all__ 查看全机）")
    parser.add_argument("--range", dest="history_range", default="30m", choices=["30m", "6h", "today", "7d"], help="指定观测时间范围（默认 30m 实时）")
    parser.add_argument("--clean-subagents", action="store_true", help="显式清理超过保护期的已结束老旧子代理以腾出 500 槽位（绝不触碰主对话）")
    parser.add_argument("--dry-run", action="store_true", help="配合 --clean-subagents 仅预览待清理条数与空间，不执行任何删改")
    parser.add_argument("--min-age", type=int, default=0, help="配合 --clean-subagents 指定子代理保护期（分钟，硬下限 30 分钟，0 表示沿用配置默认 120m）")
    parser.add_argument("--minimized", action="store_true", help="启动后静默最小化到系统托盘")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘（调试/截图模式）")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdio_utf8()
    args = _parse_args(argv)

    from core.config_store import ConfigStore
    from core.log_monitor import (
        AntigravityMonitorEngine,
        compute_cli_exit_code,
        format_json_snapshot,
        format_text_summary,
    )
    from core.slot_manager import format_bytes_human

    is_oneshot = bool(args.cli or args.json or args.clean_subagents)
    read_only = bool(args.readonly or (is_oneshot and not args.clean_subagents) or args.dry_run)

    try:
        config_store = ConfigStore(APP_ROOT / "config.json")
        cfg = config_store.load()
        engine = AntigravityMonitorEngine(config=cfg, read_only=read_only)
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {"schema_version": "1.0", "ok": False, "pressure": "offline_unknown", "exit_code": 2, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        if is_oneshot:
            print(f"Antigravity 监控初始化失败: {exc}", file=sys.stderr)
            return 2
        raise

    if args.clean_subagents:
        if args.readonly and not args.dry_run:
            if args.json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "dry_run": False,
                            "exit_code": 2,
                            "error": "readonly_conflicts_with_clean_subagents",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print("错误: --readonly 与实际清理互斥，如需预览请加 --dry-run", file=sys.stderr)
            return 2
        protect_min = args.min_age if args.min_age > 0 else int(cfg.get("slot_protect_age_min", 120))
        res = engine.clean_old_subagents(
            protect_age_min=protect_min,
            mode=str(cfg.get("slot_clean_mode", "recycle")),
            archive_dir=str(cfg.get("slot_archive_dir", "")),
            trigger="cli",
            dry_run=bool(args.dry_run),
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": not bool(res.error),
                        "dry_run": res.dry_run,
                        "mode": res.mode,
                        "protect_age_min": res.protect_age_min,
                        "candidate_count": res.candidate_count,
                        "removed_count": res.removed_count,
                        "freed_bytes": res.freed_bytes,
                        "skipped_locked_count": res.skipped_locked_count,
                        "skipped_changed_count": res.skipped_changed_count,
                        "remaining_db_count": res.remaining_db_count,
                        "archive_path": res.archive_path,
                        "error": res.error,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if res.error:
                print(f"子代理清理中止: {res.error}", file=sys.stderr)
            elif res.dry_run:
                print(
                    f"[预览模式] 保护期 {res.protect_age_min}m · 可安全清理 {res.candidate_count} 条老旧子代理"
                    f"（约 {format_bytes_human(res.freed_bytes)}），当前总槽位 {res.remaining_db_count}"
                )
            else:
                print(
                    f"清理完成（模式={res.mode}，保护期={res.protect_age_min}m）：移出 {res.removed_count}/{res.candidate_count} 条，"
                    f"释放 {format_bytes_human(res.freed_bytes)}，跳过占用 {res.skipped_locked_count} / 变动 {res.skipped_changed_count}，"
                    f"当前剩余 {res.remaining_db_count} 槽"
                )
        return 2 if res.error else 0

    if is_oneshot:
        try:
            snap = engine.poll(
                force_account=True,
                target_conversation_id=args.conversation_id,
                view_account=args.account,
                history_range=args.history_range,
            )
            if args.json:
                print(format_json_snapshot(snap))
            else:
                print(format_text_summary(snap))
            return compute_cli_exit_code(snap)
        except Exception as exc:
            if args.json:
                print(
                    json.dumps(
                        {"schema_version": "1.0", "ok": False, "pressure": "offline_unknown", "exit_code": 2, "error": str(exc)},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(f"Antigravity 实时采集失败: {exc}", file=sys.stderr)
            return 2

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
