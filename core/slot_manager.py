"""Read-only 500-slot conversation inventory monitor and guarded subagent cleanup engine.

Antigravity Desktop Language Server hard-prunes local conversations to 500 slots on startup
(shared between main conversations and ephemeral subagents/swarm workers). This module:
  1. Provides a fast, strictly read-only slot inventory scanner (never touches ``.db-shm`` mtime).
  2. Enforces 6-layer safety guards before moving/archiving/deleting any completed subagent.
  3. Hard-clamps the protection age floor at >= 30 minutes and alerts when main conversations
     approach eviction risk.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MIN_PROTECT_AGE_MIN: int = 30
DEFAULT_SLOT_CAP: int = 500
_HASH_BUF_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_CLEANUP_MUTEX_NAME = "Local\\AntigravityMonitorSlotCleanupMutex_v1"
_ISO_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
_PBTXT_TITLE_RE = re.compile(r'^\s*title\s*:\s*"((?:[^"\\]|\\.)+)"', re.MULTILINE)


def _is_reparse_or_symlink(path: Path) -> bool:
    """Detect POSIX/Windows symlinks as well as NTFS directory junctions / reparse points."""
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            st = os.lstat(path)
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except OSError:
        return False
    return False


def format_bytes_human(num_bytes: int) -> str:
    val = max(0, int(num_bytes))
    if val >= 1024 ** 3:
        return f"{val / (1024 ** 3):.2f} GiB"
    if val >= 1024 ** 2:
        return f"{val / (1024 ** 2):.1f} MiB"
    if val >= 1024:
        return f"{val / 1024:.0f} KiB"
    return f"{val} B"


def _normalize_numeric_epoch(val: float) -> Optional[float]:
    if val <= 0:
        return None
    if val >= 1e17:
        val /= 1e9  # nanoseconds
    elif val >= 1e14:
        val /= 1e6  # microseconds
    elif val >= 1e11:
        val /= 1e3  # milliseconds
    return val if val > 1e8 else None


def _parse_summary_timestamp_conservative(raw: Any, now_ts: float) -> Optional[float]:
    """Parse ``conversation_summaries.last_modified_time`` conservatively (never underestimate recency).

    Supports RFC3339Nano (up to 9 fractional digits), half-hour timezone offsets, and numeric epochs
    (s / ms / us / ns). Rejects booleans, date-only strings, and malformed strings (returning ``None``
    so the caller treats the entry conservatively as protected).
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return _normalize_numeric_epoch(float(raw))
    text = str(raw).strip()
    if not text:
        return None
    # Pure numeric timestamp string (e.g. "1795210000" / "1795210000123")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return _normalize_numeric_epoch(float(text))
        except ValueError:
            return None

    m = _ISO_TS_RE.match(text)
    if not m:
        return None
    date_part, time_part, frac_part, tz_part = m.groups()
    frac_norm = f".{frac_part[:6].ljust(6, '0')}" if frac_part else ""
    tz_norm = ""
    if tz_part:
        tz_u = tz_part.upper()
        if tz_u == "Z":
            tz_norm = "+00:00"
        elif len(tz_u) == 5 and tz_u[0] in "+-":
            tz_norm = f"{tz_u[:3]}:{tz_u[3:]}"
        else:
            tz_norm = tz_u
    try:
        dt = datetime.datetime.fromisoformat(f"{date_part}T{time_part}{frac_norm}{tz_norm}")
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.timestamp()
    ts_utc = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    ts_local = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo).timestamp()
    candidates = [t for t in (ts_utc, ts_local) if t <= now_ts + 120.0]
    if candidates:
        return max(candidates)
    return min(ts_utc, ts_local)


def _dir_tree_bytes(root_dir: Path, max_files: int = 4000) -> int:
    total = 0
    seen = 0
    try:
        if not root_dir.is_dir() or root_dir.is_symlink():
            return 0
        for dp, _dns, fns in os.walk(root_dir):
            for fn in fns:
                seen += 1
                if seen > max_files:
                    return total
                try:
                    total += os.lstat(os.path.join(dp, fn)).st_size
                except OSError:
                    pass
    except OSError:
        return total
    return total


def _sha256_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_BUF_BYTES)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


class _SHFILEOPSTRUCTW(ctypes.Structure):
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        _pack_ = 1
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def _is_volume_recycle_bin_nuke_enabled(abs_path: str) -> bool:
    """Check whether Windows Recycle Bin is disabled (NukeOnDelete=1) on the target drive."""
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(abs_path)[0]
    if not drive:
        return False
    try:
        import winreg  # type: ignore[import-not-found]
        root_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
        vol_buf = ctypes.create_unicode_buffer(128)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        if not k32.GetVolumeNameForVolumeMountPointW(drive + "\\", vol_buf, len(vol_buf)):
            return False
        vol_guid = vol_buf.value.strip("\\").split("Volume")[-1]
        if not vol_guid.startswith("{"):
            return False
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{root_key}\\{vol_guid}") as hkey:
            nuke_val, _ = winreg.QueryValueEx(hkey, "NukeOnDelete")
            return bool(int(nuke_val))
    except Exception:
        return False


