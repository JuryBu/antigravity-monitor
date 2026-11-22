"""Entry point for Antigravity Monitor with High-DPI awareness and Win32 single-instance wakeup."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

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

    def acquire(self, wait_timeout_sec: float = 0.0) -> bool:
        if os.name != "nt" or self._k32 is None:
            return True
        deadline = time.monotonic() + max(0.0, float(wait_timeout_sec))
        while True:
            ctypes.set_last_error(0)
            handle = self._k32.CreateMutexW(None, False, MUTEX_NAME)
            last_err = ctypes.get_last_error()
            if not handle:
                return True
            self.mutex_handle = int(handle)
            if last_err != ERROR_ALREADY_EXISTS:
                ev = self._k32.CreateEventW(None, False, False, WAKE_EVENT_NAME)
                self.event_handle = int(ev) if ev else None
                return True
            if wait_timeout_sec > 0.0 and time.monotonic() < deadline:
                self.release()
                time.sleep(0.1)
                continue
            self.already_running = True
            self._signal_existing_instance()
            self.release()
            return False

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


JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS = 2
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
RESTART_ERROR_LOG_MAX_BYTES = 64 * 1024
_WMI_RESULT_RE = re.compile(r"(?m)^\s*RV=(-?\d+);PID=(\d*)\s*$")


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """Win32 JOBOBJECT_BASIC_LIMIT_INFORMATION (64 bytes on x64, 48 bytes on x86)."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _USEROBJECTFLAGS(ctypes.Structure):
    _fields_ = [
        ("fInherit", wintypes.BOOL),
        ("fReserved", wintypes.BOOL),
        ("dwFlags", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class JobLimitInfo:
    in_job: bool = False
    kill_on_close: bool = False
    breakaway_ok: bool = False
    query_failed: bool = False


def _inspect_current_job_limits() -> JobLimitInfo:
    """Inspect whether the current process is inside a Win32 Job Object and its breakaway/kill flags."""
    if os.name != "nt":
        return JobLimitInfo(in_job=False, kill_on_close=False, breakaway_ok=False, query_failed=False)
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryInformationJobObject.restype = wintypes.BOOL

        in_job = wintypes.BOOL(0)
        if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(in_job)):
            return JobLimitInfo(in_job=False, kill_on_close=False, breakaway_ok=False, query_failed=True)
        if not bool(in_job.value):
            return JobLimitInfo(in_job=False, kill_on_close=False, breakaway_ok=False, query_failed=False)
    except Exception:
        return JobLimitInfo(in_job=False, kill_on_close=False, breakaway_ok=False, query_failed=True)

    try:
        info = _JOBOBJECT_BASIC_LIMIT_INFORMATION()
        ret_len = wintypes.DWORD(0)
        ok = k32.QueryInformationJobObject(
            None,
            JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(ret_len),
        )
        if not ok:
            return JobLimitInfo(in_job=True, kill_on_close=True, breakaway_ok=False, query_failed=True)
        limit_flags = int(info.LimitFlags)
        kill_on_close = bool(limit_flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        breakaway_ok = bool(
            limit_flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
        )
        return JobLimitInfo(
            in_job=True,
            kill_on_close=kill_on_close,
            breakaway_ok=breakaway_ok,
            query_failed=False,
        )
    except Exception:
        return JobLimitInfo(in_job=True, kill_on_close=True, breakaway_ok=False, query_failed=True)


def _ps_quote(s: str) -> str:
    """Escape a string as a PowerShell single-quoted literal (including PS 5.1 Unicode smart-quote delimiters)."""
    out = str(s)
    for ch in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        if ch in out:
            out = out.replace(ch, ch + ch)
    return "'" + out + "'"


def _decode_process_output(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    raw = bytes(data)
    for enc in ("utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _spawn_via_wmi(cmd: list[str], cwd: Path, timeout_sec: float = 8.0) -> Tuple[bool, str]:
    """Spawn a detached process outside a strict KILL_ON_JOB_CLOSE Job Object via Win32_Process.Create."""
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    ps_candidate = Path(sys_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ps_exe = str(ps_candidate) if ps_candidate.is_file() else "powershell.exe"

    cmdline = subprocess.list2cmdline(cmd)
    cwd_str = str(cwd)
    ps_script = "\n".join(
        [
            "$ProgressPreference = 'SilentlyContinue'",
            "$ErrorActionPreference = 'Stop'",
            "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }",
            (
                "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ "
                f"CommandLine = {_ps_quote(cmdline)}; "
                f"CurrentDirectory = {_ps_quote(cwd_str)}; "
                "ProcessStartupInformation = $si }"
            ),
            'Write-Output "RV=$($r.ReturnValue);PID=$($r.ProcessId)"',
        ]
    )
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    ps_cmd = [
        ps_exe,
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-EncodedCommand",
        encoded,
    ]

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc: Optional[subprocess.Popen[Any]] = None
    try:
        proc = subprocess.Popen(ps_cmd, **popen_kwargs)
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
        return False, f"WMI spawn timed out after {timeout_sec:.1f}s"
    except Exception as exc:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)
            except Exception:
                pass
        return False, f"WMI helper launch failed: {exc}"
    finally:
        if proc is not None:
            for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    stdout_text = _decode_process_output(stdout_raw)
    stderr_text = _decode_process_output(stderr_raw).strip()
    if proc.returncode != 0:
        detail = stderr_text or stdout_text.strip() or f"exit_code={proc.returncode}"
        return False, f"PowerShell exited with code {proc.returncode}: {detail}"

    match = _WMI_RESULT_RE.search(stdout_text)
    if match is None:
        detail = stderr_text or stdout_text.strip() or "empty stdout"
        return False, f"Unexpected WMI output: {detail}"

    rv = int(match.group(1))
    pid_str = match.group(2)
    pid = int(pid_str) if pid_str.isdigit() else 0
    if rv != 0 or pid <= 0:
        return False, f"Win32_Process.Create failed (RV={rv}, PID={pid_str or 'none'})"
    return True, f"RV=0;PID={pid}"


def _append_restart_error_log(message: str, max_bytes: int = RESTART_ERROR_LOG_MAX_BYTES) -> None:
    """Append a timestamped restart error entry to logs/restart_error.log capped at max_bytes."""
    log_path = APP_ROOT / "logs" / "restart_error.log"
    tmp_path = log_path.with_name(f".{log_path.name}.{os.getpid()}.tmp")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        clean_msg = " ".join(str(message).splitlines()).strip()
        entry_bytes = f"[{ts}] {clean_msg}\n".encode("utf-8", errors="replace")
        existing = log_path.read_bytes() if log_path.is_file() else b""
        combined = existing + entry_bytes
        if len(combined) > max_bytes:
            tail = combined[-max_bytes:]
            if len(entry_bytes) < max_bytes:
                nl = tail.find(b"\n")
                if 0 <= nl < len(tail) - len(entry_bytes):
                    tail = tail[nl + 1 :]
            tail = tail.decode("utf-8", errors="ignore").encode("utf-8")
            combined = tail[-max_bytes:]
        tmp_path.write_bytes(combined)
        os.replace(tmp_path, log_path)
    except Exception:
        pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _is_windows_interactive_session() -> bool:
    """Return False when running in a non-interactive Windows station (e.g. Session 0 service)."""
    if os.name != "nt":
        return False
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetProcessWindowStation.argtypes = []
        u32.GetProcessWindowStation.restype = wintypes.HANDLE
        u32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        u32.GetUserObjectInformationW.restype = wintypes.BOOL
        hwinsta = u32.GetProcessWindowStation()
        if not hwinsta:
            return True
        flags = _USEROBJECTFLAGS()
        needed = wintypes.DWORD(0)
        UOI_FLAGS = 1
        WSF_VISIBLE = 0x0001
        if u32.GetUserObjectInformationW(hwinsta, UOI_FLAGS, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed)):
            return bool(flags.dwFlags & WSF_VISIBLE)
    except Exception:
        pass
    return True


def _default_restart_alert(msg: str) -> None:
    """Show a native Win32 error box on restart failure when running interactively."""
    if os.name != "nt" or not _is_windows_interactive_session():
        return
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        u32.MessageBoxW.restype = ctypes.c_int
        MB_OK = 0x00000000
        MB_ICONERROR = 0x00000010
        MB_TOPMOST = 0x00040000
        u32.MessageBoxW(None, msg, "Antigravity 监控器 - 重启失败", MB_OK | MB_ICONERROR | MB_TOPMOST)
    except Exception:
        pass


_RESTART_ALERT_FN: Optional[Callable[[str], None]] = _default_restart_alert


def _notify_restart_failure(msg: str) -> None:
    """Invoke the injectable restart failure alert hook without raising."""
    fn = _RESTART_ALERT_FN
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        pass


def _job_flag(job_info: Any, key: str, default: bool = False) -> bool:
    if isinstance(job_info, dict):
        return bool(job_info.get(key, default))
    return bool(getattr(job_info, key, default))


def _build_restart_popen_kwargs(env: dict[str, str], creationflags: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(APP_ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = creationflags
    return kwargs


def _spawn_restarted_self(no_tray: bool = False, minimized: bool = False, readonly: bool = False) -> bool:
    """Relaunch the monitor robustly across normal desktop, breakaway Job, and strict KILL_ON_JOB_CLOSE Job environments."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    launcher = str(pyw) if os.name == "nt" and pyw.is_file() else str(exe)

    cmd: list[str] = [launcher]
    if sys.version_info < (3, 10):
        cmd.extend(["-X", "utf8=0"])
    cmd.extend([str(APP_ROOT / "main.py"), "--restarting"])
    if no_tray:
        cmd.append("--no-tray")
    if minimized:
        cmd.append("--minimized")
    if readonly:
        cmd.append("--readonly")

    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)

    base_flags = 0
    if os.name == "nt":
        base_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    job_info = _inspect_current_job_limits()
    kill_on_close = _job_flag(job_info, "kill_on_close")
    breakaway_ok = _job_flag(job_info, "breakaway_ok")

    errors: list[str] = []

    if not kill_on_close:
        # Branch A: normal desktop / VBS / non-KILL_ON_JOB_CLOSE launch.
        try:
            subprocess.Popen(cmd, **_build_restart_popen_kwargs(env, base_flags))
            return True
        except Exception as exc:
            errors.append(f"direct Popen failed: {exc}")
    else:
        if breakaway_ok:
            # Branch B: KILL_ON_JOB_CLOSE with breakaway allowed; fall through to Branch C on OSError/PermissionError.
            try:
                subprocess.Popen(cmd, **_build_restart_popen_kwargs(env, base_flags | CREATE_BREAKAWAY_FROM_JOB))
                return True
            except (PermissionError, OSError) as exc:
                errors.append(f"breakaway Popen failed: {exc}")
            except Exception as exc:
                errors.append(f"breakaway Popen unexpected error: {exc}")

        # Branch C: strict non-breakaway KILL_ON_JOB_CLOSE Job (or Branch B fallback).
        ok, detail = _spawn_via_wmi(cmd, APP_ROOT)
        if ok:
            return True
        errors.append(f"WMI escape failed: {detail}")

    err_summary = "; ".join(errors) if errors else "unknown restart error"
    err_msg = f"自动重启 Antigravity 监控器失败，请手动重新启动。\n原因: {err_summary}"
    if not readonly:
        _append_restart_error_log(err_summary)
    _notify_restart_failure(err_msg)
    return False


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Antigravity 实时缓存与健康度桌面监控器")
    parser.add_argument("--version", action="store_true", help="输出当前版本号并退出")
    parser.add_argument("--check-update", action="store_true", help="检查 GitHub 远端是否有新版本并退出")
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
    parser.add_argument("--restarting", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdio_utf8()
    args = _parse_args(argv)

    from core.version import read_local_version_info

    if args.version:
        info = read_local_version_info(APP_ROOT)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"Antigravity Monitor v{info['version']} ({info['repo']}@{info['branch']})")
        return 0

    if args.check_update:
        from core.updater import UpdateManager

        updater = UpdateManager(app_root=APP_ROOT)
        res = updater.check_for_update(is_manual=True)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": not bool(res.error),
                        "local_version": res.local_version,
                        "remote_version": res.remote_version,
                        "has_update": res.has_update,
                        "summary": res.summary,
                        "released_at": res.released_at,
                        "install_mode": res.install_mode,
                        "error": res.error,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if res.error:
                print(res.error, file=sys.stderr)
            elif res.has_update:
                print(f"发现新版本 v{res.remote_version}（当前 v{res.local_version}，安装模式={res.install_mode}）：{res.summary}")
            else:
                print(f"当前已是最新版本 v{res.local_version}（安装模式={res.install_mode}）")
        return 2 if res.error else 0

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
    if not guard.acquire(wait_timeout_sec=12.0 if args.restarting else 0.0):
        return 0

    from core.updater import UpdateManager
    from ui.capsule_app import AntigravityCapsuleApp

    restart_requested = False

    def _mark_restart() -> None:
        nonlocal restart_requested
        restart_requested = True

    updater = UpdateManager(app_root=APP_ROOT)
    wake_stop = threading.Event()
    app = AntigravityCapsuleApp(
        app_root=APP_ROOT,
        config_store=config_store,
        engine=engine,
        enable_tray=not args.no_tray,
        start_minimized_override=(True if args.minimized else (False if args.restarting else None)),
        on_restart=_mark_restart,
        update_manager=updater,
    )
    wake_thread = guard.start_wakeup_listener(wake_stop, lambda: app.dispatch_to_ui(app.show_window))
    try:
        app.run()
    finally:
        restart_minimized = bool((not args.no_tray) and (not app._visible))
        wake_stop.set()
        guard.signal_stop()
        if wake_thread is not None and wake_thread.is_alive():
            wake_thread.join(timeout=1.0)
        guard.release()
        if restart_requested:
            _spawn_restarted_self(
                no_tray=args.no_tray,
                minimized=restart_minimized,
                readonly=bool(args.readonly),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