def _max_tree_path_len(abs_path: str) -> int:
    max_len = len(abs_path)
    if not os.path.isdir(abs_path):
        return max_len
    try:
        for dp, dns, fns in os.walk(abs_path):
            if len(dp) > max_len:
                max_len = len(dp)
            for fn in fns:
                cand = len(dp) + 1 + len(fn)
                if cand > max_len:
                    max_len = cand
            for dn in dns:
                cand = len(dp) + 1 + len(dn)
                if cand > max_len:
                    max_len = cand
    except OSError:
        pass
    return max_len


def _win32_recycle_path(target: Path) -> Tuple[bool, str]:
    """Move a file or directory to the Windows Recycle Bin via SHFileOperationW (zero dependencies)."""
    if os.name != "nt":
        return False, "recycle_unsupported_non_windows"
    if _is_reparse_or_symlink(target):
        return False, "refuse_reparse_or_symlink"
    abs_str = os.path.abspath(str(target))
    if not os.path.exists(abs_str):
        return True, ""
    if _max_tree_path_len(abs_str) >= 248:
        return False, "path_exceeds_max_path_for_recycle"
    if _is_volume_recycle_bin_nuke_enabled(abs_str):
        return False, "recycle_bin_disabled_on_volume"
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        # Verify the volume Recycle Bin answers SHQueryRecycleBinW before invoking SHFileOperationW
        drive = os.path.splitdrive(abs_str)[0]
        if drive:
            shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO)]
            shell32.SHQueryRecycleBinW.restype = ctypes.c_long
            rb_info = _SHQUERYRBINFO()
            rb_info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
            if int(shell32.SHQueryRecycleBinW(drive + "\\", ctypes.byref(rb_info))) != 0:
                return False, "recycle_bin_unavailable_on_volume"
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        # pFrom MUST be double-null terminated
        p_from = ctypes.create_unicode_buffer(abs_str + "\0\0")
        op = _SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=FO_DELETE,
            pFrom=ctypes.cast(p_from, wintypes.LPCWSTR),
            pTo=None,
            fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        rc = int(shell32.SHFileOperationW(ctypes.byref(op)))
        if rc != 0 or op.fAnyOperationsAborted:
            return False, f"SHFileOperationW rc={rc} aborted={bool(op.fAnyOperationsAborted)}"
        if os.path.exists(abs_str):
            return False, "still_exists_after_recycle"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _acquire_cross_proc_cleanup_mutex() -> Tuple[bool, Optional[int]]:
    """Best-effort Win32 Named Mutex preventing concurrent CLI + GUI cleanup runs."""
    if os.name != "nt":
        return True, None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.CreateMutexW(None, False, _CLEANUP_MUTEX_NAME)
        if not handle:
            return True, None
        WAIT_OBJECT_0 = 0x00000000
        WAIT_ABANDONED = 0x00000080
        res = int(k32.WaitForSingleObject(handle, 0))
        if res in (WAIT_OBJECT_0, WAIT_ABANDONED):
            return True, int(handle)
        k32.CloseHandle(handle)
        return False, None
    except Exception:
        return True, None


def _release_cross_proc_cleanup_mutex(handle: Optional[int]) -> None:
    if os.name != "nt" or not handle:
        return
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.ReleaseMutex(wintypes.HANDLE(handle))
        k32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        pass


@dataclass(frozen=True)
class SlotConversationItem:
    conversation_id: str
    short_id: str
    category: str          # "main" | "cleanable_sub" | "protected_sub" | "unknown"
    title: str
    agent_name: str
    parent_conversation_id: str
    step_count: int
    status: str
    not_fully_idle: int
    disk_bytes: int
    last_active_ts: float
    age_min: float
    cleanable_at_30m: bool
    protect_reason: str = ""
    db_size: int = 0
    db_mtime_ns: int = 0
    wal_size: int = 0
    wal_mtime_ns: int = 0
    transcript_size: int = 0
    transcript_mtime_ns: int = 0


@dataclass
class SlotInventorySnapshot:
    observed_at: float
    cap: int = DEFAULT_SLOT_CAP
    total_db: int = 0
    remaining_slots: int = DEFAULT_SLOT_CAP
    main_count: int = 0
    protected_sub_count: int = 0
    cleanable_sub_count: int = 0
    cleanable_bytes: int = 0
    cleanable_30m_count: int = 0
    cleanable_30m_bytes: int = 0
    unknown_count: int = 0
    pinned_floor_count: int = 0
    protect_age_min: int = 120
    status: str = "ok"          # "ok" | "warn" | "danger" | "main_at_risk"
    main_at_risk: bool = False
    risk_message: str = ""
    summary_db_ok: bool = True
    summary_db_error: str = ""
    total_bytes: int = 0
    items: List[SlotConversationItem] = field(default_factory=list)
    last_auto_clean_summary: str = ""

    @property
    def usage_pct(self) -> float:
        return round((self.total_db / max(1, self.cap)) * 100.0, 1)

    @property
    def headroom(self) -> int:
        return self.remaining_slots

    @property
    def risk_headline(self) -> str:
        return self.risk_message


@dataclass
class SlotCleanupResult:
    started_at: float
    finished_at: float
    trigger: str                # "manual" | "auto_offline" | "auto_high_water" | "cli"
    mode: str                   # "recycle" | "archive" | "delete"
    protect_age_min: int
    dry_run: bool
    candidate_count: int = 0
    removed_count: int = 0
    freed_bytes: int = 0
    skipped_locked_count: int = 0
    skipped_changed_count: int = 0
    remaining_db_count: int = 0
    archive_path: str = ""
    error: str = ""
    removed_cids: List[str] = field(default_factory=list)


class SlotInventoryScanner:
    """Strictly read-only scanner for Antigravity's 500-slot local conversation pool."""

    def __init__(
        self,
        conversations_dir: Path,
        brain_dir: Path,
        summaries_db_path: Path,
        cache_ttl_sec: float = 15.0,
    ) -> None:
        self.conversations_dir = Path(conversations_dir)
        self.brain_dir = Path(brain_dir)
        self.summaries_db_path = Path(summaries_db_path)
        self.cache_ttl_sec = max(2.0, float(cache_ttl_sec))
        self._lock = threading.RLock()
        self._cached_snap: Optional[SlotInventorySnapshot] = None
        self._cached_key: Optional[Tuple[Any, ...]] = None
        self._cached_at: float = 0.0

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cached_snap = None
            self._cached_key = None
            self._cached_at = 0.0

    def _read_summaries_ro(self) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
        if not self.summaries_db_path.is_file():
            return False, {}
        conn: Optional[sqlite3.Connection] = None
        try:
            resolved = self.summaries_db_path.resolve().as_posix()
            encoded = urllib.parse.quote(resolved, safe="/:")
            uri = f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.6)
            cur = conn.cursor()
            cols = {str(r[1]).lower() for r in cur.execute("PRAGMA table_info(conversation_summaries)").fetchall()}
            if "conversation_id" not in cols:
                return False, {}

            def _col(name: str, default_expr: str) -> str:
                return name if name in cols else default_expr

            empty_str = "''"
            sql = (
                "SELECT conversation_id, "
                f"{_col('title', empty_str)}, "
                f"{_col('step_count', '0')}, "
                f"{_col('last_modified_time', empty_str)}, "
                f"{_col('status', empty_str)}, "
                f"{_col('agent_name', empty_str)}, "
                f"{_col('parent_conversation_id', empty_str)}, "
                f"{_col('nesting_depth', '0')}, "
                f"{_col('not_fully_idle', '0')} "
                "FROM conversation_summaries"
            )
            out: Dict[str, Dict[str, Any]] = {}
            for row in cur.execute(sql).fetchall():
                cid = str(row[0] or "").strip()
                if not cid:
                    continue
                out[cid] = {
                    "title": str(row[1] or "").strip(),
                    "step_count": int(row[2] or 0),
                    "last_modified_time": row[3],
                    "status": str(row[4] or "").strip(),
                    "agent_name": str(row[5] or "").strip(),
                    "parent_conversation_id": str(row[6] or "").strip(),
                    "nesting_depth": int(row[7] or 0),
                    "not_fully_idle": int(row[8] or 0),
                }
            return True, out
        except (sqlite3.Error, ValueError, TypeError):
            return False, {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _compute_light_stamp(
        self,
        protect_age_min: int,
        slot_cap: int,
        warn_threshold: int,
        alert_threshold: int,
    ) -> Tuple[Any, ...]:
        conv_mt = 0
        try:
            conv_mt = self.conversations_dir.stat().st_mtime_ns
        except OSError:
            pass
        sum_mt, sum_sz, wal_mt, wal_sz = 0, 0, 0, 0
        try:
            st = self.summaries_db_path.stat()
            sum_mt, sum_sz = st.st_mtime_ns, st.st_size
        except OSError:
            pass
        try:
            wst = Path(str(self.summaries_db_path) + "-wal").stat()
            wal_mt, wal_sz = wst.st_mtime_ns, wst.st_size
        except OSError:
            pass
        return (conv_mt, sum_mt, sum_sz, wal_mt, wal_sz, protect_age_min, slot_cap, warn_threshold, alert_threshold)

    def scan(
        self,
        now_ts: Optional[float] = None,
        protect_age_min: int = 120,
        slot_cap: int = DEFAULT_SLOT_CAP,
        warn_threshold: int = 380,
        alert_threshold: int = 450,
        include_items: bool = False,
        force: bool = False,
    ) -> SlotInventorySnapshot:
        now = float(now_ts if now_ts is not None else time.time())
        eff_protect = max(MIN_PROTECT_AGE_MIN, int(protect_age_min))
        cap = max(50, int(slot_cap))
        warn_th = max(10, min(int(warn_threshold), cap - 5))
        alert_th = max(warn_th + 1, min(int(alert_threshold), cap))

        with self._lock:
            stamp = self._compute_light_stamp(eff_protect, cap, warn_th, alert_th)
            if (
                not include_items
                and not force
                and self._cached_snap is not None
                and self._cached_key == stamp
                and (now - self._cached_at) < self.cache_ttl_sec
            ):
                return self._cached_snap

        db_entries: List[Tuple[str, str]] = []
        if self.conversations_dir.is_dir():
            try:
                with os.scandir(self.conversations_dir) as it:
                    for entry in it:
                        if entry.is_file(follow_symlinks=False) and entry.name.endswith(".db"):
                            if _is_reparse_or_symlink(Path(entry.path)):
                                continue
                            db_entries.append((entry.name[:-3], entry.path))
            except OSError:
                db_entries = []

        summary_ok, sum_map = self._read_summaries_ro()

        main_cnt = 0
        prot_sub_cnt = 0
        clean_cnt = 0
        clean_bytes = 0
        clean30_cnt = 0
        clean30_bytes = 0
        unknown_cnt = 0
        total_bytes = 0
        items_out: List[SlotConversationItem] = []

        brain_root_str = str(self.brain_dir)
        join = os.path.join
        lstat = os.lstat

        for cid, db_path_str in db_entries:
            try:
                db_st = lstat(db_path_str)
            except OSError:
                continue
            db_sz = int(db_st.st_size)
            db_mt_ns = int(db_st.st_mtime_ns)
            real_mt = float(db_st.st_mtime)

            # NEVER inspect .db-shm mtime (read-only scans / LS mmap refreshes touch .db-shm on hundreds of old DBs!)
            wal_path_str = db_path_str + "-wal"
            wal_sz = 0
            wal_mt_ns = 0
            try:
                wst = lstat(wal_path_str)
                wal_sz = int(wst.st_size)
                wal_mt_ns = int(wst.st_mtime_ns)
                if wal_sz > 0 and wst.st_mtime > real_mt:
                    real_mt = float(wst.st_mtime)
            except OSError:
                pass

            tr_path_str = join(brain_root_str, cid, ".system_generated", "logs", "transcript.jsonl")
            tr_sz = 0
            tr_mt_ns = 0
            try:
                tst = lstat(tr_path_str)
                tr_sz = int(tst.st_size)
                tr_mt_ns = int(tst.st_mtime_ns)
                if tst.st_mtime > real_mt:
                    real_mt = float(tst.st_mtime)
            except OSError:
                pass

            disk_age_min = max(0.0, (now - real_mt) / 60.0)
            est_bytes = db_sz + wal_sz + tr_sz
            if include_items:
                brain_bytes = _dir_tree_bytes(self.brain_dir / cid)
                est_bytes = db_sz + wal_sz + brain_bytes
            total_bytes += est_bytes

            info = sum_map.get(cid) if summary_ok else None
            if info is None:
                unknown_cnt += 1
                if include_items:
                    items_out.append(
                        SlotConversationItem(
                            conversation_id=cid,
                            short_id=cid[:6],
                            category="unknown",
                            title="",
                            agent_name="",
                            parent_conversation_id="",
                            step_count=0,
                            status="",
                            not_fully_idle=0,
                            disk_bytes=est_bytes,
                            last_active_ts=real_mt,
                            age_min=round(disk_age_min, 1),
                            cleanable_at_30m=False,
                            protect_reason="摘要库无登记（保守保护）" if summary_ok else "摘要库暂不可读（保守保护）",
                            db_size=db_sz,
                            db_mtime_ns=db_mt_ns,
                            wal_size=wal_sz,
                            wal_mtime_ns=wal_mt_ns,
                            transcript_size=tr_sz,
                            transcript_mtime_ns=tr_mt_ns,
                        )
                    )
                continue

            is_sub = bool(
                info["agent_name"]
                or info["parent_conversation_id"]
                or info["nesting_depth"] > 0
            )
            sum_ts = _parse_summary_timestamp_conservative(info["last_modified_time"], now)
            eff_ts = max(real_mt, sum_ts) if sum_ts is not None else real_mt
            eff_age_min = max(0.0, (now - eff_ts) / 60.0)
            sum_age_min = max(0.0, (now - sum_ts) / 60.0) if sum_ts is not None else 0.0
            # Require BOTH disk real activity age and summary last_modified_time age to exceed threshold
            min_age_min = min(disk_age_min, sum_age_min) if sum_ts is not None else 0.0

            if not is_sub:
                main_cnt += 1
                cat = "main"
                c30 = False
                reason = "主对话（永久保护）"
            else:
                idle_ok = (info["status"] == "CASCADE_RUN_STATUS_IDLE") and (info["not_fully_idle"] == 0)
                untitled_ok = not info["title"]
                c30 = idle_ok and untitled_ok and (min_age_min >= float(MIN_PROTECT_AGE_MIN))
                if c30:
                    clean30_cnt += 1
                    clean30_bytes += est_bytes
                if c30 and (min_age_min >= float(eff_protect)):
                    clean_cnt += 1
                    clean_bytes += est_bytes
                    cat = "cleanable_sub"
                    reason = f"已结束闲置 · {int(min_age_min)}分钟前"
                else:
                    prot_sub_cnt += 1
                    cat = "protected_sub"
                    if not idle_ok:
                        reason = f"运行中/未完全空闲 ({info['status'] or 'ACTIVE'})"
                    elif not untitled_ok:
                        reason = "具名对话（保守保护）"
                    elif min_age_min < float(MIN_PROTECT_AGE_MIN):
                        reason = f"30m 硬下限保护中 ({int(min_age_min)}分钟前)"
                    else:
                        reason = f"{eff_protect}m 保护期内 ({int(min_age_min)}分钟前)"

            if include_items:
                items_out.append(
                    SlotConversationItem(
                        conversation_id=cid,
                        short_id=cid[:6],
                        category=cat,
                        title=info["title"],
                        agent_name=info["agent_name"],
                        parent_conversation_id=info["parent_conversation_id"],
                        step_count=info["step_count"],
                        status=info["status"],
                        not_fully_idle=info["not_fully_idle"],
                        disk_bytes=est_bytes,
                        last_active_ts=eff_ts,
                        age_min=round(eff_age_min, 1),
                        cleanable_at_30m=c30,
                        protect_reason=reason,
                        db_size=db_sz,
                        db_mtime_ns=db_mt_ns,
                        wal_size=wal_sz,
                        wal_mtime_ns=wal_mt_ns,
                        transcript_size=tr_sz,
                        transcript_mtime_ns=tr_mt_ns,
                    )
                )

        total_db = len(db_entries)
        remaining = max(0, cap - total_db)
        pinned_floor = max(0, total_db - clean30_cnt)
        pinned_30m_sub = max(0, pinned_floor - main_cnt - unknown_cnt)
        cap_floor_margin = min(30, max(5, int(round(cap * 0.06))))
        near_cap_margin = min(15, max(3, int(round(cap * 0.03))))
        # Main conversations face real pruning danger ONLY when summary_db_ok is True AND hitting/nearing cap
        # while even cleaning everything down to the 30m hard floor still leaves the pool critically full.
        main_at_risk = bool(
            summary_ok
            and (
                (total_db >= alert_th and pinned_floor >= alert_th)
                or (total_db >= cap - near_cap_margin and clean30_cnt == 0)
                or (pinned_floor >= cap - cap_floor_margin)
            )
        )
        risk_msg = ""
        if not summary_ok:
            status = "warn" if total_db >= warn_th else "ok"
            if total_db >= warn_th:
                risk_msg = f"本机槽位 {total_db}/{cap}，但 conversation_summaries.db 暂不可读，已保守暂停自动识别清理"
        elif main_at_risk:
            status = "main_at_risk"
            parts = [f"主对话 {main_cnt}"]
            if pinned_30m_sub > 0:
                parts.append(f"30m内保护子代理 {pinned_30m_sub}")
            if unknown_cnt > 0:
                parts.append(f"未知 {unknown_cnt}")
            risk_msg = (
                f"已撞 30m 保护下限！不可清基底 {pinned_floor}/{cap}（{' / '.join(parts)}），"
                "重启 Antigravity 可能裁删老主对话，请先备份或归档主对话"
            )
        elif total_db >= alert_th:
            status = "danger"
            risk_msg = f"本机槽位已达 {total_db}/{cap}，建议尽快让位 {clean_cnt} 条历史子代理防重启裁剪"
        elif total_db >= warn_th:
            status = "warn"
            risk_msg = f"本机槽位 {total_db}/{cap} 偏高，当前有 {clean_cnt} 条超过 {eff_protect} 分钟的子代理可安全让位"
        else:
            status = "ok"

        if include_items:
            items_out.sort(key=lambda x: x.last_active_ts, reverse=True)

        snap = SlotInventorySnapshot(
            observed_at=now,
            cap=cap,
            total_db=total_db,
            remaining_slots=remaining,
            main_count=main_cnt,
            protected_sub_count=prot_sub_cnt,
            cleanable_sub_count=clean_cnt,
            cleanable_bytes=clean_bytes,
            cleanable_30m_count=clean30_cnt,
            cleanable_30m_bytes=clean30_bytes,
            unknown_count=unknown_cnt,
            pinned_floor_count=pinned_floor,
            protect_age_min=eff_protect,
            status=status,
            main_at_risk=main_at_risk,
            risk_message=risk_msg,
            summary_db_ok=summary_ok,
            summary_db_error="" if summary_ok else "conversation_summaries.db 缺失或暂不可读",
            total_bytes=total_bytes,
            items=items_out,
        )
        if not include_items:
            with self._lock:
                self._cached_snap = snap
                self._cached_key = stamp
                self._cached_at = now
        return snap


class SlotSubagentCleaner:
    """Explicit, 6-guard safe cleaner for completed historical subagents (never touches main chats)."""

    def __init__(
        self,
        scanner: SlotInventoryScanner,
        journal_path: Optional[Path] = None,
        recycle_fn: Optional[Callable[[Path], Tuple[bool, str]]] = None,
    ) -> None:
        self.scanner = scanner
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self._recycle_fn = recycle_fn or _win32_recycle_path
        self._clean_lock = threading.Lock()

    def _append_journal(self, entry: Dict[str, Any]) -> None:
        if self.journal_path is None:
            return
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _has_local_user_or_title_guard(conv_dir: Path, brain_dir: Path, cid: str) -> bool:
        """Extra defensive guard: refuse cleanup if annotations/<cid>.pbtxt has a title or transcript has USER_EXPLICIT."""
        ann_path = conv_dir.parent / "annotations" / f"{cid}.pbtxt"
        try:
            if ann_path.is_file() and not _is_reparse_or_symlink(ann_path):
                txt = ann_path.read_text(encoding="utf-8", errors="replace")[:4096]
                if _PBTXT_TITLE_RE.search(txt):
                    return True
        except OSError:
            pass
        tr_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            if tr_path.is_file() and not _is_reparse_or_symlink(tr_path):
                # Scan first 64 KiB for USER_EXPLICIT marker (always appears in opening user turn)
                with tr_path.open("rb") as fh:
                    head = fh.read(65536)
                if b'"USER_EXPLICIT"' in head:
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    def _archive_candidates_to_tar(
        tar_path: Path,
        conv_dir: Path,
        brain_dir: Path,
        candidates: List[SlotConversationItem],
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[bool, str, List[SlotConversationItem]]:
        """Atomically pack candidates into PAX tar (.tar.tmp -> SHA-256 re-read -> os.replace); skips unreadable single items."""
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_tar = tar_path.with_name(tar_path.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
        expected: Dict[str, Tuple[str, int]] = {}
        packed_items: List[SlotConversationItem] = []
        try:
            with tarfile.open(tmp_tar, "w", format=tarfile.PAX_FORMAT) as tf:
                for item in candidates:
                    if stop_event is not None and stop_event.is_set():
                        break
                    cid = item.conversation_id
                    if "/" in cid or "\\" in cid or ".." in cid:
                        continue
                    db_src = conv_dir / f"{cid}.db"
                    wal_src = conv_dir / f"{cid}.db-wal"
                    bp = brain_dir / cid
                    if _is_reparse_or_symlink(db_src) or _is_reparse_or_symlink(wal_src) or _is_reparse_or_symlink(bp):
                        continue
                    # Pre-collect and check all files for this candidate so a single bad item never aborts the whole batch
                    item_files: List[Tuple[Path, str]] = []
                    bad_item = False
                    for src, arc in ((db_src, f"conversations/{cid}.db"), (wal_src, f"conversations/{cid}.db-wal")):
                        if src.exists():
                            if _is_reparse_or_symlink(src) or not src.is_file():
                                bad_item = True
                                break
                            item_files.append((src, arc))
                    if not bad_item and bp.exists():
                        if _is_reparse_or_symlink(bp) or not bp.is_dir():
                            bad_item = True
                        else:
                            for dp, dns, fns in os.walk(bp):
                                dns.sort()
                                for d in list(dns):
                                    if _is_reparse_or_symlink(Path(dp) / d):
                                        bad_item = True
                                        break
                                if bad_item:
                                    break
                                for fn in sorted(fns):
                                    fp = Path(dp) / fn
                                    if _is_reparse_or_symlink(fp):
                                        bad_item = True
                                        break
                                    rel = os.path.relpath(str(fp), str(brain_dir)).replace("\\", "/")
                                    item_files.append((fp, f"brain/{rel}"))
                                if bad_item:
                                    break
                    if bad_item or not item_files:
                        continue
                    # Hash and add files for this candidate
                    item_hashes: List[Tuple[Path, str, str, int]] = []
                    try:
                        for fp, arc in item_files:
                            digest, sz = _sha256_file(fp)
                            item_hashes.append((fp, arc, digest, sz))
                    except OSError:
                        # Single locked/disappeared file -> skip this candidate safely
                        continue
                    for fp, arc, digest, sz in item_hashes:
                        tf.add(fp, arcname=arc, recursive=False)
                        expected[arc] = (digest, sz)
                    packed_items.append(item)
            if not packed_items:
                try:
                    tmp_tar.unlink(missing_ok=True)
                except OSError:
                    pass
                return True, "", []

            # Independent re-read verification against tmp_tar before publishing
            seen: set[str] = set()
            with tarfile.open(tmp_tar, "r") as tf:
                for member in tf:
                    if not member.isfile():
                        continue
                    exp = expected.get(member.name)
                    if exp is None:
                        raise RuntimeError(f"tar_unexpected_member:{member.name}")
                    seen.add(member.name)
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        raise RuntimeError(f"tar_unreadable:{member.name}")
                    h = hashlib.sha256()
                    sz = 0
                    while True:
                        chunk = fobj.read(_HASH_BUF_BYTES)
                        if not chunk:
                            break
                        h.update(chunk)
                        sz += len(chunk)
                    if sz != exp[1] or h.hexdigest() != exp[0]:
                        raise RuntimeError(f"tar_sha256_mismatch:{member.name}")
            if len(seen) != len(expected):
                raise RuntimeError(f"tar_missing_members:{len(expected) - len(seen)}")

            os.replace(tmp_tar, tar_path)
            return True, "", packed_items
        except Exception as exc:
            try:
                tmp_tar.unlink(missing_ok=True)
            except OSError:
                pass
            return False, f"tar_archive_failed:{exc}", []

    def _remove_single_path(self, target: Path, mode: str) -> Tuple[bool, str]:
        if _is_reparse_or_symlink(target):
            return False, "refuse_reparse_or_symlink"
        if not target.exists():
            return True, ""
        if mode == "recycle":
            return self._recycle_fn(target)
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                os.remove(target)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def execute_cleanup(
        self,
        protect_age_min: int = 120,
        mode: str = "recycle",
        archive_dir: str = "",
        trigger: str = "manual",
        dry_run: bool = False,
        now_ts: Optional[float] = None,
        slot_cap: int = DEFAULT_SLOT_CAP,
        max_items: int = 0,
        stop_event: Optional[threading.Event] = None,
    ) -> SlotCleanupResult:
        started = float(now_ts if now_ts is not None else time.time())
        eff_protect = max(MIN_PROTECT_AGE_MIN, int(protect_age_min))
        clean_mode = mode if mode in ("recycle", "archive", "delete") else "recycle"
        if clean_mode == "recycle" and os.name != "nt" and self._recycle_fn is _win32_recycle_path:
            return SlotCleanupResult(
                started_at=started,
                finished_at=time.time(),
                trigger=trigger,
                mode=clean_mode,
                protect_age_min=eff_protect,
                dry_run=dry_run,
                error="recycle_unsupported_on_this_os",
            )

        if not self._clean_lock.acquire(blocking=False):
            return SlotCleanupResult(
                started_at=started,
                finished_at=time.time(),
                trigger=trigger,
                mode=clean_mode,
                protect_age_min=eff_protect,
                dry_run=dry_run,
                error="cleanup_already_running",
            )

        cross_mutex_handle: Optional[int] = None
        if not dry_run:
            got_cross, cross_mutex_handle = _acquire_cross_proc_cleanup_mutex()
            if not got_cross:
                self._clean_lock.release()
                return SlotCleanupResult(
                    started_at=started,
                    finished_at=time.time(),
                    trigger=trigger,
                    mode=clean_mode,
                    protect_age_min=eff_protect,
                    dry_run=dry_run,
                    error="cleanup_already_running_other_process",
                )

        try:
            inv = self.scanner.scan(
                now_ts=started,
                protect_age_min=eff_protect,
                slot_cap=slot_cap,
                include_items=True,
                force=True,
            )
            if not inv.summary_db_ok:
                return SlotCleanupResult(
                    started_at=started,
                    finished_at=time.time(),
                    trigger=trigger,
                    mode=clean_mode,
                    protect_age_min=eff_protect,
                    dry_run=dry_run,
                    remaining_db_count=inv.total_db,
                    error="conversation_summaries_db_unavailable",
                )

            # Oldest completed subagents first
            candidates = sorted(
                (it for it in inv.items if it.category == "cleanable_sub"),
                key=lambda it: it.last_active_ts,
            )
            if max_items > 0:
                candidates = candidates[: int(max_items)]

            res = SlotCleanupResult(
                started_at=started,
                finished_at=started,
                trigger=trigger,
                mode=clean_mode,
                protect_age_min=eff_protect,
                dry_run=dry_run,
                candidate_count=len(candidates),
                remaining_db_count=inv.total_db,
            )
            if not candidates or dry_run:
                res.freed_bytes = sum(it.disk_bytes for it in candidates) if dry_run else 0
                res.finished_at = time.time()
                return res

            conv_dir_raw = self.scanner.conversations_dir
            brain_dir_raw = self.scanner.brain_dir
            conv_dir = conv_dir_raw.resolve()
            brain_dir = brain_dir_raw.resolve()

            # Pre-filter candidates with local annotations/USER_EXPLICIT extra defense BEFORE archiving or removing
            safe_candidates: List[SlotConversationItem] = []
            for item in candidates:
                if self._has_local_user_or_title_guard(conv_dir, brain_dir, item.conversation_id):
                    res.skipped_changed_count += 1
                    continue
                safe_candidates.append(item)
            candidates = safe_candidates
            if not candidates:
                res.finished_at = time.time()
                return res

            archive_tar_str = ""
            if clean_mode == "archive":
                arch_root = Path(archive_dir.strip()) if archive_dir.strip() else None
                if arch_root is None:
                    res.error = "archive_dir_not_configured"
                    res.finished_at = time.time()
                    return res
                # Refuse to proceed if external drive/root does not exist
                if arch_root.drive and not Path(arch_root.drive + os.sep).exists():
                    res.error = f"archive_drive_missing:{arch_root.drive}"
                    res.finished_at = time.time()
                    return res
                stamp_s = datetime.datetime.fromtimestamp(started).strftime("%Y%m%d_%H%M%S")
                uniq = uuid.uuid4().hex[:6]
                tar_path = arch_root / f"subagents_archive_{stamp_s}_{eff_protect}m_{len(candidates)}items_{uniq}.tar"
                ok_tar, tar_err, packed_items = self._archive_candidates_to_tar(
                    tar_path=tar_path,
                    conv_dir=conv_dir,
                    brain_dir=brain_dir,
                    candidates=candidates,
                    stop_event=stop_event,
                )
                if not ok_tar:
                    res.error = tar_err
                    res.finished_at = time.time()
                    return res
                if not packed_items:
                    res.skipped_locked_count += len(candidates)
                    res.finished_at = time.time()
                    return res
                skipped_during_tar = len(candidates) - len(packed_items)
                if skipped_during_tar > 0:
                    res.skipped_locked_count += skipped_during_tar
                candidates = packed_items
                archive_tar_str = str(tar_path)
                res.archive_path = archive_tar_str

            # Second live re-verification pass immediately prior to removal
            ok_live, live_sum = self.scanner._read_summaries_ro()
            if not ok_live:
                res.error = "live_summary_recheck_failed"
                res.finished_at = time.time()
                return res

            remove_mode = "delete" if clean_mode == "archive" else clean_mode
            check_now = float(now_ts if now_ts is not None else time.time())
            brain_fail_count = 0

            for item in candidates:
                if stop_event is not None and stop_event.is_set():
                    break
                cid = item.conversation_id
                if "/" in cid or "\\" in cid or ".." in cid:
                    res.skipped_changed_count += 1
                    continue
                live_info = live_sum.get(cid)
                if not live_info:
                    res.skipped_changed_count += 1
                    continue
                is_sub = bool(
                    live_info["agent_name"]
                    or live_info["parent_conversation_id"]
                    or live_info["nesting_depth"] > 0
                )
                if (
                    (not is_sub)
                    or live_info["title"]
                    or live_info["status"] != "CASCADE_RUN_STATUS_IDLE"
                    or live_info["not_fully_idle"] != 0
                ):
                    res.skipped_changed_count += 1
                    continue

                # Re-verify summary last_modified_time age against eff_protect (catches wakeups during tar/planning)
                live_sum_ts = _parse_summary_timestamp_conservative(live_info["last_modified_time"], check_now)
                if live_sum_ts is None or max(0.0, (check_now - live_sum_ts) / 60.0) < float(eff_protect):
                    res.skipped_changed_count += 1
                    continue

                # Construct UNRESOLVED paths FIRST so symlink / NTFS Junction checks inspect the link itself!
                db_path = conv_dir / f"{cid}.db"
                wal_path = conv_dir / f"{cid}.db-wal"
                shm_path = conv_dir / f"{cid}.db-shm"
                bp_path = brain_dir / cid
                if (
                    db_path.parent != conv_dir
                    or wal_path.parent != conv_dir
                    or shm_path.parent != conv_dir
                    or bp_path.parent != brain_dir
                    or _is_reparse_or_symlink(db_path)
                    or _is_reparse_or_symlink(wal_path)
                    or _is_reparse_or_symlink(shm_path)
                    or _is_reparse_or_symlink(bp_path)
                ):
                    res.skipped_changed_count += 1
                    continue

                # Ensure source files did not mutate or vanish unexpectedly since planning
                try:
                    cur_db = os.lstat(db_path)
                    if cur_db.st_size != item.db_size or cur_db.st_mtime_ns != item.db_mtime_ns:
                        res.skipped_changed_count += 1
                        continue
                except OSError:
                    res.skipped_changed_count += 1
                    continue

                if wal_path.exists():
                    try:
                        cur_wal = os.lstat(wal_path)
                        if cur_wal.st_size != item.wal_size or (cur_wal.st_size > 0 and cur_wal.st_mtime_ns != item.wal_mtime_ns):
                            res.skipped_changed_count += 1
                            continue
                    except OSError:
                        res.skipped_changed_count += 1
                        continue
                elif item.wal_size > 0:
                    res.skipped_changed_count += 1
                    continue

                tr_path = bp_path / ".system_generated" / "logs" / "transcript.jsonl"
                if tr_path.exists():
                    if _is_reparse_or_symlink(tr_path):
                        res.skipped_changed_count += 1
                        continue
                    try:
                        cur_tr = os.lstat(tr_path)
                        if cur_tr.st_size != item.transcript_size or cur_tr.st_mtime_ns != item.transcript_mtime_ns:
                            res.skipped_changed_count += 1
                            continue
                    except OSError:
                        res.skipped_changed_count += 1
                        continue
                elif item.transcript_size > 0:
                    res.skipped_changed_count += 1
                    continue

                # Step 1: Try removing/recycling .db FIRST. If LS holds an open handle on Windows, fail cleanly and skip!
                ok_db, _err_db = self._remove_single_path(db_path, remove_mode)
                if not ok_db:
                    res.skipped_locked_count += 1
                    continue

                # Step 2: Best-effort remove/recycle .db-wal, .db-shm and brain/<cid>/
                freed_this = item.db_size
                for aux, aux_sz in ((wal_path, item.wal_size), (shm_path, 0)):
                    if aux.exists() and not _is_reparse_or_symlink(aux):
                        ok_aux, _ = self._remove_single_path(aux, remove_mode)
                        if ok_aux:
                            freed_this += aux_sz
                brain_expected = max(0, item.disk_bytes - item.db_size - item.wal_size)
                if bp_path.exists() and bp_path.is_dir() and not _is_reparse_or_symlink(bp_path):
                    ok_bp, _ = self._remove_single_path(bp_path, remove_mode)
                    if ok_bp:
                        freed_this += brain_expected
                    else:
                        brain_fail_count += 1
                else:
                    freed_this += brain_expected

                res.removed_count += 1
                res.freed_bytes += freed_this
                res.removed_cids.append(cid)

            self.scanner.invalidate_cache()
            post_cnt = 0
            if conv_dir.is_dir():
                try:
                    post_cnt = sum(
                        1
                        for e in os.scandir(conv_dir)
                        if e.is_file(follow_symlinks=False) and e.name.endswith(".db") and not _is_reparse_or_symlink(Path(e.path))
                    )
                except OSError:
                    post_cnt = max(0, inv.total_db - res.removed_count)
            res.remaining_db_count = post_cnt
            res.finished_at = time.time()

            self._append_journal(
                {
                    "at": datetime.datetime.fromtimestamp(res.finished_at).astimezone().isoformat(timespec="seconds"),
                    "trigger": trigger,
                    "mode": clean_mode,
                    "protect_age_min": eff_protect,
                    "candidates": res.candidate_count,
                    "removed": res.removed_count,
                    "freed_bytes": res.freed_bytes,
                    "skipped_locked": res.skipped_locked_count,
                    "skipped_changed": res.skipped_changed_count,
                    "brain_remove_failed": brain_fail_count,
                    "remaining_db": res.remaining_db_count,
                    "archive_path": archive_tar_str,
                }
            )
            return res
        finally:
            _release_cross_proc_cleanup_mutex(cross_mutex_handle)
            self._clean_lock.release()
