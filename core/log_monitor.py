"""Pure read-only telemetry engine for Antigravity desktop monitor.

Reads only local files and localhost Language Server status endpoints:
  1. ~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl
  2. ~/.gemini/antigravity/conversations/<id>.db  (read-only SQLite + WAL stamp cache)
  3. %APPDATA%/Antigravity/logs/language_server.log (incremental seek + rotation guard)
  4. ~/.gemini/antigravity/daemon/hub_*.json (localhost 127.0.0.1 read-only GetUserStatus)
     + ~/.antigravity_cockpit/platform-quota-history-v1.sqlite (optional read-only fallback)
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from core.account_ledger import AccountSwitchLedger, _mask_short
from core.config_store import DEFAULT_CONFIG, sanitize_config
from core.slot_manager import (
    MIN_PROTECT_AGE_MIN,
    SlotCleanupResult,
    SlotInventoryScanner,
    SlotInventorySnapshot,
    SlotSubagentCleaner,
    format_bytes_human,
)
from core.sqlite_reader import ConversationDbReader, ConversationDbSnapshot, parse_pb_map

EMAIL_RE = re.compile(r"(?<!\*)\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
RETRY_RE = re.compile(r"(?:^|[\s\[/\\])run\.go:\d+\].*attempt\s+\d+\s+failed", re.IGNORECASE)
# Strip Go/glog wrapper prefix so timestamp microseconds (e.g. .350309) and "ERROR: logging before google.Init:" never false-match!
GLOG_PREFIX_RE = re.compile(
    r"^(?:ERROR:\s+logging before google\.Init:\s+)?([IWEF])(\d{2})(\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.\d+)?\s+\d+\s+([^\]]+)\]\s*(.*)$"
)
# Supports both ISO-8601 (YYYY-MM-DD...) and Go stdlog slash dates (YYYY/MM/DD HH:MM:SS)
FULL_TS_RE = re.compile(
    r"^.{0,12}?(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*(.*)$"
)
# Strict upstream HTTP/gRPC error patterns (evaluated ONLY against stripped message body)
HTTP_503_RE = re.compile(
    r"(?:no capacity|resource_exhausted|\b(?:http(?:\s+status)?|status(?:\s+code)?|error\s+code|code)\s*[:=]?\s*503\b|\b503\s+(?:service\s+)?unavailable\b)",
    re.IGNORECASE,
)
STREAM_TIMEOUT_RE = re.compile(
    r"(?:stream idle timeout(?:\s*\((\d+(?:\.\d+)?(?:h|m|s|ms)(?:[0-9.]*(?:h|m|s|ms))*)\))?|timed out after\s+(\d+(?:\.\d+)?(?:h|m|s|ms)(?:[0-9.]*(?:h|m|s|ms))*))",
    re.IGNORECASE,
)
_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)", re.IGNORECASE)


def _parse_duration_to_sec(raw: str) -> Optional[float]:
    if not raw:
        return None
    total = 0.0
    matched = False
    for num_s, unit in _DURATION_TOKEN_RE.findall(raw):
        matched = True
        val = float(num_s)
        u = unit.lower()
        if u == "h":
            total += val * 3600.0
        elif u == "m":
            total += val * 60.0
        elif u == "s":
            total += val
        elif u == "ms":
            total += val / 1000.0
    return total if matched else None


def _is_light_immune_model(model_name: str) -> bool:
    """Lightweight models (Hydrogen / Flash) are largely immune to flagship T1/T2 throttling and must not whitewash flagship T2."""
    low = (model_name or "").lower()
    return ("hydrogen" in low) or ("flash" in low)
TOS_403_RE = re.compile(
    r"(?:tos_violation|permission_denied[^a-z0-9]{0,16}(?:code\s*[:=]?\s*)?403\b|(?:status|http|code)[^0-9]{0,12}403\b.*permission_denied)",
    re.IGNORECASE,
)
IGNORED_LOG_SOURCES = (
    "extract_searchable_content.go",
    "llm_monitor.go",
    "runner.go",
    "code_assist_client.go",
    "conversationsearch",
)

MODEL_SHORT_MAP = (
    ("gemini-4-neon-xhigh", "Neon XHigh"),
    ("gemini-4-neon-high", "Neon High"),
    ("gemini-4-neon-medium", "Neon Med"),
    ("gemini-4-neon-low", "Neon Low"),
    ("gemini-4-neon", "Neon"),
    ("gemini-4-carbon-high", "Carbon High"),
    ("gemini-4-carbon-medium", "Carbon Med"),
    ("gemini-4-carbon-low", "Carbon Low"),
    ("gemini-4-carbon", "Carbon"),
    ("gemini-4-hydrogen-xhigh", "Hydrogen XH"),
    ("gemini-4-hydrogen-tiered-xhigh", "Hydrogen XH"),
    ("gemini-4-hydrogen-high", "Hydrogen H"),
    ("gemini-4-hydrogen-tiered-high", "Hydrogen H"),
    ("gemini-4-hydrogen", "Hydrogen"),
    ("gemini-4-argon-high", "Argon High"),
    ("gemini-4-argon", "Argon"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-fable", "Fable"),
)


def mask_email(text: str) -> str:
    """Mask every email address in a string (e.g. alice123@example.com -> al***3@example.com)."""
    if not text:
        return ""

    def _repl(match: re.Match[str]) -> str:
        user, domain = match.group(1), match.group(2)
        if "***" in user:
            return match.group(0)
        return f"{_mask_identifier(user)}@{domain}"

    return EMAIL_RE.sub(_repl, text)


def _mask_identifier(ident: str) -> str:
    clean = ident.strip()
    if not clean:
        return ""
    if len(clean) == 1:
        return "*"
    if len(clean) == 2:
        return f"{clean[0]}***"
    if len(clean) == 3:
        return f"{clean[:2]}***"
    return f"{clean[:2]}***{clean[-1]}"


def short_email_label(email: str) -> str:
    """Short masked label for compact HUD display (masks both email & plain usernames)."""
    if not email or not email.strip():
        return "离线"
    masked = mask_email(email.strip())
    if "@" in masked:
        return masked.split("@", 1)[0]
    return _mask_identifier(masked) or "离线"


def format_model_name(raw: str) -> str:
    if not raw:
        return "未知模型"
    low = raw.strip().lower()
    for prefix, short in MODEL_SHORT_MAP:
        if prefix in low:
            return short
    if len(raw) > 18:
        return raw[:17] + "…"
    return raw


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def format_delta_tokens(delta: int) -> str:
    if delta == 0:
        return "+0"
    sign = "+" if delta > 0 else "-"
    mag = abs(delta)
    if mag >= 1_000_000:
        return f"{sign}{mag / 1_000_000:.2f}M"
    if mag >= 1_000:
        return f"{sign}{mag / 1_000:.1f}K"
    return f"{sign}{mag}"


def _epoch_to_iso_utc(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_iso_local(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _normalize_reset_time(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val <= 0:
            return None
        sec = val / 1000.0 if val > 1e11 else val
        try:
            return _epoch_to_iso_utc(sec)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _normalize_reset_time(float(text))
    return text


def format_countdown(seconds: Optional[int]) -> str:
    if seconds is None:
        return "闲置"
    if seconds <= 0:
        return "已过期"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"


def truncate_display_width(text: str, max_cols: int = 18) -> str:
    """Truncate text by terminal/UI visual columns (CJK wide chars count as 2, ASCII as 1)."""
    clean = " ".join((text or "").split())
    if not clean or max_cols <= 2:
        return clean
    cols = 0
    out: List[str] = []
    for ch in clean:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cols + w > max_cols:
            while out and cols + 1 > max_cols:
                rem = out.pop()
                cols -= 2 if unicodedata.east_asian_width(rem) in ("W", "F") else 1
            return "".join(out).rstrip() + "…"
        out.append(ch)
        cols += w
    return clean


def parse_iso_timestamp(raw: str) -> Optional[float]:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return dt.timestamp()


_KERNEL32_LIVENESS: Optional[ctypes.WinDLL] = None


def _get_kernel32() -> ctypes.WinDLL:
    global _KERNEL32_LIVENESS
    if _KERNEL32_LIVENESS is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32_LIVENESS = k32
    return _KERNEL32_LIVENESS


def _is_windows_pid_alive(pid: int) -> bool:
    """Zero-subprocess PID liveness check with proper 64-bit Win32 HANDLE signatures."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        kernel32 = _get_kernel32()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


@dataclass(frozen=True)
class StepRecord:
    step_index: int
    timestamp: float
    model: str
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    total_tokens: int
    hit_rate: float
    classification: str  # "hit" | "expected_cold" | "abnormal_miss"
    reason: str          # "cached" | "first_turn" | "idle_rewarm" | "model_switch" | "compact_reset" | "revert_reset" | "small_ctx" | "first_cacheable" | "abnormal_zero"
    context_delta: int = 0
    is_revert: bool = False
    is_compact: bool = False


@dataclass
class ConversationSummary:
    conversation_id: str
    short_id: str
    model: str
    model_raw: str
    context_tokens: int
    max_context_tokens: int
    latest_hit_rate: float
    latest_cached_in: int
    latest_uncached_in: int
    latest_timestamp: float
    recent_steps: List[StepRecord]
    consec_miss: int
    consec_miss_start_step: int
    single_miss: bool
    wasted_cold_tokens: int
    freshness_remaining_sec: Optional[int]
    freshness_invalid_reason: str = ""  # non-empty when latest step abnormally lost cache (cr=0)
    abnormal_in_recent10: int = 0
    cacheable_in_recent10: int = 0
    recent10_hit_ratio: Optional[float] = None
    first_abnormal_step_in_recent10: int = 0
    ttft_sec: Optional[float] = None
    streaming_sec: Optional[float] = None
    tps: Optional[float] = None
    shard_tag: str = ""
    speed_status: str = "unknown"  # "ok" | "slow" | "unknown"
    recent_ttfts: List[Optional[float]] = field(default_factory=list)
    recent_tps_list: List[Optional[float]] = field(default_factory=list)
    ttft_avg_sec: Optional[float] = None
    ttft_max_sec: Optional[float] = None
    is_subagent: bool = False
    latest_context_delta: int = 0
    total_step_count: int = 0
    title: str = ""
    full_step_count: int = 0
    full_latest_timestamp: float = 0.0
    _scoped_steps_all: List[StepRecord] = field(default_factory=list, repr=False, compare=False)
    _scoped_gens_all: List[Optional[Any]] = field(default_factory=list, repr=False, compare=False)


@dataclass(frozen=True)
class ConversationDetailPoint:
    step_index: int
    timestamp: float
    model: str
    total_tokens: int
    context_delta: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    hit_rate: float
    classification: str
    reason: str
    ttft_sec: Optional[float] = None
    tps: Optional[float] = None
    streaming_sec: Optional[float] = None
    is_revert: bool = False
    is_compact: bool = False


@dataclass
class ConversationDetailReport:
    conversation_id: str
    short_id: str
    is_subagent: bool
    model: str
    model_raw: str
    total_steps: int
    today_steps: int
    peak_context_tokens: int
    latest_context_tokens: int
    latest_context_delta: int
    max_context_tokens: int
    compact_count: int
    cache_hit_steps: int
    abnormal_miss_steps: int
    expected_cold_steps: int
    overall_cacheable_hit_ratio: Optional[float]
    avg_ttft_sec: Optional[float]
    p95_ttft_sec: Optional[float]
    max_ttft_sec: Optional[float]
    avg_tps: Optional[float]
    latest_tps: Optional[float]
    latest_ttft_sec: Optional[float]
    freshness_remaining_sec: Optional[int]
    freshness_invalid_reason: str
    freshness_window_sec: int
    points: List[ConversationDetailPoint] = field(default_factory=list)
    title: str = ""
    revert_count: int = 0
    scope_label: str = ""


@dataclass(frozen=True)
class LogEvent:
    timestamp: float
    category: str   # "503" | "timeout" | "403" | "retry"
    summary: str
    event_key: str
    model: str = ""


@dataclass(frozen=True)
class ModelPerfStats:
    model: str
    step_count: int
    hit_count: int
    abnormal_miss_count: int
    ttft_avg_sec: Optional[float]
    ttft_max_sec: Optional[float]
    latest_ttft_sec: Optional[float]
    tps_avg: Optional[float]
    latest_timestamp: float
    steps_after_last_t2: int = 0
    ttft_after_last_t2_avg: Optional[float] = None
    ttft_after_last_t2_latest: Optional[float] = None
    ttft_after_last_t2_median: Optional[float] = None
    main_steps_after_last_t2: int = 0
    main_ttft_after_last_t2_median: Optional[float] = None
    main_ttft_after_last_t2_latest: Optional[float] = None
    main_ttft_after_last_t2_recent2_max: Optional[float] = None


@dataclass
class QuotaPoolInfo:
    name: str  # "gemini" | "3p"
    five_hour_pct: Optional[float] = None
    weekly_pct: Optional[float] = None
    five_hour_reset_at: Optional[str] = None
    weekly_reset_at: Optional[str] = None
    model_count: int = 0


@dataclass
class AccountQuotaInfo:
    email_masked: str = ""
    short_label: str = "离线"
    tier: str = ""
    five_hour_pct: Optional[float] = None
    weekly_pct: Optional[float] = None
    quota_family: str = "gemini"  # "gemini" | "3p"
    online: bool = False
    observed_at: float = 0.0
    plan_status: str = ""
    five_hour_reset_at: Optional[str] = None
    weekly_reset_at: Optional[str] = None
    pools: Dict[str, QuotaPoolInfo] = field(default_factory=dict)


@dataclass
class MonitorSnapshot:
    generated_at: float
    level: str            # "green" | "orange" | "red"
    grade: str            # "IDLE" | "T0" | "WARN" | "T1" | "T2" | "T3"
    headline: str
    sub_headline: str
    active_model: str
    primary_hit_rate: Optional[float]
    primary_hit_classification: str
    primary_tps: Optional[float]
    primary_ttft: Optional[float]
    primary_speed_status: str
    min_freshness_sec: Optional[int]
    min_freshness_conv: str
    min_freshness_ctx: int
    min_freshness_invalid_reason: str
    today_steps: int
    daily_steps_warn: int
    daily_steps_alert: int
    active_conversations: List[ConversationSummary]
    large_freshness_list: List[ConversationSummary]
    recent_503: int
    today_503: int
    recent_timeout: int
    today_timeout: int
    recent_403: int
    today_403: int
    recent_retry: int
    today_retry: int
    recent_events: List[LogEvent]
    wasted_cold_tokens_total: int
    max_consec_miss: int
    account: AccountQuotaInfo
    sliding_window_min: int
    freshness_window_sec: int
    alerts: List[Tuple[str, str, str]] = field(default_factory=list)
    primary_ttft_avg: Optional[float] = None
    primary_ttft_max: Optional[float] = None
    primary_recent_ttfts: List[Optional[float]] = field(default_factory=list)
    account_today_steps: int = 0
    total_today_steps: int = 0
    steps_scope_label: str = "全机"
    max_abnormal_recent10: int = 0
    worst_recent10_hit_ratio: Optional[float] = None
    pressure: str = "normal"  # "normal" | "warn" | "red" | "degraded" | "offline_unknown"
    main_today_steps: int = 0
    subagent_today_steps: int = 0
    total_main_today_steps: int = 0
    total_subagent_today_steps: int = 0
    last_1h_steps: int = 0
    peak_hour_steps_today: int = 0
    peak_hour_label: str = ""
    target_conversation: Optional[ConversationSummary] = None
    target_conversation_query: str = ""
    min_freshness_title: str = ""
    model_stats: List[ModelPerfStats] = field(default_factory=list)
    log_scope_label: str = "全机"
    machine_today_503: int = 0
    machine_today_timeout: int = 0
    machine_today_403: int = 0
    machine_today_retry: int = 0
    last_t2_error_ts: float = 0.0
    t2_recovery_note: str = ""
    available_accounts: List[str] = field(default_factory=list)
    view_account: str = ""
    resolved_account: str = ""
    is_custom_account_view: bool = False
    history_range: str = "30m"
    history_range_label: str = "近 30 分钟"
    is_historical_rating: bool = False
    rating_anchor_ts: float = 0.0
    range_steps: int = 0
    slot_inventory: Optional[SlotInventorySnapshot] = None
    is_host_idle_offline: bool = False


HISTORY_RANGE_SPECS: Dict[str, Tuple[str, str]] = {
    "30m": ("实时30m", "近 30 分钟"),
    "6h": ("近6h", "近 6 小时"),
    "today": ("今日", "今日"),
    "7d": ("近7天", "近 7 天"),
}


def format_relative_age(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"{sec}秒前"
    if sec < 3600:
        return f"{sec // 60}分钟前"
    if sec < 86400:
        hrs = sec // 3600
        mins = (sec % 3600) // 60
        return f"{hrs}小时{mins}分前" if mins > 0 else f"{hrs}小时前"
    days = sec // 86400
    hrs = (sec % 86400) // 3600
    return f"{days}天{hrs}小时前" if hrs > 0 else f"{days}天前"


def compute_pressure(
    grade: str,
    today_steps: int,
    daily_steps_warn: int,
    daily_steps_alert: int,
) -> str:
    """Map raw monitor state into a clean 5-tier pressure code for RULES automation."""
    if grade == "IDLE":
        return "offline_unknown"
    if grade in ("T1", "T2", "T3"):
        return "degraded"
    if today_steps >= daily_steps_alert:
        return "red"
    if grade == "WARN" or today_steps >= daily_steps_warn:
        return "warn"
    return "normal"


def compute_cli_exit_code(snap: MonitorSnapshot) -> int:
    """Semantic exit code: 0=normal healthy, 1=warn/red/degraded throttle needed, 2=offline/unknown."""
    if snap.pressure == "offline_unknown":
        return 2
    if snap.pressure in ("warn", "red", "degraded"):
        return 1
    if snap.pressure == "normal":
        return 0
    return 2


def default_antigravity_paths() -> Dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    return {
        "brain_dir": home / ".gemini" / "antigravity" / "brain",
        "conversations_dir": home / ".gemini" / "antigravity" / "conversations",
        "annotations_dir": home / ".gemini" / "antigravity" / "annotations",
        "summaries_db_path": home / ".gemini" / "antigravity" / "conversation_summaries.db",
        "daemon_dir": home / ".gemini" / "antigravity" / "daemon",
        "ls_log_path": appdata / "Antigravity" / "logs" / "language_server.log",
        "cockpit_quota_db": home / ".antigravity_cockpit" / "platform-quota-history-v1.sqlite",
        "cockpit_logs_dir": home / ".antigravity_cockpit" / "logs",
        "ledger_path": Path(__file__).resolve().parent.parent / "account_ledger.json",
        "vscdb_path": appdata / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
    }


_PBTXT_TITLE_RE = re.compile(rb'(?:^|[\s{])title\s*:\s*"((?:\\.|[^"\\])*)"')


def parse_pbtxt_title(raw: bytes) -> str:
    """Decode `title:"..."` from Antigravity annotations/*.pbtxt (UTF-8 & protobuf text escapes)."""
    m = _PBTXT_TITLE_RE.search(raw)
    if not m:
        return ""
    body = m.group(1)
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == 0x5C and i + 1 < n:  # backslash escape
            nxt = body[i + 1]
            if 0x30 <= nxt <= 0x37:  # octal \ooo (1-3 digits)
                j = i + 1
                while j < n and j < i + 4 and 0x30 <= body[j] <= 0x37:
                    j += 1
                out.append(int(body[i + 1 : j], 8) & 0xFF)
                i = j
                continue
            if nxt in (0x78, 0x58) and i + 3 < n:  # hex \xHH
                try:
                    out.append(int(body[i + 2 : i + 4], 16) & 0xFF)
                    i += 4
                    continue
                except ValueError:
                    pass
            esc_map = {
                ord('"'): ord('"'),
                ord("'"): ord("'"),
                ord("\\"): ord("\\"),
                ord("n"): ord(" "),
                ord("r"): ord(" "),
                ord("t"): ord(" "),
            }
            out.append(esc_map.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return " ".join(out.decode("utf-8", errors="ignore").split()).strip()


def classify_steps(
    raw_steps: List[Dict[str, Any]],
    idle_rewarm_sec: int = 180,
    min_cacheable_tokens: int = 32000,
    account_switches: Optional[List[float]] = None,
) -> List[StepRecord]:
    """Classify consecutive PLANNER_RESPONSE steps, filtering normal zero-cache causes."""
    classified: List[StepRecord] = []
    prev_ts: Optional[float] = None
    prev_model: Optional[str] = None
    prev_total: Optional[int] = None
    prev_step_idx: Optional[int] = None
    switches = sorted(account_switches) if account_switches else []

    for idx, item in enumerate(raw_steps):
        inp = max(0, int(item.get("input_tokens", 0) or 0))
        cr = max(0, int(item.get("cache_read_tokens", 0) or 0))
        out = max(0, int(item.get("output_tokens", 0) or 0))
        total = inp + cr
        if total <= 0:
            continue
        ts = float(item.get("timestamp", 0.0) or 0.0)
        model = str(item.get("model", "") or "").strip()
        raw_si = item.get("step_index")
        step_idx = int(raw_si) if raw_si is not None else idx
        hit_rate = (cr / total) * 100.0

        crossed_switch = False
        if prev_ts is not None and ts > prev_ts and switches:
            for sw_ts in switches:
                if prev_ts < sw_ts <= ts + 2.0:
                    crossed_switch = True
                    break

        after_revert = bool(item.get("after_revert", False))
        if (
            not after_revert
            and prev_step_idx is not None
            and raw_si is not None
            and step_idx <= prev_step_idx
            and (prev_ts is None or ts <= 0.0 or prev_ts <= 0.0 or ts >= prev_ts - 5.0)
        ):
            after_revert = True

        after_checkpoint = bool(item.get("after_checkpoint", False))
        is_real_compact = (
            not after_revert
            and prev_total is not None
            and prev_total >= min_cacheable_tokens
            and ((after_checkpoint and total < prev_total * 0.85) or (total < prev_total * 0.75))
        )
        if cr > 0:
            cls, reason = "hit", "cached"
        elif prev_ts is None:
            cls, reason = "expected_cold", "first_turn"
        elif crossed_switch:
            cls, reason = "expected_cold", "account_switch"
        elif after_revert:
            cls, reason = "expected_cold", "revert_reset"
        elif is_real_compact:
            cls, reason = "expected_cold", "compact_reset"
        elif ts > 0 and prev_ts > 0 and (ts - prev_ts) > idle_rewarm_sec:
            cls, reason = "expected_cold", "idle_rewarm"
        elif prev_model and model and model.lower() != prev_model.lower():
            cls, reason = "expected_cold", "model_switch"
        elif total < min_cacheable_tokens:
            cls, reason = "expected_cold", "small_ctx"
        elif prev_total is not None and prev_total < min_cacheable_tokens:
            # First turn crossing the >=32K implicit cache threshold cannot have a prior warm prefix yet
            cls, reason = "expected_cold", "first_cacheable"
        else:
            cls, reason = "abnormal_miss", "abnormal_zero"

        ctx_delta = (total - prev_total) if prev_total is not None else 0
        classified.append(
            StepRecord(
                step_index=step_idx,
                timestamp=ts,
                model=model,
                input_tokens=inp,
                cache_read_tokens=cr,
                output_tokens=out,
                total_tokens=total,
                hit_rate=hit_rate,
                classification=cls,
                reason=reason,
                context_delta=ctx_delta,
                is_revert=after_revert,
                is_compact=is_real_compact,
            )
        )
        # Do not let an out-of-order delayed historical step clobber the forward cursor
        is_delayed_ooo = prev_ts is not None and ts > 0.0 and prev_ts > 0.0 and ts < prev_ts - 5.0
        if not is_delayed_ooo:
            prev_ts = ts
            prev_model = model
            prev_total = total
            prev_step_idx = step_idx

    return classified


class LanguageServerLogTailer:
    """Incremental seek(offset) tailer with rotation, partial-line, and sliding-window guards."""

    def __init__(self, log_path: Path, max_events: int = 200) -> None:
        self.log_path = Path(log_path)
        self.max_events = max(20, int(max_events))
        self._offset: int = 0
        self._events: Deque[LogEvent] = deque(maxlen=self.max_events)
        self._initialized: bool = False
        self._last_seen_ts: Optional[float] = None

    def poll(self, now_ts: Optional[float] = None) -> None:
        now = now_ts if now_ts is not None else time.time()
        if not self.log_path.is_file():
            self._offset = 0
            self._initialized = True
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return

        if size < self._offset:
            self._offset = 0
            self._events.clear()
            self._last_seen_ts = None

        if size == self._offset and self._initialized:
            return

        is_cold_start = not self._initialized
        discard_first_partial = False
        if is_cold_start and size > 2 * 1024 * 1024:
            self._offset = size - 2 * 1024 * 1024
            discard_first_partial = True

        try:
            with self.log_path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return

        self._initialized = True
        if not chunk:
            return

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return
        consumed = chunk[: last_nl + 1]
        self._offset += len(consumed)

        if discard_first_partial:
            first_nl = consumed.find(b"\n")
            if first_nl != -1:
                consumed = consumed[first_nl + 1 :]
            else:
                return

        text = consumed.decode("utf-8", errors="replace")
        for line in text.splitlines():
            event = self._parse_line(line, now, is_cold_start=is_cold_start)
            if event is not None:
                self._events.append(event)

    def _parse_line(self, line: str, fallback_ts: float, is_cold_start: bool = False) -> Optional[LogEvent]:
        if not line:
            return None
        low_full = line.lower()
        if "127.0.0.1" in low_full or "localhost" in low_full or "tls handshake error" in low_full:
            return None

        ts, source, body = self._split_header_and_body(line, fallback_ts, is_cold_start=is_cold_start)
        if ts is not None:
            self._last_seen_ts = ts

        src_low = source.lower()
        body_low = body.lower()
        if any(ign in src_low or ign in body_low[:80] for ign in IGNORED_LOG_SOURCES):
            return None
        if "monitor sir_" in body_low or "llm monitor" in body_low:
            return None

        category: Optional[str] = None
        label = ""
        m_to = STREAM_TIMEOUT_RE.search(body)
        if TOS_403_RE.search(body):
            category = "403"
            label = "403 TOS/权限拒绝"
        elif m_to is not None:
            is_explicit_stream = m_to.group(1) is not None or ("stream idle timeout" in body_low)
            raw_dur = (m_to.group(1) or m_to.group(2) or "").strip().lower()
            dur_sec = _parse_duration_to_sec(raw_dur) if raw_dur else None
            # Generic "timed out after <dur>" below 30s (e.g. 500ms / 5s local RPC) must NOT false-alarm LLM stream timeout
            if (not is_explicit_stream) and (dur_sec is not None and dur_sec < 30.0):
                category = None
            else:
                category = "timeout"
                norm_dur = re.sub(r"^(\d+h)?(\d+m)0s$", lambda mm: (mm.group(1) or "") + mm.group(2), raw_dur) if raw_dur else ""
                if norm_dur.endswith("0m") and "h" in norm_dur:
                    norm_dur = norm_dur[:-2]
                label = f"流超时 ({norm_dur})" if norm_dur else "流超时"
        elif HTTP_503_RE.search(body):
            category = "503"
            label = "服务端 503 No capacity"
        elif RETRY_RE.search(line) and "context canceled" not in body_low and "shutting down" not in body_low:
            category = "retry"
            label = "代理/流断连重试"

        if category is None:
            return None

        # On cold start, ignore lines that have no verifiable timestamp at all rather than stamping them as "now"
        effective_ts = ts if ts is not None else (self._last_seen_ts if self._last_seen_ts is not None else (0.0 if is_cold_start else fallback_ts))
        if effective_ts <= 0.0:
            return None

        hm = datetime.datetime.fromtimestamp(effective_ts).strftime("%H:%M:%S")
        clean_detail = mask_email(body.strip())
        if len(clean_detail) > 96:
            clean_detail = clean_detail[:93] + "..."
        summary = f"[{hm}] {label}"
        event_key = f"log:{category}:{int(effective_ts)}:{hash(clean_detail) & 0xFFFF:04x}"
        return LogEvent(timestamp=effective_ts, category=category, summary=summary, event_key=event_key)

    @staticmethod
    def _split_header_and_body(line: str, fallback_ts: float, is_cold_start: bool = False) -> Tuple[Optional[float], str, str]:
        # 1. glog format: [ERROR: logging before google.Init: ]W1120 05:08:41.350309 607 file.go:54] body
        m_glog = GLOG_PREFIX_RE.match(line)
        if m_glog:
            mm, dd, hms, source, body = int(m_glog.group(2)), int(m_glog.group(3)), m_glog.group(4), m_glog.group(5), m_glog.group(6)
            now_dt = datetime.datetime.fromtimestamp(fallback_ts)
            try:
                h, mi, s = (int(p) for p in hms.split(":"))
                cand = now_dt.replace(month=mm, day=dd, hour=h, minute=mi, second=s, microsecond=0)
                if cand.timestamp() > fallback_ts + 86400:
                    cand = cand.replace(year=now_dt.year - 1)
                return cand.timestamp(), source, body
            except ValueError:
                return None, source, body

        # 2. ISO / Go stdlog slash format: 2026/11/20 02:18:54 body
        m_full = FULL_TS_RE.match(line)
        if m_full:
            iso_norm = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}T{m_full.group(4)}"
            parsed = parse_iso_timestamp(iso_norm)
            return parsed, "", m_full.group(5)

        return None, "", line

    def get_counts(
        self,
        now_ts: float,
        window_min: int,
        account_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: Optional[float] = None,
        range_end_ts: Optional[float] = None,
    ) -> Tuple[Dict[str, int], Dict[str, int], List[LogEvent]]:
        win_end = range_end_ts if range_end_ts is not None else now_ts
        default_start = win_end - max(1, window_min) * 60
        cutoff = max(range_start_ts if range_start_ts is not None else default_start, account_since_ts)
        today_start = datetime.datetime.fromtimestamp(now_ts).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        recent = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        today = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        in_window_notable: List[LogEvent] = []

        for ev in self._events:
            if account_filter_fn is not None and not account_filter_fn(ev.timestamp):
                continue
            if ev.timestamp >= today_start:
                today[ev.category] = today.get(ev.category, 0) + 1
            if cutoff <= ev.timestamp <= win_end + 2.0:
                recent[ev.category] = recent.get(ev.category, 0) + 1
                if ev.category in ("503", "timeout", "403"):
                    in_window_notable.append(ev)

        return recent, today, in_window_notable[-3:]

    def get_machine_today_counts(self, now_ts: float) -> Dict[str, int]:
        today_start = datetime.datetime.fromtimestamp(now_ts).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        today = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        for ev in self._events:
            if ev.timestamp >= today_start:
                today[ev.category] = today.get(ev.category, 0) + 1
        return today

    def get_latest_error_ts(
        self,
        now_ts: float,
        window_min: int,
        categories: Tuple[str, ...] = ("503", "timeout"),
        account_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: Optional[float] = None,
        range_end_ts: Optional[float] = None,
    ) -> float:
        win_end = range_end_ts if range_end_ts is not None else now_ts
        default_start = win_end - max(1, window_min) * 60
        cutoff = max(range_start_ts if range_start_ts is not None else default_start, account_since_ts)
        latest = 0.0
        for ev in self._events:
            if account_filter_fn is not None and not account_filter_fn(ev.timestamp):
                continue
            if cutoff <= ev.timestamp <= win_end + 2.0 and ev.category in categories and ev.timestamp > latest:
                latest = ev.timestamp
        return latest

    def get_latest_account_event_ts(
        self,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        since_ts: float = 0.0,
        categories: Tuple[str, ...] = ("503", "timeout", "403"),
    ) -> float:
        latest = 0.0
        for ev in self._events:
            if ev.timestamp < since_ts or ev.category not in categories:
                continue
            if account_filter_fn is not None and not account_filter_fn(ev.timestamp):
                continue
            if ev.timestamp > latest:
                latest = ev.timestamp
        return latest


class AntigravityMonitorEngine:
    """Stateful incremental read-only monitor combining all 4 local data sources."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        paths: Optional[Dict[str, Path]] = None,
        read_only: bool = False,
    ) -> None:
        self.config = sanitize_config(config or DEFAULT_CONFIG)
        self.read_only: bool = bool(read_only)
        base_paths = default_antigravity_paths()
        if paths:
            base_paths.update(paths)
        self.brain_dir = Path(base_paths["brain_dir"])
        self.conversations_dir = Path(base_paths["conversations_dir"])
        self.daemon_dir = Path(base_paths["daemon_dir"])
        self.ls_log_path = Path(base_paths["ls_log_path"])
        self.cockpit_quota_db = Path(base_paths["cockpit_quota_db"])
        cockpit_logs_dir = base_paths.get("cockpit_logs_dir")
        ledger_path = base_paths.get("ledger_path")
        vscdb_path = base_paths.get("vscdb_path")
        annotations_dir = base_paths.get("annotations_dir")
        # In isolated test harnesses where daemon_dir is overridden without explicit ledger_path/cockpit_logs_dir/vscdb_path/annotations_dir,
        # avoid reading the host's real Cockpit logs, VSCode state DB, annotations, or polluting the real local ledger file.
        if paths and "ledger_path" not in paths:
            ledger_path = None
        if paths and "cockpit_logs_dir" not in paths:
            cockpit_logs_dir = None
        if paths and "vscdb_path" not in paths:
            vscdb_path = None
        if paths and "annotations_dir" not in paths:
            annotations_dir = None

        self.vscdb_path: Optional[Path] = Path(vscdb_path) if vscdb_path is not None else None
        self.annotations_dir: Optional[Path] = Path(annotations_dir) if annotations_dir is not None else None
        summaries_db_path = base_paths.get("summaries_db_path")
        if paths and "summaries_db_path" not in paths:
            summaries_db_path = self.conversations_dir.parent / "conversation_summaries.db"
        self.summaries_db_path: Path = Path(summaries_db_path) if summaries_db_path is not None else (self.conversations_dir.parent / "conversation_summaries.db")
        self._lock = threading.RLock()
        self._transcript_cache: "OrderedDict[str, Tuple[Tuple[int, int], List[Dict[str, Any]], bool, bool]]" = OrderedDict()
        self._transcript_error_ts: "OrderedDict[str, List[Tuple[float, str]]]" = OrderedDict()
        self._classified_cache: "OrderedDict[Tuple[str, Tuple[int, int], Tuple[float, ...], int], List[StepRecord]]" = OrderedDict()
        self._full_align_cache: "OrderedDict[Tuple[str, Tuple[int, int], int, int], List[Optional[Any]]]" = OrderedDict()
        self._subagent_hint_cache: "OrderedDict[str, Tuple[int, bool]]" = OrderedDict()
        self._conv_titles: Dict[str, str] = {}
        self._annotation_title_cache: "OrderedDict[str, Tuple[Tuple[int, int], str]]" = OrderedDict()
        self._vscdb_titles_stamp: Optional[Tuple[int, int]] = None
        self._vscdb_titles_cache: Dict[str, str] = {}
        self._db_reader = ConversationDbReader(self.conversations_dir, max_cache_entries=36, tail_rows=16)
        self._log_tailer = LanguageServerLogTailer(self.ls_log_path, max_events=1500)
        self._ledger = AccountSwitchLedger(
            ledger_path=ledger_path,
            cockpit_logs_dir=cockpit_logs_dir,
            read_only=self.read_only,
        )
        journal_path = None if self.read_only else (Path(__file__).resolve().parent.parent / "logs" / "slot_cleanup_journal.jsonl")
        if paths and "slot_journal_path" in paths:
            journal_path = None if self.read_only else Path(paths["slot_journal_path"])
        self.slot_scanner = SlotInventoryScanner(
            conversations_dir=self.conversations_dir,
            brain_dir=self.brain_dir,
            summaries_db_path=self.summaries_db_path,
            cache_ttl_sec=15.0,
        )
        self.slot_cleaner = SlotSubagentCleaner(self.slot_scanner, journal_path=journal_path)
        self._offline_since_ts: float = 0.0
        self._last_auto_clean_ts: float = 0.0
        self._last_auto_clean_summary: str = ""
        self._account_cache: AccountQuotaInfo = AccountQuotaInfo()
        self._active_hub_pid: int = 0
        self._last_account_poll: float = 0.0
        self._last_quota_family: str = "gemini"
        self._baseline_ready: bool = False
        self._emitted_alert_keys: Deque[str] = deque(maxlen=256)

    def read_slot_inventory(self, include_items: bool = True, force: bool = True, now_ts: Optional[float] = None) -> SlotInventorySnapshot:
        with self._lock:
            cfg = dict(self.config)
            summary_txt = self._last_auto_clean_summary
        inv = self.slot_scanner.scan(
            now_ts=now_ts,
            protect_age_min=int(cfg.get("slot_protect_age_min", 120)),
            slot_cap=int(cfg.get("slot_cap", 500)),
            warn_threshold=int(cfg.get("slot_warn_threshold", 380)),
            alert_threshold=int(cfg.get("slot_alert_threshold", 450)),
            include_items=include_items,
            force=force,
        )
        # Never mutate cached SlotInventorySnapshot in place across threads!
        return replace(inv, last_auto_clean_summary=summary_txt)

    def clean_old_subagents(
        self,
        protect_age_min: Optional[int] = None,
        mode: Optional[str] = None,
        archive_dir: Optional[str] = None,
        trigger: str = "manual",
        dry_run: bool = False,
        now_ts: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
        update_summary: bool = True,
    ) -> SlotCleanupResult:
        with self._lock:
            cfg = dict(self.config)
        eff_age = max(
            MIN_PROTECT_AGE_MIN,
            int(cfg.get("slot_protect_age_min", 120)) if protect_age_min is None else int(protect_age_min),
        )
        eff_mode = str(cfg.get("slot_clean_mode", "recycle")) if mode is None else str(mode)
        eff_arch = str(cfg.get("slot_archive_dir", "")) if archive_dir is None else str(archive_dir)
        started = float(now_ts if now_ts is not None else time.time())
        if self.read_only and not dry_run:
            return SlotCleanupResult(
                started_at=started,
                finished_at=time.time(),
                trigger=trigger,
                mode=eff_mode,
                protect_age_min=eff_age,
                dry_run=False,
                error="read_only_mode_forbids_cleanup",
            )
        res = self.slot_cleaner.execute_cleanup(
            protect_age_min=eff_age,
            mode=eff_mode,
            archive_dir=eff_arch,
            trigger=trigger,
            dry_run=dry_run,
            now_ts=now_ts,
            slot_cap=int(cfg.get("slot_cap", 500)),
            stop_event=stop_event,
        )
        if not dry_run:
            with self._lock:
                for cid in res.removed_cids:
                    self._transcript_cache.pop(cid, None)
                    self._transcript_error_ts.pop(cid, None)
                if update_summary:
                    hm = datetime.datetime.fromtimestamp(res.finished_at).strftime("%H:%M")
                    tag = "手动清理" if trigger == "manual" else "自动让位"
                    if res.error:
                        self._last_auto_clean_summary = f"{hm} {tag}失败: {res.error}"
                    elif res.removed_count > 0:
                        self._last_auto_clean_summary = f"{hm} {tag} {res.removed_count} 条子代理 ({format_bytes_human(res.freed_bytes)})"
        return res

    def maybe_auto_clean_slots(
        self,
        snap: MonitorSnapshot,
        now_ts: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[SlotCleanupResult]:
        """Run safe automatic old-subagent cleanup when Antigravity is offline/closed or slots hit high water."""
        with self._lock:
            cfg = dict(self.config)
            if self.read_only or not bool(cfg.get("slot_auto_clean_enabled", True)):
                return None
            inv = snap.slot_inventory
            if inv is None or not inv.summary_db_ok:
                return None
            now = float(now_ts if now_ts is not None else time.time())
            # Decouple offline detection from UI history view filters (6h/today/7d/custom account)!
            is_offline_idle = bool(snap.is_host_idle_offline or ((not snap.account.online) and (snap.grade == "IDLE")))
            if is_offline_idle:
                if self._offline_since_ts <= 0.0:
                    self._offline_since_ts = now
            else:
                self._offline_since_ts = 0.0

            # Respect 10-minute cooldown between automatic cleanup passes
            if self._last_auto_clean_ts > 0.0 and (now - self._last_auto_clean_ts) < 600.0:
                return None

            base_protect = max(MIN_PROTECT_AGE_MIN, int(cfg.get("slot_protect_age_min", 120)))
            trigger_th = int(cfg.get("slot_auto_trigger_threshold", 420))
            alert_th = int(cfg.get("slot_alert_threshold", 450))

            chosen_trigger = ""
            chosen_age = base_protect
            if is_offline_idle and (now - self._offline_since_ts) >= 15.0:
                # Ideal window: Antigravity is closed/idle (zero LS handle locks) before next startup prune!
                if inv.cleanable_sub_count > 0:
                    chosen_trigger = "auto_offline"
                    chosen_age = base_protect
                elif inv.total_db >= trigger_th and inv.cleanable_30m_count > 0:
                    chosen_trigger = "auto_offline"
                    chosen_age = MIN_PROTECT_AGE_MIN
            elif snap.account.online and inv.total_db >= trigger_th:
                if inv.cleanable_sub_count > 0:
                    chosen_trigger = "auto_high_water"
                    chosen_age = base_protect
                elif inv.total_db >= alert_th and inv.cleanable_30m_count > 0:
                    chosen_trigger = "auto_high_water"
                    chosen_age = MIN_PROTECT_AGE_MIN

            if not chosen_trigger:
                return None
            self._last_auto_clean_ts = now

        res = self.clean_old_subagents(
            protect_age_min=chosen_age,
            trigger=chosen_trigger,
            dry_run=False,
            now_ts=now,
            stop_event=stop_event,
            update_summary=False,
        )
        # Stepped pass down to the 30m hard floor if pool is still high after 2h pass:
        # - Offline: remaining >= trigger_th
        # - Online high-water: remaining >= alert_th (prevents 1 locked >2h subagent from starving 30m..120m cleanups!)
        need_step2 = (
            chosen_age > MIN_PROTECT_AGE_MIN
            and not res.error
            and (stop_event is None or not stop_event.is_set())
            and (
                (chosen_trigger == "auto_offline" and res.remaining_db_count >= trigger_th)
                or (chosen_trigger == "auto_high_water" and res.remaining_db_count >= alert_th)
            )
        )
        if need_step2:
            res2 = self.clean_old_subagents(
                protect_age_min=MIN_PROTECT_AGE_MIN,
                trigger=chosen_trigger,
                dry_run=False,
                now_ts=now,
                stop_event=stop_event,
                update_summary=False,
            )
            if not res2.error and res2.removed_count > 0:
                res.removed_count += res2.removed_count
                res.freed_bytes += res2.freed_bytes
                res.skipped_locked_count += res2.skipped_locked_count
                res.skipped_changed_count += res2.skipped_changed_count
                res.remaining_db_count = res2.remaining_db_count
                res.removed_cids.extend(res2.removed_cids)
            elif res2.error and not res.error and res.removed_count == 0:
                res.error = res2.error

        hm = datetime.datetime.fromtimestamp(res.finished_at).strftime("%H:%M")
        with self._lock:
            if res.error:
                self._last_auto_clean_summary = f"{hm} 自动让位失败: {res.error}"
            elif res.removed_count > 0:
                self._last_auto_clean_summary = f"{hm} 自动让位 {res.removed_count} 条子代理 ({format_bytes_human(res.freed_bytes)})"
        if res.error:
            self.slot_cleaner._append_journal(
                {
                    "at": datetime.datetime.fromtimestamp(res.finished_at).astimezone().isoformat(timespec="seconds"),
                    "trigger": chosen_trigger,
                    "mode": res.mode,
                    "protect_age_min": res.protect_age_min,
                    "error": res.error,
                }
            )
        return res

    def update_config(self, config: Dict[str, Any]) -> None:
        with self._lock:
            self.config = sanitize_config(config)

    def _classify_steps_cached(
        self,
        conv_id: str,
        raw_steps: List[Dict[str, Any]],
        switch_timestamps: List[float],
    ) -> List[StepRecord]:
        t_entry = self._transcript_cache.get(conv_id)
        t_stamp = t_entry[0] if t_entry is not None else (0, len(raw_steps))
        sw_sig = tuple(round(t, 1) for t in switch_timestamps[-24:])
        idle_sec = int(self.config["idle_rewarm_sec"])
        key = (conv_id, t_stamp, sw_sig, idle_sec)
        cached = self._classified_cache.get(key)
        if cached is not None:
            self._classified_cache.move_to_end(key)
            return cached
        steps = classify_steps(
            raw_steps,
            idle_rewarm_sec=idle_sec,
            account_switches=switch_timestamps,
        )
        self._classified_cache[key] = steps
        self._classified_cache.move_to_end(key)
        while len(self._classified_cache) > 256:
            self._classified_cache.popitem(last=False)
        return steps

    def _check_subagent_hint_cached(self, conv_id: str) -> bool:
        db_path = self.conversations_dir / f"{conv_id}.db"
        try:
            mtime_ns = db_path.stat().st_mtime_ns
        except OSError:
            self._subagent_hint_cache.pop(conv_id, None)
            return False
        cached = self._subagent_hint_cache.get(conv_id)
        if cached is not None and cached[0] == mtime_ns:
            self._subagent_hint_cache.move_to_end(conv_id)
            return cached[1]
        hint = self._db_reader.check_subagent_hint(conv_id)
        self._subagent_hint_cache[conv_id] = (mtime_ns, hint)
        self._subagent_hint_cache.move_to_end(conv_id)
        while len(self._subagent_hint_cache) > 512:
            self._subagent_hint_cache.popitem(last=False)
        return hint

    @staticmethod
    def _recompute_conv_eval_stats(c: ConversationSummary, eval_since_ts: float) -> None:
        """Restrict a conversation's miss/hit health counters to steps at or after ``eval_since_ts``."""
        src_steps = c._scoped_steps_all or c.recent_steps
        eval_steps = [s for s in src_steps if s.timestamp >= eval_since_ts - 2.0]
        eval_recent10 = [s for s in c.recent_steps if s.timestamp >= eval_since_ts - 2.0]
        consec_miss = 0
        consec_start_step = eval_steps[-1].step_index if eval_steps else 0
        for s in reversed(eval_steps):
            if s.classification == "abnormal_miss":
                consec_miss += 1
                consec_start_step = s.step_index
            else:
                break
        abnormal_steps_r10 = [s for s in eval_recent10 if s.classification == "abnormal_miss"]
        abnormal_r10 = len(abnormal_steps_r10)
        hits_r10 = sum(1 for s in eval_recent10 if s.classification == "hit")
        cacheable_r10 = abnormal_r10 + hits_r10
        c.consec_miss = consec_miss
        c.consec_miss_start_step = consec_start_step
        c.single_miss = consec_miss == 1
        c.wasted_cold_tokens = sum(s.input_tokens for s in eval_recent10 if s.classification == "abnormal_miss")
        c.abnormal_in_recent10 = abnormal_r10
        c.cacheable_in_recent10 = cacheable_r10
        c.recent10_hit_ratio = (hits_r10 / cacheable_r10) if cacheable_r10 > 0 else None
        c.first_abnormal_step_in_recent10 = abnormal_steps_r10[0].step_index if abnormal_steps_r10 else 0

    def _build_conversation_summary(
        self,
        conv_id: str,
        raw_steps: List[Dict[str, Any]],
        has_user_explicit: bool,
        has_subagent_marker: bool,
        now: float,
        switch_timestamps: List[float],
        current_acct_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: float = 0.0,
    ) -> Optional[Tuple[ConversationSummary, List[StepRecord], List[int]]]:
        all_steps = self._classify_steps_cached(conv_id, raw_steps, switch_timestamps)
        if not all_steps:
            return None

        scoped_indices: List[int] = []
        for idx_s, s in enumerate(all_steps):
            if account_filter_fn is not None and not account_filter_fn(s.timestamp):
                continue
            if range_start_ts > 0.0 and s.timestamp < range_start_ts:
                continue
            scoped_indices.append(idx_s)

        if not scoped_indices:
            return None

        scoped_steps = [all_steps[i] for i in scoped_indices]
        latest = scoped_steps[-1]
        recent10 = scoped_steps[-10:]
        if account_filter_fn is None and current_acct_since_ts > 0.0:
            eval_steps = [s for s in scoped_steps if s.timestamp >= current_acct_since_ts - 2.0]
            eval_recent10 = [s for s in recent10 if s.timestamp >= current_acct_since_ts - 2.0]
        else:
            eval_steps = scoped_steps
            eval_recent10 = recent10

        consec_miss = 0
        consec_start_step = latest.step_index
        for s in reversed(eval_steps):
            if s.classification == "abnormal_miss":
                consec_miss += 1
                consec_start_step = s.step_index
            else:
                break
        single_miss = consec_miss == 1
        wasted_cold = sum(s.input_tokens for s in eval_recent10 if s.classification == "abnormal_miss")
        abnormal_steps_r10 = [s for s in eval_recent10 if s.classification == "abnormal_miss"]
        abnormal_r10 = len(abnormal_steps_r10)
        hits_r10 = sum(1 for s in eval_recent10 if s.classification == "hit")
        cacheable_r10 = abnormal_r10 + hits_r10
        hit_ratio_r10 = (hits_r10 / cacheable_r10) if cacheable_r10 > 0 else None
        first_abn_step_r10 = abnormal_steps_r10[0].step_index if abnormal_steps_r10 else 0

        fresh_rem: Optional[int] = None
        fresh_invalid = ""
        if latest.total_tokens >= self.config["large_context_threshold"] and latest.timestamp > 0:
            age = now - latest.timestamp
            if age <= self.config["freshness_window_sec"] + 300:
                if latest.classification == "abnormal_miss" and (
                    account_filter_fn is not None
                    or current_acct_since_ts <= 0.0
                    or latest.timestamp >= current_acct_since_ts - 2.0
                ):
                    fresh_invalid = f"无热缓存 (连掉 {consec_miss} 步)" if consec_miss >= 2 else "本步 cr=0 无热缓存"
                elif (
                    account_filter_fn is not None
                    or current_acct_since_ts <= 0.0
                    or latest.timestamp >= current_acct_since_ts - 2.0
                ):
                    fresh_rem = int(self.config["freshness_window_sec"] - age)

        summary = ConversationSummary(
            conversation_id=conv_id,
            short_id=conv_id[:6],
            model=format_model_name(latest.model),
            model_raw=latest.model,
            context_tokens=latest.total_tokens,
            max_context_tokens=0,
            latest_hit_rate=latest.hit_rate,
            latest_cached_in=latest.cache_read_tokens,
            latest_uncached_in=latest.input_tokens,
            latest_timestamp=latest.timestamp,
            recent_steps=recent10,
            consec_miss=consec_miss,
            consec_miss_start_step=consec_start_step,
            single_miss=single_miss,
            wasted_cold_tokens=wasted_cold,
            freshness_remaining_sec=fresh_rem,
            freshness_invalid_reason=fresh_invalid,
            abnormal_in_recent10=abnormal_r10,
            cacheable_in_recent10=cacheable_r10,
            recent10_hit_ratio=hit_ratio_r10,
            first_abnormal_step_in_recent10=first_abn_step_r10,
            is_subagent=(not has_user_explicit) and has_subagent_marker,
            latest_context_delta=latest.context_delta,
            total_step_count=len(scoped_steps),
            title=self._resolve_conv_title(conv_id),
            full_step_count=len(all_steps),
            full_latest_timestamp=all_steps[-1].timestamp,
            _scoped_steps_all=scoped_steps,
        )
        return summary, all_steps, scoped_indices

    def _enrich_conv_from_db(
        self,
        c: ConversationSummary,
        all_steps: Optional[List[StepRecord]] = None,
        scoped_indices: Optional[List[int]] = None,
        account_scoped: bool = False,
        need_full_window_gens: bool = False,
    ) -> None:
        snap: Optional[ConversationDbSnapshot] = self._db_reader.read(c.conversation_id)
        if not snap:
            return
        if snap.is_subagent_hint and not self._transcript_cache.get(c.conversation_id, (None, [], False, False))[2]:
            c.is_subagent = True
        c.max_context_tokens = snap.max_context_tokens

        tail_gens = [g for g in snap.recent if g.total_in > 0 or g.output_tokens > 0]
        is_tail_truncated = bool(
            all_steps is not None
            and scoped_indices
            and scoped_indices[-1] < len(all_steps) - 1
        )
        # Detect ANY non-contiguous gap inside scoped recent steps (even short cross-account gaps <= 16 rows!),
        # so intervening steps from another account can NEVER be mismatched onto this account's TTFT bars!
        recent_n = min(len(scoped_indices), len(c.recent_steps)) if scoped_indices else 0
        has_internal_gap = bool(
            all_steps is not None
            and scoped_indices
            and recent_n >= 2
            and ((scoped_indices[-1] - scoped_indices[-recent_n] + 1) > recent_n)
        )
        need_full = (
            is_tail_truncated
            or has_internal_gap
            or (need_full_window_gens and len(c._scoped_steps_all) > len(c.recent_steps))
        )

        aligned_recent: List[Optional[Any]] = []
        if not need_full and c.recent_steps and tail_gens:
            # Fast zero-full-scan path for contiguous live un-truncated conversations
            aligned_recent = _align_steps_with_gen_history(c.recent_steps, tail_gens)
            if any(g is not None for g in aligned_recent):
                c._scoped_gens_all = list(aligned_recent)
            else:
                need_full = True

        if need_full and all_steps and scoped_indices:
            # Global alignment on full all_steps with LRU cache keyed by (cid, transcript_stamp, len(all_steps), len(full_gens))
            full_gens = self._db_reader.read_full_history(c.conversation_id)
            if full_gens:
                t_entry = self._transcript_cache.get(c.conversation_id)
                t_stamp = t_entry[0] if t_entry is not None else (0, len(all_steps))
                a_key = (c.conversation_id, t_stamp, len(all_steps), len(full_gens))
                full_aligned = self._full_align_cache.get(a_key)
                if full_aligned is not None:
                    self._full_align_cache.move_to_end(a_key)
                else:
                    full_aligned = _align_steps_with_gen_history(all_steps, full_gens)
                    self._full_align_cache[a_key] = full_aligned
                    self._full_align_cache.move_to_end(a_key)
                    while len(self._full_align_cache) > 48:
                        self._full_align_cache.popitem(last=False)
                scoped_gens = [full_aligned[i] for i in scoped_indices]
                c._scoped_gens_all = scoped_gens
                aligned_recent = scoped_gens[-len(c.recent_steps):] if c.recent_steps else []

        if any(g is not None for g in aligned_recent):
            c.recent_ttfts = [g.ttft_sec if g is not None else None for g in aligned_recent]
            c.recent_tps_list = [g.tps if g is not None else None for g in aligned_recent]
            valid_ttfts = [t for t in c.recent_ttfts if t is not None]
            if valid_ttfts:
                c.ttft_avg_sec = sum(valid_ttfts) / len(valid_ttfts)
                c.ttft_max_sec = max(valid_ttfts)
            latest_gen = next((g for g in reversed(aligned_recent) if g is not None), None)
            if latest_gen is not None:
                c.ttft_sec = latest_gen.ttft_sec
                c.streaming_sec = latest_gen.streaming_sec
                c.tps = latest_gen.tps
                c.shard_tag = latest_gen.shard_tag
                c.speed_status = self._classify_speed(
                    latest_gen.output_tokens, latest_gen.tps, latest_gen.ttft_sec, c.context_tokens
                )
            return

        if account_scoped or is_tail_truncated:
            return

        valid_gens = tail_gens[-10:]
        c.recent_ttfts = [g.ttft_sec for g in valid_gens]
        c.recent_tps_list = [g.tps for g in valid_gens]
        valid_ttfts = [t for t in c.recent_ttfts if t is not None]
        if valid_ttfts:
            c.ttft_avg_sec = sum(valid_ttfts) / len(valid_ttfts)
            c.ttft_max_sec = max(valid_ttfts)
        if snap.latest:
            gen = snap.latest
            c.ttft_sec = gen.ttft_sec
            c.streaming_sec = gen.streaming_sec
            c.tps = gen.tps
            c.shard_tag = gen.shard_tag
            c.speed_status = self._classify_speed(gen.output_tokens, gen.tps, gen.ttft_sec, c.context_tokens)

    def poll(
        self,
        now_ts: Optional[float] = None,
        force_account: bool = False,
        target_conversation_id: str = "",
        view_account: str = "",
        history_range: str = "30m",
    ) -> MonitorSnapshot:
        with self._lock:
            return self._poll_locked(
                now_ts=now_ts,
                force_account=force_account,
                target_conversation_id=target_conversation_id,
                view_account=view_account,
                history_range=history_range,
            )

    def _poll_locked(
        self,
        now_ts: Optional[float] = None,
        force_account: bool = False,
        target_conversation_id: str = "",
        view_account: str = "",
        history_range: str = "30m",
    ) -> MonitorSnapshot:
        now = now_ts if now_ts is not None else time.time()
        self._log_tailer.poll(now)

        hr_key = (history_range or "30m").strip().lower()
        if hr_key not in HISTORY_RANGE_SPECS:
            hr_key = "30m"
        _hr_short, hr_label = HISTORY_RANGE_SPECS[hr_key]

        # Poll account liveness first so switch ledger & per-account step attribution stay up-to-date
        hub_dropped = self._account_cache.online and self._active_hub_pid > 0 and not _is_windows_pid_alive(self._active_hub_pid)
        account_interval = 15.0 if self._account_cache.online else 5.0
        if force_account or hub_dropped or (now - self._last_account_poll >= account_interval):
            self._account_cache = self._poll_account_and_quota(now, self._last_quota_family)
            self._last_account_poll = now
            if self._account_cache.online and self._account_cache.short_label:
                self._ledger.observe_online(self._account_cache.short_label, now)

        today_start = datetime.datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        win_sec = float(max(1, int(self.config["log_sliding_window_min"])) * 60)

        timeline = self._ledger.get_timeline(min(today_start, now - 7 * 86400.0))
        timeline_times = [t for t, _ in timeline]
        cur_acct_label = self._account_cache.short_label if self._account_cache.online else ""
        available_accounts = self._ledger.list_known_accounts(now - 7 * 86400.0, current_label=cur_acct_label)

        raw_view = (view_account or "").strip()
        if raw_view == "__all__":
            req_view = "__all__"
            resolved_account = ""
            is_custom_account_view = True
        elif raw_view:
            norm_view = _mask_short(raw_view) or raw_view
            # Match canonical casing from available_accounts when possible
            canon = next((a for a in available_accounts if a.lower() == norm_view.lower()), norm_view)
            req_view = canon
            resolved_account = canon
            is_custom_account_view = (not cur_acct_label) or (canon.lower() != cur_acct_label.lower())
        else:
            req_view = ""
            resolved_account = cur_acct_label
            is_custom_account_view = False

        acct_filter_fn: Optional[Callable[[float], bool]] = None
        if resolved_account and timeline:
            acct_filter_fn = self._ledger.build_account_matcher(
                resolved_account,
                timeline,
                fallback_label=cur_acct_label,
            )
        elif resolved_account and is_custom_account_view:
            acct_filter_fn = lambda _ev_ts: False

        # Determine whether the target account has older (>48h) timeline presence so 30m historical anchor never misses e.g. 52h-old T1 accounts
        acct_has_older_tl = bool(
            resolved_account
            and any(lbl.lower() == resolved_account.lower() and ts < now - 40 * 3600.0 for ts, lbl in timeline)
        )
        if hr_key == "7d":
            range_start_ts = now - 7 * 86400.0
            lookback_cutoff = range_start_ts - 3600.0
            max_older_convs = 160
        elif hr_key == "today":
            range_start_ts = today_start
            lookback_cutoff = min(today_start - 3600.0, now - 48 * 3600.0)
            max_older_convs = 48
        elif hr_key == "6h":
            range_start_ts = now - 6 * 3600.0
            lookback_cutoff = min(today_start - 3600.0, now - 48 * 3600.0)
            max_older_convs = 48
        else:  # "30m"
            range_start_ts = 0.0
            if is_custom_account_view or acct_has_older_tl:
                lookback_cutoff = now - 7 * 86400.0
                max_older_convs = 160
            else:
                lookback_cutoff = min(today_start - 3600.0, now - 48 * 3600.0)
                max_older_convs = 48

        switch_timestamps = self._ledger.get_switch_timestamps(lookback_cutoff)
        current_acct_since_ts = timeline[-1][0] if (resolved_account and not is_custom_account_view and len(timeline) >= 2) else 0.0

        target_query = (target_conversation_id or "").strip()
        target_query_low = target_query.lower()

        all_recent = self._iter_recent_transcripts(lookback_cutoff, today_start, max_older=max_older_convs)
        convs_bundle: List[Tuple[ConversationSummary, List[StepRecord], List[int]]] = []
        target_bundle: Optional[Tuple[ConversationSummary, List[StepRecord], List[int]]] = None
        collected_main_count = 0
        has_any_live_transcript = False
        latest_host_transcript_ts = 0.0

        total_today_steps = 0
        total_main_today_steps = 0
        total_subagent_today_steps = 0
        account_today_steps = 0
        account_main_today_steps = 0
        account_subagent_today_steps = 0
        range_steps = 0
        last_1h_steps = 0
        hourly_counts: Dict[int, int] = {}
        one_hour_cutoff = now - 3600.0
        eff_range_step_cutoff = range_start_ts if range_start_ts > 0.0 else (now - win_sec)

        for idx_conv, (conv_id, tp) in enumerate(all_recent):
            raw_steps, has_user_explicit, has_subagent_marker = self._read_transcript_cached(conv_id, tp)
            if not raw_steps:
                continue

            last_raw_ts = float(raw_steps[-1].get("timestamp", 0.0) or 0.0)
            if last_raw_ts > latest_host_transcript_ts:
                latest_host_transcript_ts = last_raw_ts
            if (now - last_raw_ts) <= 120.0:
                has_any_live_transcript = True

            if has_user_explicit:
                is_sub_conv = False
            elif has_subagent_marker:
                is_sub_conv = True
            else:
                is_sub_conv = self._check_subagent_hint_cached(conv_id)

            for s in raw_steps:
                sts = float(s.get("timestamp", 0.0) or 0.0)
                if sts <= 0.0:
                    continue
                if sts >= today_start:
                    total_today_steps += 1
                    if is_sub_conv:
                        total_subagent_today_steps += 1
                    else:
                        total_main_today_steps += 1

                in_scope = True if acct_filter_fn is None else acct_filter_fn(sts)
                if not in_scope:
                    continue

                if sts >= eff_range_step_cutoff:
                    range_steps += 1
                if sts >= today_start:
                    if resolved_account:
                        account_today_steps += 1
                        if is_sub_conv:
                            account_subagent_today_steps += 1
                        else:
                            account_main_today_steps += 1
                    if sts >= one_hour_cutoff:
                        last_1h_steps += 1
                    hr = datetime.datetime.fromtimestamp(sts).hour
                    hourly_counts[hr] = hourly_counts.get(hr, 0) + 1

            matches_target = bool(
                target_query_low
                and (conv_id.lower() == target_query_low or conv_id.lower().startswith(target_query_low))
            )
            scan_limit = 96 if (acct_filter_fn is not None or hr_key != "30m") else 24
            want_summary = idx_conv < scan_limit or (not is_sub_conv and collected_main_count < 16) or matches_target
            if not want_summary:
                continue

            built = self._build_conversation_summary(
                conv_id=conv_id,
                raw_steps=raw_steps,
                has_user_explicit=has_user_explicit,
                has_subagent_marker=has_subagent_marker or is_sub_conv,
                now=now,
                switch_timestamps=switch_timestamps,
                current_acct_since_ts=0.0 if acct_filter_fn is not None else current_acct_since_ts,
                account_filter_fn=acct_filter_fn,
                range_start_ts=range_start_ts,
            )
            if built is None:
                continue
            convs_bundle.append(built)
            if not built[0].is_subagent:
                collected_main_count += 1
            if matches_target and (target_bundle is None or conv_id.lower() == target_query_low):
                target_bundle = built

        if target_query_low and target_bundle is None:
            resolved_tp = self._resolve_conversation_transcript(target_query)
            if resolved_tp is not None:
                res_id, res_path = resolved_tp
                raw_steps, has_ue, has_sm = self._read_transcript_cached(res_id, res_path)
                if raw_steps:
                    target_bundle = self._build_conversation_summary(
                        conv_id=res_id,
                        raw_steps=raw_steps,
                        has_user_explicit=has_ue,
                        has_subagent_marker=has_sm,
                        now=now,
                        switch_timestamps=switch_timestamps,
                        current_acct_since_ts=0.0,
                        account_filter_fn=None,
                        range_start_ts=0.0,
                    )

        convs_bundle.sort(key=lambda b: b[0].latest_timestamp, reverse=True)
        protected_mains = [b for b in convs_bundle if not b[0].is_subagent][:12]
        seen_ids = {b[0].conversation_id for b in protected_mains}
        top_bundles: List[Tuple[ConversationSummary, List[StepRecord], List[int]]] = list(protected_mains)
        for b in convs_bundle:
            if len(top_bundles) >= 24:
                break
            if b[0].conversation_id not in seen_ids:
                top_bundles.append(b)
                seen_ids.add(b[0].conversation_id)
        top_bundles.sort(key=lambda b: b[0].latest_timestamp, reverse=True)

        is_acct_scoped = acct_filter_fn is not None
        need_wide_gens = hr_key != "30m"
        for c, all_st, sc_idx in top_bundles:
            self._enrich_conv_from_db(
                c,
                all_steps=all_st,
                scoped_indices=sc_idx,
                account_scoped=is_acct_scoped,
                need_full_window_gens=need_wide_gens,
            )
        target_conv: Optional[ConversationSummary] = target_bundle[0] if target_bundle is not None else None
        if target_bundle is not None and target_bundle[0].conversation_id not in seen_ids:
            self._enrich_conv_from_db(
                target_bundle[0],
                all_steps=target_bundle[1],
                scoped_indices=target_bundle[2],
                account_scoped=False,
            )

        top_convs = [b[0] for b in top_bundles]
        convs = [b[0] for b in convs_bundle]

        main_convs = [c for c in top_convs if not c.is_subagent]
        primary = main_convs[0] if main_convs else (top_convs[0] if top_convs else None)
        active_family = (
            "3p"
            if (primary and any(k in primary.model_raw.lower() for k in ("claude", "fable", "opus", "sonnet", "gpt")))
            else "gemini"
        )
        if not is_custom_account_view and active_family != self._last_quota_family:
            self._last_quota_family = active_family
            if self._account_cache.pools:
                self._sync_account_active_pool(self._account_cache, active_family)
            else:
                self._account_cache = self._poll_account_and_quota(now, active_family)
                self._last_account_poll = now
                if self._account_cache.online and self._account_cache.short_label:
                    self._ledger.observe_online(self._account_cache.short_label, now)

        if resolved_account:
            steps_scope_label = resolved_account
            log_scope_label = resolved_account
            today_steps = account_today_steps
            main_today_steps = account_main_today_steps
            subagent_today_steps = account_subagent_today_steps
        else:
            steps_scope_label = "全机"
            log_scope_label = "全机"
            today_steps = total_today_steps
            main_today_steps = total_main_today_steps
            subagent_today_steps = total_subagent_today_steps

        peak_hour_steps = 0
        peak_hour_label = ""
        if hourly_counts:
            peak_hr, peak_hour_steps = max(hourly_counts.items(), key=lambda kv: (kv[1], kv[0]))
            peak_hour_label = f"{peak_hr:02d}:00-{(peak_hr + 1) % 24:02d}:00"

        # Large-context freshness applies ONLY to live real-time current-account monitoring (never jump green countdown in historical views)
        if is_custom_account_view or hr_key != "30m":
            large_freshness: List[ConversationSummary] = []
        else:
            large_freshness = [
                c
                for c in convs
                if (not c.is_subagent) and (c.freshness_remaining_sec is not None or c.freshness_invalid_reason)
            ]
            large_freshness.sort(
                key=lambda c: (
                    0 if c.freshness_invalid_reason else (1 if (c.freshness_remaining_sec is not None and c.freshness_remaining_sec > 0) else 2),
                    c.freshness_remaining_sec if (c.freshness_remaining_sec is not None and c.freshness_remaining_sec > 0) else (-c.latest_timestamp),
                )
            )

        min_fresh_sec: Optional[int] = None
        min_fresh_conv = ""
        min_fresh_title = ""
        min_fresh_ctx = 0
        min_fresh_invalid = ""
        if large_freshness:
            head_f = large_freshness[0]
            min_fresh_sec = head_f.freshness_remaining_sec
            min_fresh_conv = head_f.short_id
            min_fresh_title = head_f.title
            min_fresh_ctx = head_f.context_tokens
            min_fresh_invalid = head_f.freshness_invalid_reason

        machine_today_log = self._log_tailer.get_machine_today_counts(now)

        is_historical_rating = False
        rating_anchor_ts = now
        historical_tag = ""
        prior_hist_warning = ""

        if hr_key == "30m":
            live_win_start = now - win_sec
            live_eval_floor = max(live_win_start, current_acct_since_ts) if current_acct_since_ts > 0.0 else live_win_start
            live_log, today_log, live_notable = self._log_tailer.get_counts(
                now,
                self.config["log_sliding_window_min"],
                account_filter_fn=acct_filter_fn,
                range_start_ts=live_win_start,
                range_end_ts=now,
            )
            live_active = [c for c in top_convs if c.latest_timestamp >= live_win_start]
            has_live_acct_activity = bool(live_active) or (live_log["503"] + live_log["timeout"] + live_log["403"] > 0)

            if has_live_acct_activity:
                # Recompute miss counters strictly inside current live session floor so yesterday's misses never leak into today's live T1!
                for c in live_active:
                    self._recompute_conv_eval_stats(c, live_eval_floor)
                recent_active = live_active
                recent_log = live_log
                notable_events = live_notable
                eval_cutoff_ts = live_eval_floor
                eval_end_ts = now
                last_t2_error_ts = self._log_tailer.get_latest_error_ts(
                    now,
                    self.config["log_sliding_window_min"],
                    categories=("503", "timeout"),
                    account_filter_fn=acct_filter_fn,
                    range_start_ts=live_win_start,
                    range_end_ts=now,
                )
                # Guard against 1-step pure expected_cold (0 cacheable verification) washing away an account's prior historical T1/T2!
                live_cacheable = sum(c.cacheable_in_recent10 for c in live_active)
                if live_cacheable == 0 and (live_log["503"] + live_log["timeout"] + live_log["403"] == 0):
                    # Check if this account had prior degraded history before live_eval_floor
                    prior_steps_ts = max(
                        (
                            s.timestamp
                            for c in top_convs
                            for s in c._scoped_steps_all
                            if s.timestamp < live_eval_floor - 2.0
                        ),
                        default=0.0,
                    )
                    prior_err_ts = self._log_tailer.get_latest_account_event_ts(
                        account_filter_fn=acct_filter_fn,
                        since_ts=lookback_cutoff,
                    )
                    if prior_err_ts >= live_eval_floor - 2.0:
                        prior_err_ts = 0.0
                    p_anchor = max(prior_steps_ts, prior_err_ts)
                    if p_anchor > 0.0:
                        p_win_s = p_anchor - win_sec
                        p_log, _, _ = self._log_tailer.get_counts(
                            now,
                            self.config["log_sliding_window_min"],
                            account_filter_fn=acct_filter_fn,
                            range_start_ts=p_win_s,
                            range_end_ts=p_anchor,
                        )
                        p_consec = 0
                        for c in top_convs:
                            p_st = [s for s in c._scoped_steps_all if p_win_s <= s.timestamp <= p_anchor + 2.0]
                            cm = 0
                            for s in reversed(p_st):
                                if s.classification == "abnormal_miss":
                                    cm += 1
                                else:
                                    break
                            p_consec = max(p_consec, cm)
                        if p_log["403"] > 0:
                            prior_hist_warning = f"前序历史曾触发 403（{format_relative_age(now - p_anchor)}），首步冷启尚未验真"
                        elif (p_log["timeout"] + p_log["503"]) >= 2:
                            prior_hist_warning = f"前序历史曾触发 T2 限流（{format_relative_age(now - p_anchor)}），首步冷启待次拍验真"
                        elif p_consec >= 2:
                            prior_hist_warning = f"前序历史曾连续 {p_consec} 步掉缓存 T1（{format_relative_age(now - p_anchor)}），首步冷启待次拍验真"
                if is_custom_account_view:
                    is_historical_rating = True
                    historical_tag = f"历史视角 · {resolved_account or '全机'}"
            else:
                latest_step_ts = top_convs[0].latest_timestamp if top_convs else 0.0
                latest_err_ts = self._log_tailer.get_latest_account_event_ts(
                    account_filter_fn=acct_filter_fn,
                    since_ts=lookback_cutoff,
                )
                anchor_ts = max(latest_step_ts, latest_err_ts)
                if anchor_ts > 0.0:
                    is_historical_rating = True
                    rating_anchor_ts = anchor_ts
                    eval_cutoff_ts = anchor_ts - win_sec
                    eval_end_ts = anchor_ts
                    recent_active = [c for c in top_convs if c.latest_timestamp >= eval_cutoff_ts]
                    for c in recent_active:
                        self._recompute_conv_eval_stats(c, eval_cutoff_ts)
                    recent_log, today_log, notable_events = self._log_tailer.get_counts(
                        now,
                        self.config["log_sliding_window_min"],
                        account_filter_fn=acct_filter_fn,
                        range_start_ts=eval_cutoff_ts,
                        range_end_ts=eval_end_ts,
                    )
                    last_t2_error_ts = self._log_tailer.get_latest_error_ts(
                        now,
                        self.config["log_sliding_window_min"],
                        categories=("503", "timeout"),
                        account_filter_fn=acct_filter_fn,
                        range_start_ts=eval_cutoff_ts,
                        range_end_ts=eval_end_ts,
                    )
                    historical_tag = f"历史参考 · {format_relative_age(now - anchor_ts)}"
                else:
                    recent_active = []
                    recent_log = live_log
                    notable_events = []
                    eval_cutoff_ts = live_win_start
                    eval_end_ts = now
                    last_t2_error_ts = 0.0
        else:
            is_historical_rating = True
            eval_cutoff_ts = range_start_ts
            eval_end_ts = now
            recent_active = list(top_convs)
            recent_log, today_log, notable_events = self._log_tailer.get_counts(
                now,
                self.config["log_sliding_window_min"],
                account_filter_fn=acct_filter_fn,
                range_start_ts=eval_cutoff_ts,
                range_end_ts=eval_end_ts,
            )
            last_t2_error_ts = self._log_tailer.get_latest_error_ts(
                now,
                self.config["log_sliding_window_min"],
                categories=("503", "timeout"),
                account_filter_fn=acct_filter_fn,
                range_start_ts=eval_cutoff_ts,
                range_end_ts=eval_end_ts,
            )
            rating_anchor_ts = max(
                top_convs[0].latest_timestamp if top_convs else 0.0,
                last_t2_error_ts,
            ) or now
            historical_tag = f"{hr_label}汇总"

        eval_mains = [c for c in recent_active if not c.is_subagent]
        eval_primary = eval_mains[0] if eval_mains else (recent_active[0] if recent_active else primary)
        active_model = eval_primary.model if eval_primary else "等待请求"
        primary_hit = eval_primary.latest_hit_rate if eval_primary else None
        primary_hit_cls = eval_primary.recent_steps[-1].classification if (eval_primary and eval_primary.recent_steps) else ""
        primary_tps = eval_primary.tps if eval_primary else None
        primary_ttft = eval_primary.ttft_sec if eval_primary else None
        primary_speed = eval_primary.speed_status if eval_primary else "unknown"

        max_consec_miss = max((c.consec_miss for c in recent_active), default=0)
        any_single_miss = any(c.single_miss for c in recent_active)
        any_slow = any(c.speed_status == "slow" for c in recent_active[:2])
        wasted_total = sum(c.wasted_cold_tokens for c in recent_active)
        max_abnormal_r10 = max((c.abnormal_in_recent10 for c in recent_active), default=0)
        severe_frequent_miss_conv = next(
            (
                c
                for c in recent_active
                if c.abnormal_in_recent10 >= 4
                and c.cacheable_in_recent10 >= 4
                and (c.recent10_hit_ratio is not None and c.recent10_hit_ratio < 0.5)
            ),
            None,
        )
        unstable_recovery_conv = next(
            (c for c in recent_active if c.abnormal_in_recent10 >= 2),
            None,
        )
        ratios = [c.recent10_hit_ratio for c in recent_active if c.cacheable_in_recent10 >= 2 and c.recent10_hit_ratio is not None]
        worst_hit_ratio = min(ratios) if ratios else None

        notable_events = self._annotate_log_events_with_conversations(notable_events, top_convs)
        latest_timeout_ev = next((ev for ev in reversed(notable_events) if ev.category == "timeout"), None)
        latest_timeout_summary = latest_timeout_ev.summary if latest_timeout_ev is not None else ""

        slot_inv = self.read_slot_inventory(now_ts=now, include_items=False, force=False)

        model_stats = self._compute_recent_model_stats(
            recent_active=recent_active,
            cutoff_ts=eval_cutoff_ts,
            last_t2_error_ts=last_t2_error_ts,
            use_full_scoped=(hr_key != "30m"),
        )

        eff_online = self._account_cache.online or is_custom_account_view or (hr_key != "30m")
        level, grade, headline, sub_headline, t2_recovery_note = self._evaluate_health(
            active_model=active_model,
            recent_log=recent_log,
            today_log=today_log,
            max_consec_miss=max_consec_miss,
            any_single_miss=any_single_miss,
            any_slow=any_slow,
            today_steps=today_steps,
            primary=eval_primary,
            is_online=eff_online,
            has_live_transcript=has_any_live_transcript,
            severe_frequent_miss_conv=severe_frequent_miss_conv,
            unstable_recovery_conv=unstable_recovery_conv,
            model_stats=model_stats,
            last_t2_error_ts=last_t2_error_ts,
            now_ts=eval_end_ts,
            switched_acct_label=resolved_account if (resolved_account and not is_custom_account_view and current_acct_since_ts > 0.0) else "",
            custom_empty_acct=resolved_account if is_custom_account_view else "",
            is_historical=is_historical_rating,
            historical_tag=historical_tag,
            history_range_label=hr_label,
            prior_hist_warning=prior_hist_warning,
            latest_timeout_summary=latest_timeout_summary,
        )

        # Offline + custom/history lookup yielding no records -> offline_unknown (exit 2) rather than normal (exit 0)
        if grade == "WAIT" and not self._account_cache.online:
            pressure = "offline_unknown"
        else:
            pressure = compute_pressure(
                grade=grade,
                today_steps=today_steps,
                daily_steps_warn=self.config["daily_steps_warn"],
                daily_steps_alert=self.config["daily_steps_alert"],
            )

        alerts = self._collect_new_alerts(
            grade=grade,
            notable_events=notable_events,
            recent_active=recent_active,
            max_consec_miss=max_consec_miss,
            today_steps=today_steps,
            steps_scope_label=steps_scope_label,
            is_online=self._account_cache.online,
            severe_frequent_miss_conv=severe_frequent_miss_conv,
            slot_inventory=slot_inv,
            slots_only=bool(is_custom_account_view or hr_key != "30m" or is_historical_rating),
        )

        primary_ttft_avg = eval_primary.ttft_avg_sec if eval_primary else None
        primary_ttft_max = eval_primary.ttft_max_sec if eval_primary else None
        primary_recent_ttfts = list(eval_primary.recent_ttfts) if eval_primary else []

        return MonitorSnapshot(
            generated_at=now,
            level=level,
            grade=grade,
            headline=headline,
            sub_headline=sub_headline,
            active_model=active_model,
            primary_hit_rate=primary_hit,
            primary_hit_classification=primary_hit_cls,
            primary_tps=primary_tps,
            primary_ttft=primary_ttft,
            primary_speed_status=primary_speed,
            min_freshness_sec=min_fresh_sec,
            min_freshness_conv=min_fresh_conv,
            min_freshness_ctx=min_fresh_ctx,
            min_freshness_invalid_reason=min_fresh_invalid,
            today_steps=today_steps,
            daily_steps_warn=self.config["daily_steps_warn"],
            daily_steps_alert=self.config["daily_steps_alert"],
            active_conversations=top_convs,
            large_freshness_list=large_freshness[:12],
            recent_503=recent_log["503"],
            today_503=today_log["503"],
            recent_timeout=recent_log["timeout"],
            today_timeout=today_log["timeout"],
            recent_403=recent_log["403"],
            today_403=today_log["403"],
            recent_retry=recent_log["retry"],
            today_retry=today_log["retry"],
            recent_events=notable_events,
            wasted_cold_tokens_total=wasted_total,
            max_consec_miss=max_consec_miss,
            account=self._account_cache,
            sliding_window_min=self.config["log_sliding_window_min"],
            freshness_window_sec=self.config["freshness_window_sec"],
            alerts=alerts,
            primary_ttft_avg=primary_ttft_avg,
            primary_ttft_max=primary_ttft_max,
            primary_recent_ttfts=primary_recent_ttfts,
            account_today_steps=account_today_steps,
            total_today_steps=total_today_steps,
            steps_scope_label=steps_scope_label,
            max_abnormal_recent10=max_abnormal_r10,
            worst_recent10_hit_ratio=worst_hit_ratio,
            pressure=pressure,
            main_today_steps=main_today_steps,
            subagent_today_steps=subagent_today_steps,
            total_main_today_steps=total_main_today_steps,
            total_subagent_today_steps=total_subagent_today_steps,
            last_1h_steps=last_1h_steps,
            peak_hour_steps_today=peak_hour_steps,
            peak_hour_label=peak_hour_label,
            target_conversation=target_conv,
            target_conversation_query=target_query,
            min_freshness_title=min_fresh_title,
            model_stats=model_stats,
            log_scope_label=log_scope_label,
            machine_today_503=machine_today_log["503"],
            machine_today_timeout=machine_today_log["timeout"],
            machine_today_403=machine_today_log["403"],
            machine_today_retry=machine_today_log["retry"],
            last_t2_error_ts=last_t2_error_ts,
            t2_recovery_note=t2_recovery_note,
            available_accounts=available_accounts,
            view_account=req_view,
            resolved_account=resolved_account,
            is_custom_account_view=is_custom_account_view,
            history_range=hr_key,
            history_range_label=hr_label,
            is_historical_rating=is_historical_rating,
            rating_anchor_ts=rating_anchor_ts,
            range_steps=range_steps,
            slot_inventory=slot_inv,
            is_host_idle_offline=bool((not self._account_cache.online) and ((now - latest_host_transcript_ts) >= 15.0)),
        )

    def _is_cached_subagent(self, cid: str, conv_map: Dict[str, ConversationSummary]) -> bool:
        conv_obj = conv_map.get(cid)
        if conv_obj is not None:
            return bool(conv_obj.is_subagent)
        cached = self._transcript_cache.get(cid)
        if cached is not None:
            _, _, has_ue, has_sm = cached
            if has_ue:
                return False
            if has_sm:
                return True
        return self._check_subagent_hint_cached(cid)

    def _annotate_log_events_with_conversations(
        self,
        events: List[LogEvent],
        known_convs: List[ConversationSummary],
    ) -> List[LogEvent]:
        """Match language_server.log error timestamps against per-conversation ERROR_MESSAGE timestamps (±6s)."""
        if not events or not self._transcript_error_ts:
            return events
        conv_map = {c.conversation_id: c for c in known_convs}
        out: List[LogEvent] = []
        for ev in events:
            if ev.category not in ("timeout", "503", "403"):
                out.append(ev)
                continue
            best_cid = ""
            best_key: Optional[Tuple[int, int, float]] = None
            for cid, err_entries in self._transcript_error_ts.items():
                if not err_entries:
                    continue
                is_sub = 1 if self._is_cached_subagent(cid, conv_map) else 0
                for ets, etxt in err_entries:
                    diff = abs(ets - ev.timestamp)
                    if diff > 6.0:
                        continue
                    # Prefer content-relevant ERROR_MESSAGE matches over unrelated tool errors in the same second
                    if ev.category == "timeout":
                        rel_rank = 0 if ("timeout" in etxt or "deadline" in etxt or "timed out" in etxt) else 1
                    elif ev.category == "503":
                        rel_rank = 0 if ("503" in etxt or "capacity" in etxt or "unavailable" in etxt or "exhausted" in etxt) else 1
                    else:
                        rel_rank = 0 if ("403" in etxt or "permission" in etxt or "tos" in etxt) else 1
                    cand_key = (rel_rank, is_sub, diff)
                    if best_key is None or cand_key < best_key:
                        best_key = cand_key
                        best_cid = cid
            if best_cid:
                conv_obj = conv_map.get(best_cid)
                title = conv_obj.title if conv_obj is not None else self._resolve_conv_title(best_cid)
                title_part = f" {truncate_display_width(title, 22)}" if title else ""
                tag = f"{best_cid[:6]}{title_part}"
                new_summary = ev.summary if tag in ev.summary else f"{ev.summary} · {tag}"
                out.append(
                    LogEvent(
                        timestamp=ev.timestamp,
                        category=ev.category,
                        summary=new_summary,
                        event_key=ev.event_key,
                        model=ev.model,
                    )
                )
            else:
                out.append(ev)
        return out

    @staticmethod
    def _compute_recent_model_stats(
        recent_active: List[ConversationSummary],
        cutoff_ts: float,
        last_t2_error_ts: float = 0.0,
        use_full_scoped: bool = False,
    ) -> List[ModelPerfStats]:
        """Aggregate per-model TTFT, TPS, cache hits, and post-503 recovery evidence."""
        buckets: Dict[str, Dict[str, Any]] = {}
        for c in recent_active:
            if use_full_scoped and c._scoped_steps_all:
                steps = c._scoped_steps_all
                gens = c._scoped_gens_all if len(c._scoped_gens_all) == len(steps) else []
                ttfts = [g.ttft_sec if g is not None else None for g in gens] if gens else c.recent_ttfts
                tpss = [g.tps if g is not None else None for g in gens] if gens else c.recent_tps_list
            else:
                steps = c.recent_steps[-10:]
                ttfts = c.recent_ttfts[-len(steps):] if c.recent_ttfts else []
                tpss = c.recent_tps_list[-len(steps):] if c.recent_tps_list else []
            t_off = len(steps) - len(ttfts)
            p_off = len(steps) - len(tpss)
            for idx, st in enumerate(steps):
                if st.timestamp < cutoff_ts:
                    continue
                mname = format_model_name(st.model)
                b = buckets.setdefault(
                    mname,
                    {
                        "steps": 0,
                        "hits": 0,
                        "abn": 0,
                        "ttfts": [],
                        "tpss": [],
                        "latest_ts": 0.0,
                        "latest_ttft": None,
                        "post_steps": 0,
                        "post_ttfts": [],
                        "post_latest_ts": 0.0,
                        "post_latest_ttft": None,
                        "main_post_steps": 0,
                        "main_post_pairs": [],  # (timestamp, ttft_sec)
                    },
                )
                b["steps"] += 1
                if st.classification == "hit":
                    b["hits"] += 1
                elif st.classification == "abnormal_miss":
                    b["abn"] += 1
                ti = idx - t_off
                pi = idx - p_off
                t_val = ttfts[ti] if 0 <= ti < len(ttfts) else None
                p_val = tpss[pi] if 0 <= pi < len(tpss) else None
                if t_val is not None:
                    b["ttfts"].append(float(t_val))
                if p_val is not None:
                    b["tpss"].append(float(p_val))
                if st.timestamp >= b["latest_ts"]:
                    b["latest_ts"] = st.timestamp
                    if t_val is not None:
                        b["latest_ttft"] = float(t_val)
                if last_t2_error_ts > 0.0 and st.timestamp > last_t2_error_ts + 1.0:
                    b["post_steps"] += 1
                    if t_val is not None:
                        b["post_ttfts"].append(float(t_val))
                        if st.timestamp >= b["post_latest_ts"]:
                            b["post_latest_ts"] = st.timestamp
                            b["post_latest_ttft"] = float(t_val)
                    if not c.is_subagent:
                        b["main_post_steps"] += 1
                        if t_val is not None:
                            b["main_post_pairs"].append((st.timestamp, float(t_val)))

        out: List[ModelPerfStats] = []
        for mname, b in buckets.items():
            tl = b["ttfts"]
            pl = b["tpss"]
            ptl = b["post_ttfts"]
            ptl_median: Optional[float] = None
            if ptl:
                sp = sorted(ptl)
                mid = len(sp) // 2
                ptl_median = sp[mid] if (len(sp) % 2 == 1) else ((sp[mid - 1] + sp[mid]) / 2.0)

            mpairs = sorted(b["main_post_pairs"], key=lambda x: x[0])
            mvals = [v for _, v in mpairs]
            m_median: Optional[float] = None
            if mvals:
                sm = sorted(mvals)
                mm = len(sm) // 2
                m_median = sm[mm] if (len(sm) % 2 == 1) else ((sm[mm - 1] + sm[mm]) / 2.0)
            m_latest = mvals[-1] if mvals else None
            m_rec2_max = max(mvals[-2:]) if len(mvals) >= 2 else m_latest

            out.append(
                ModelPerfStats(
                    model=mname,
                    step_count=int(b["steps"]),
                    hit_count=int(b["hits"]),
                    abnormal_miss_count=int(b["abn"]),
                    ttft_avg_sec=(sum(tl) / len(tl)) if tl else None,
                    ttft_max_sec=max(tl) if tl else None,
                    latest_ttft_sec=b["latest_ttft"],
                    tps_avg=(sum(pl) / len(pl)) if pl else None,
                    latest_timestamp=float(b["latest_ts"]),
                    steps_after_last_t2=int(b["post_steps"]),
                    ttft_after_last_t2_avg=(sum(ptl) / len(ptl)) if ptl else None,
                    ttft_after_last_t2_latest=b["post_latest_ttft"],
                    ttft_after_last_t2_median=ptl_median,
                    main_steps_after_last_t2=int(b["main_post_steps"]),
                    main_ttft_after_last_t2_median=m_median,
                    main_ttft_after_last_t2_latest=m_latest,
                    main_ttft_after_last_t2_recent2_max=m_rec2_max,
                )
            )
        out.sort(key=lambda m: m.latest_timestamp, reverse=True)
        return out

    def _classify_speed(
        self,
        output_tokens: int,
        tps: Optional[float],
        ttft_sec: Optional[float],
        context_tokens: int,
    ) -> str:
        if tps is None and ttft_sec is None:
            return "unknown"
        if output_tokens >= 350 and tps is not None and tps < self.config["slow_tps_threshold"]:
            return "slow"
        if ttft_sec is not None and ttft_sec >= self.config["slow_ttft_threshold_sec"] and context_tokens < 220000:
            return "slow"
        return "ok"

    def _evaluate_health(
        self,
        active_model: str,
        recent_log: Dict[str, int],
        today_log: Dict[str, int],
        max_consec_miss: int,
        any_single_miss: bool,
        any_slow: bool,
        today_steps: int,
        primary: Optional[ConversationSummary],
        is_online: bool = True,
        has_live_transcript: bool = True,
        severe_frequent_miss_conv: Optional[ConversationSummary] = None,
        unstable_recovery_conv: Optional[ConversationSummary] = None,
        model_stats: Optional[List[ModelPerfStats]] = None,
        last_t2_error_ts: float = 0.0,
        now_ts: float = 0.0,
        switched_acct_label: str = "",
        custom_empty_acct: str = "",
        is_historical: bool = False,
        historical_tag: str = "",
        history_range_label: str = "近 30 分钟",
        prior_hist_warning: str = "",
        latest_timeout_summary: str = "",
    ) -> Tuple[str, str, str, str, str]:
        win = self.config["log_sliding_window_min"]
        scope_win_txt = history_range_label if is_historical and history_range_label != "近 30 分钟" else f"近 {win} 分钟"
        hist_prefix = f"[{historical_tag}] " if (is_historical and historical_tag) else ""
        hist_head_tag = " (历史)" if is_historical else ""

        # When Antigravity desktop client is not running (no live hub) and no very recent (<2m) active steps,
        # stay quietly in standby instead of alerting on stale pre-shutdown log disconnects.
        if not is_online and not has_live_transcript:
            return ("green", "IDLE", "离线待机 · Antigravity 未运行", "客户端未启动，已暂停实时异常告警", "")

        # Only evaluate live language_server.log T2/T3 alerts while online or inspecting history.
        eff_403 = recent_log["403"] if is_online else 0
        eff_timeout = recent_log["timeout"] if is_online else 0
        eff_503 = recent_log["503"] if is_online else 0

        if eff_403 > 0:
            return (
                "red",
                "T3",
                f"T3 封禁告警{hist_head_tag} · {active_model}",
                f"{hist_prefix}检测到 403 TOS/权限拒绝，请立即停止使用该账号",
                "",
            )

        # Dynamic T2 evaluation:
        # - Single isolated stream timeout (or single 503) warns in orange (WARN) instead of hard-alarming T2.
        # - (eff_503 + eff_timeout) >= 2 enters T2 evaluation using MAIN-conversation post-error median TTFT:
        #   1. Subagents & lightweight immune models (Hydrogen/Flash) NEVER whitewash flagship (Neon/Carbon) T2!
        #   2. If the active primary model itself shows post-error degradation, no other model can override it.
        #   3. When primary has >=4 main post-steps and median < 30s, tolerate a single multimodal TTFT spike (<150s)
        #      unless the latest two steps are both >=60s (prevents WARN <-> T2 flapping).
        #   4. Full green T0 recovery requires actual measured TTFT samples on the active model (never via another model).
        t2_active = False
        t2_recovering_warn = ""
        t2_recovered_info = ""
        if (eff_timeout + eff_503) >= 2:
            stats_list = model_stats or []
            active_is_light = _is_light_immune_model(active_model)

            def _eff_steps(ms: ModelPerfStats) -> int:
                return ms.main_steps_after_last_t2 if ms.main_steps_after_last_t2 > 0 else (ms.steps_after_last_t2 if ms.main_ttft_after_last_t2_median is None and not any(m.main_steps_after_last_t2 > 0 for m in stats_list) else 0)

            def _eff_rep_ttft(ms: ModelPerfStats) -> Optional[float]:
                if ms.main_steps_after_last_t2 > 0:
                    return ms.main_ttft_after_last_t2_median
                return ms.ttft_after_last_t2_median if ms.ttft_after_last_t2_median is not None else ms.ttft_after_last_t2_avg

            def _eff_latest_ttft(ms: ModelPerfStats) -> Optional[float]:
                return ms.main_ttft_after_last_t2_latest if ms.main_steps_after_last_t2 > 0 else ms.ttft_after_last_t2_latest

            def _is_degraded(ms: ModelPerfStats) -> bool:
                steps_n = ms.main_steps_after_last_t2
                if steps_n <= 0:
                    return False
                rep = ms.main_ttft_after_last_t2_median
                lat = ms.main_ttft_after_last_t2_latest
                rec2 = ms.main_ttft_after_last_t2_recent2_max
                if rep is not None and rep >= 45.0:
                    return True
                if steps_n >= 4 and rep is not None and rep < 30.0:
                    # Tolerate single multimodal spike; only degraded if extreme (>=150s) or consecutive slow
                    if lat is not None and lat >= 150.0:
                        return True
                    return False
                return bool(lat is not None and lat >= 60.0 and (rec2 is None or rec2 >= 60.0))

            def _is_healthy_main_recovery(ms: ModelPerfStats, allow_no_ttft: bool = False) -> bool:
                steps_n = ms.main_steps_after_last_t2
                if steps_n < 2:
                    return False
                rep = ms.main_ttft_after_last_t2_median
                lat = ms.main_ttft_after_last_t2_latest
                if rep is None and not allow_no_ttft:
                    return False
                if rep is not None and rep >= 45.0:
                    return False
                if steps_n >= 4 and rep is not None and rep < 30.0:
                    # Single multimodal spike tolerance: allow up to 150s if previous step was fast
                    if lat is not None and lat >= 150.0:
                        return False
                    return True
                if lat is not None and lat >= 60.0:
                    return False
                return True

            primary_ms = next((m for m in stats_list if m.model == active_model), None)
            primary_degraded = primary_ms is not None and _is_degraded(primary_ms)
            elapsed_since_err = (now_ts - last_t2_error_ts) if (now_ts > 0 and last_t2_error_ts > 0) else 0.0
            err_parts = []
            if eff_503 > 0:
                err_parts.append(f"{eff_503} 次 503")
            if eff_timeout > 0:
                err_parts.append(f"{eff_timeout} 次流超时")
            err_desc = " / ".join(err_parts) if err_parts else "限流事件"

            if primary_ms is not None and _is_healthy_main_recovery(primary_ms, allow_no_ttft=False):
                rep = _eff_rep_ttft(primary_ms)
                ttft_desc = f"，TTFT 中位 {rep:.1f}s" if rep is not None else ""
                p_steps = primary_ms.main_steps_after_last_t2
                if p_steps >= 6 and elapsed_since_err >= 600.0:
                    t2_recovered_info = f"前序 {err_desc} 已平复（{primary_ms.model} 事后连跑 {p_steps} 步正常{ttft_desc}）"
                else:
                    t2_recovering_warn = f"前序 {err_desc}，{primary_ms.model} 事后已连跑 {p_steps} 步正常{ttft_desc}"
            elif primary_ms is not None and _is_healthy_main_recovery(primary_ms, allow_no_ttft=True):
                # Main steps succeeded without SQLite TTFT yet -> allow downgrade to WARN, never green T0
                p_steps = primary_ms.main_steps_after_last_t2
                t2_recovering_warn = f"前序 {err_desc}，{primary_ms.model} 事后已连跑 {p_steps} 步（待 TTFT 采样验真）"
            elif not primary_degraded:
                # Only allow a non-lightweight main-conversation peer model to soften T2 -> WARN (never to green T0!)
                peer_candidates = [
                    m for m in stats_list
                    if m.model != active_model
                    and (active_is_light or not _is_light_immune_model(m.model))
                    and _is_healthy_main_recovery(m, allow_no_ttft=False)
                ]
                if peer_candidates:
                    best_peer = max(peer_candidates, key=lambda m: (m.main_steps_after_last_t2, m.latest_timestamp))
                    rep = _eff_rep_ttft(best_peer)
                    ttft_desc = f"，TTFT 中位 {rep:.1f}s" if rep is not None else ""
                    t2_recovering_warn = (
                        f"前序 {err_desc}，另一主模型 {best_peer.model} 已连跑 {best_peer.main_steps_after_last_t2} 步可用{ttft_desc}"
                    )
                else:
                    t2_active = True
            else:
                t2_active = True

        if t2_active:
            extra = ""
            if primary is not None and primary.ttft_sec is not None:
                extra = f"（TTFT {primary.ttft_sec:.1f}s）"
            return (
                "red",
                "T2",
                f"T2 限流黑洞{hist_head_tag} · {active_model}",
                f"{hist_prefix}{scope_win_txt}触发 {eff_503} 次 503 / {eff_timeout} 次流超时，事后未恢复稳定响应{extra}",
                "",
            )
        if max_consec_miss >= 2:
            return (
                "red",
                "T1",
                f"T1 连续掉缓存{hist_head_tag} · {active_model}",
                f"{hist_prefix}活跃会话已连续 {max_consec_miss} 步 cr=0，建议检查账号或切回主力号",
                t2_recovering_warn or t2_recovered_info,
            )
        if severe_frequent_miss_conv is not None:
            pct = int(round((severe_frequent_miss_conv.recent10_hit_ratio or 0.0) * 100))
            return (
                "red",
                "T1",
                f"T1 频繁掉缓存{hist_head_tag} · {active_model}",
                f"{hist_prefix}近 10 步异常归零 {severe_frequent_miss_conv.abnormal_in_recent10}/{severe_frequent_miss_conv.cacheable_in_recent10} 步（有效命中仅 {pct}%），单步命中未恢复健康",
                t2_recovering_warn or t2_recovered_info,
            )
        if today_steps >= self.config["daily_steps_alert"]:
            return (
                "red",
                "WARN",
                f"步数超红线{hist_head_tag} · {active_model}",
                f"{hist_prefix}今日累计 {today_steps} 步已达 {self.config['daily_steps_alert']} 步保护红线，建议减速防降权",
                t2_recovering_warn or t2_recovered_info,
            )
        if (
            prior_hist_warning
            or t2_recovering_warn
            or any_single_miss
            or unstable_recovery_conv is not None
            or eff_503 == 1
            or eff_timeout == 1
            or any_slow
            or today_steps >= self.config["daily_steps_warn"]
        ):
            reasons = []
            if prior_hist_warning:
                reasons.append(prior_hist_warning)
            if t2_recovering_warn:
                reasons.append(t2_recovering_warn)
            if any_single_miss:
                reasons.append("单步异常归零")
            elif unstable_recovery_conv is not None:
                reasons.append(f"近10步 {unstable_recovery_conv.abnormal_in_recent10} 次掉缓存(观察恢复中)")
            if any_slow:
                reasons.append("疑似暗限速")
            if eff_503 == 1 and not t2_recovering_warn:
                reasons.append("偶发 1 次 503")
            if eff_timeout == 1 and not t2_recovering_warn:
                reasons.append(f"偶发流超时: {latest_timeout_summary}" if latest_timeout_summary else "偶发 1 次流超时")
            if today_steps >= self.config["daily_steps_warn"]:
                reasons.append(f"今日步数 {today_steps}")
            return ("orange", "WARN", f"留意预警{hist_head_tag} · {active_model}", f"{hist_prefix}" + " / ".join(reasons), t2_recovering_warn)

        if primary is None:
            if custom_empty_acct:
                return (
                    "cyan",
                    "WAIT",
                    f"历史视角 · {custom_empty_acct} 暂无记录",
                    f"{hist_prefix}{scope_win_txt}该账号无可用对话或日志记录",
                    "",
                )
            if switched_acct_label:
                return (
                    "cyan",
                    "WAIT",
                    f"待采样 · 已切入 {switched_acct_label}",
                    f"{hist_prefix}该账号暂无历史与实时采样，前序账号异常已隔离，发出首步后自动定级",
                    "",
                )
            return ("cyan", "WAIT", f"待采样{hist_head_tag} · 暂无会话记录", f"{hist_prefix}{scope_win_txt}暂无可用采样记录", "")

        if t2_recovered_info:
            return ("green", "T0", f"T0 健康{hist_head_tag} · {active_model}", f"{hist_prefix}{t2_recovered_info}", t2_recovered_info)

        hist_503 = today_log["503"]
        if hist_503 > 0 and eff_503 == 0:
            sub = f"{hist_prefix}{scope_win_txt}无新异常（该号今日历史 {hist_503} 条 503 已衰减）"
        else:
            sub = f"{hist_prefix}{scope_win_txt}缓存与底层日志均健康"
        return ("green", "T0", f"T0 健康{hist_head_tag} · {active_model}", sub, "")

    def _collect_new_alerts(
        self,
        grade: str,
        notable_events: List[LogEvent],
        recent_active: List[ConversationSummary],
        max_consec_miss: int,
        today_steps: int,
        steps_scope_label: str = "全机",
        is_online: bool = True,
        severe_frequent_miss_conv: Optional[ConversationSummary] = None,
        slot_inventory: Optional[SlotInventorySnapshot] = None,
        slots_only: bool = False,
    ) -> List[Tuple[str, str, str]]:
        candidates: List[Tuple[str, str, str]] = []
        if slot_inventory is not None and slot_inventory.summary_db_ok:
            if slot_inventory.main_at_risk:
                candidates.append(
                    (
                        f"slot:main_risk:{slot_inventory.total_db // 5}:{slot_inventory.main_count}",
                        "Antigravity 500 槽位告急（主对话风险）",
                        slot_inventory.risk_headline
                        or f"本地会话已达 {slot_inventory.total_db}/{slot_inventory.cap}，已撞 30 分钟保护下限，请先备份主对话再重启！",
                    )
                )
            elif slot_inventory.status == "danger":
                candidates.append(
                    (
                        f"slot:danger:{slot_inventory.total_db // 10}",
                        "Antigravity 本机槽位逼近 500 上限",
                        f"当前 {slot_inventory.total_db}/{slot_inventory.cap}（主 {slot_inventory.main_count} / 可清子代理 {slot_inventory.cleanable_sub_count}），建议及时清理让位",
                    )
                )

        if not slots_only and grade != "IDLE" and (is_online or recent_active):
            if grade == "T3":
                for ev in notable_events:
                    if ev.category == "403":
                        candidates.append((ev.event_key, "Antigravity 封禁告警 (T3)", ev.summary))
            elif grade == "T2":
                for ev in notable_events:
                    if ev.category in ("503", "timeout"):
                        candidates.append((ev.event_key, "Antigravity 限流/超时告警 (T2)", ev.summary))
            elif grade == "T1":
                # Deduplicate per contiguous cr=0 streak start or severe 10-step window start
                offending = next((c for c in recent_active if c.consec_miss >= 2), None)
                if offending is not None:
                    key = f"t1:{offending.conversation_id}:{offending.consec_miss_start_step}"
                    candidates.append(
                        (key, "Antigravity 疑似 T1 掉缓存", f"{offending.short_id} ({offending.model}) 已连续 {max_consec_miss} 步 cr=0")
                    )
                elif severe_frequent_miss_conv is not None:
                    c = severe_frequent_miss_conv
                    pct = int(round((c.recent10_hit_ratio or 0.0) * 100))
                    key = f"t1freq:{c.conversation_id}:{c.first_abnormal_step_in_recent10}"
                    candidates.append(
                        (key, "Antigravity 频繁掉缓存 (T1)", f"{c.short_id} 近 10 步归零 {c.abnormal_in_recent10}/{c.cacheable_in_recent10} 步（有效命中仅 {pct}%）")
                    )
            elif today_steps >= self.config["daily_steps_alert"]:
                day = datetime.date.today().isoformat()
                candidates.append(
                    (
                        f"steps:{day}:{steps_scope_label}:{self.config['daily_steps_alert']}",
                        f"Antigravity 今日步数红线 ({steps_scope_label})",
                        f"{steps_scope_label} 今日已达 {today_steps} 步，注意控频护号",
                    )
                )

        if not self._baseline_ready:
            immediate_critical: List[Tuple[str, str, str]] = []
            for item in candidates:
                self._emitted_alert_keys.append(item[0])
                # Never silence a main-conversation eviction risk alert on first poll!
                if item[0].startswith("slot:main_risk:"):
                    immediate_critical.append(item)
            self._baseline_ready = True
            return immediate_critical

        fresh: List[Tuple[str, str, str]] = []
        for item in candidates:
            if item[0] not in self._emitted_alert_keys:
                self._emitted_alert_keys.append(item[0])
                fresh.append(item)
        return fresh

    def _iter_recent_transcripts(
        self,
        lookback_cutoff: float,
        today_start: float,
        max_older: int = 16,
    ) -> List[Tuple[str, Path]]:
        if not self.brain_dir.is_dir():
            return []
        today_items: List[Tuple[float, str, str]] = []
        older_items: List[Tuple[float, str, str]] = []
        join = os.path.join
        stat = os.stat
        try:
            with os.scandir(self.brain_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    tp_str = join(entry.path, ".system_generated", "logs", "transcript.jsonl")
                    try:
                        st = stat(tp_str)
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_mtime < lookback_cutoff:
                        continue
                    if st.st_mtime >= today_start:
                        today_items.append((st.st_mtime, entry.name, tp_str))
                    else:
                        older_items.append((st.st_mtime, entry.name, tp_str))
        except OSError:
            return []

        today_items.sort(key=lambda item: item[0], reverse=True)
        older_items.sort(key=lambda item: item[0], reverse=True)
        merged = today_items + older_items[:max(16, int(max_older))]
        return [(cid, Path(tp_str)) for _, cid, tp_str in merged]

    def _read_transcript_cached(self, conv_id: str, tp: Path) -> Tuple[List[Dict[str, Any]], bool, bool]:
        try:
            st = tp.stat()
        except OSError:
            self._transcript_cache.pop(conv_id, None)
            self._transcript_error_ts.pop(conv_id, None)
            return [], False, False

        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._transcript_cache.get(conv_id)
        if cached is not None and cached[0] == stamp:
            self._transcript_cache.move_to_end(conv_id)
            if conv_id in self._transcript_error_ts:
                self._transcript_error_ts.move_to_end(conv_id)
            return cached[1], cached[2], cached[3]

        parsed: List[Dict[str, Any]] = []
        error_ts_list: List[Tuple[float, str]] = []
        has_user_explicit = False
        has_subagent_marker = False
        pending_checkpoint = False
        pending_revert = False
        seen_planner_idx: Optional[int] = None
        max_step_ts: float = 0.0
        try:
            with tp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not has_user_explicit and '"USER_EXPLICIT"' in line:
                        has_user_explicit = True
                    if not has_subagent_marker and (
                        '"SUBAGENT"' in line or '"SWARM"' in line or "[swarm]" in line or '"source":"AGENT"' in line
                    ):
                        has_subagent_marker = True
                    if '"ERROR_MESSAGE"' in line:
                        try:
                            err_obj = json.loads(line)
                            if err_obj.get("type") == "ERROR_MESSAGE":
                                ets = parse_iso_timestamp(str(err_obj.get("created_at", "") or ""))
                                if ets is not None:
                                    ec = str(err_obj.get("content", "") or "")[:160].lower()
                                    error_ts_list.append((ets, ec))
                                continue
                        except json.JSONDecodeError:
                            pass
                    if '"CHECKPOINT"' in line:
                        try:
                            chk_obj = json.loads(line)
                            if chk_obj.get("type") == "CHECKPOINT":
                                pending_checkpoint = True
                        except json.JSONDecodeError:
                            pass
                    if '"USER_INPUT"' in line and '"PLANNER_RESPONSE"' not in line:
                        try:
                            u_obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if u_obj.get("type") == "USER_INPUT":
                            u_si = u_obj.get("step_index")
                            u_ts = parse_iso_timestamp(str(u_obj.get("created_at", "") or ""))
                            # Only treat regressed USER_INPUT as a real Undo/revert after at least one PLANNER_RESPONSE
                            # has already completed (avoids false positives on first-turn 503 resend duplicates).
                            if (
                                isinstance(u_si, int)
                                and seen_planner_idx is not None
                                and u_ts is not None
                                and u_si <= seen_planner_idx
                                and (max_step_ts <= 0.0 or u_ts >= max_step_ts - 5.0)
                            ):
                                pending_revert = True
                                seen_planner_idx = u_si
                                if u_ts > max_step_ts:
                                    max_step_ts = u_ts
                        continue
                    if '"PLANNER_RESPONSE"' not in line or '"input_tokens"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "PLANNER_RESPONSE" or "input_tokens" not in obj:
                        continue
                    ts = parse_iso_timestamp(str(obj.get("created_at", "") or "")) or st.st_mtime
                    raw_si = obj.get("step_index")
                    is_rev = pending_revert
                    if isinstance(raw_si, int):
                        if is_rev:
                            seen_planner_idx = raw_si
                            if ts > max_step_ts:
                                max_step_ts = ts
                        elif (
                            seen_planner_idx is not None
                            and raw_si <= seen_planner_idx
                            and (max_step_ts <= 0.0 or ts >= max_step_ts - 5.0)
                        ):
                            is_rev = True
                            seen_planner_idx = raw_si
                            if ts > max_step_ts:
                                max_step_ts = ts
                        elif seen_planner_idx is None or (raw_si > seen_planner_idx and ts >= max_step_ts - 60.0):
                            seen_planner_idx = raw_si
                            if ts > max_step_ts:
                                max_step_ts = ts
                    elif ts > max_step_ts:
                        max_step_ts = ts
                    parsed.append(
                        {
                            "step_index": raw_si if raw_si is not None else len(parsed),
                            "timestamp": ts,
                            "model": obj.get("model", ""),
                            "input_tokens": obj.get("input_tokens", 0),
                            "cache_read_tokens": obj.get("cache_read_tokens", 0),
                            "output_tokens": obj.get("output_tokens", 0),
                            "after_checkpoint": pending_checkpoint,
                            "after_revert": is_rev,
                        }
                    )
                    pending_checkpoint = False
                    pending_revert = False
        except OSError:
            return (cached[1], cached[2], cached[3]) if cached is not None else ([], False, False)

        self._transcript_cache[conv_id] = (stamp, parsed, has_user_explicit, has_subagent_marker)
        self._transcript_cache.move_to_end(conv_id)
        while len(self._transcript_cache) > 384:
            evicted_cid, _ = self._transcript_cache.popitem(last=False)
            self._transcript_error_ts.pop(evicted_cid, None)
        if error_ts_list:
            self._transcript_error_ts[conv_id] = error_ts_list[-16:]
            self._transcript_error_ts.move_to_end(conv_id)
            while len(self._transcript_error_ts) > 384:
                self._transcript_error_ts.popitem(last=False)
        else:
            self._transcript_error_ts.pop(conv_id, None)
        return parsed, has_user_explicit, has_subagent_marker

    def _resolve_conversation_transcript(self, query: str) -> Optional[Tuple[str, Path]]:
        clean = (query or "").strip()
        if not clean or not self.brain_dir.is_dir():
            return None
        exact_tp = self.brain_dir / clean / ".system_generated" / "logs" / "transcript.jsonl"
        if exact_tp.is_file():
            return clean, exact_tp
        low = clean.lower()
        matches: List[Tuple[float, str, Path]] = []
        try:
            with os.scandir(self.brain_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name.lower().startswith(low):
                        tp = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
                        try:
                            st = tp.stat()
                        except OSError:
                            continue
                        if st.st_size > 0:
                            matches.append((st.st_mtime, entry.name, tp))
        except OSError:
            return None
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1], matches[0][2]

    def read_conversation_detail(
        self,
        conversation_id: str,
        now_ts: Optional[float] = None,
        view_account: str = "",
        history_range: str = "30m",
    ) -> Optional[ConversationDetailReport]:
        """On-demand full-history report (context growth, TTFT, TPS, cache) for one conversation."""
        with self._lock:
            resolved = self._resolve_conversation_transcript(conversation_id)
            if resolved is None:
                return None
            conv_id, tp = resolved
            now = now_ts if now_ts is not None else time.time()
            raw_steps, has_user_explicit, has_subagent_marker = self._read_transcript_cached(conv_id, tp)
            if not raw_steps:
                return None

            today_start = datetime.datetime.fromtimestamp(now).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            first_ts = float(raw_steps[0].get("timestamp", 0.0) or 0.0)
            tl_since = min(today_start - 3600.0, first_ts or (now - 7 * 86400.0))
            switch_timestamps = self._ledger.get_switch_timestamps(tl_since)

            all_steps = self._classify_steps_cached(conv_id, raw_steps, switch_timestamps)
            if not all_steps:
                return None

            gens = self._db_reader.read_full_history(conv_id)
            is_subagent = (not has_user_explicit) and (has_subagent_marker or self._check_subagent_hint_cached(conv_id))
            aligned_gens = _align_steps_with_gen_history(all_steps, gens)

            raw_view = (view_account or "").strip()
            hr_key = (history_range or "30m").strip().lower()
            if hr_key not in HISTORY_RANGE_SPECS:
                hr_key = "30m"
            hr_short, _hr_label = HISTORY_RANGE_SPECS[hr_key]
            use_acct_filter = bool(raw_view) and raw_view != "__all__"
            use_range_filter = hr_key != "30m"

            scope_label = ""
            if use_acct_filter or use_range_filter:
                acct_filter_fn: Optional[Callable[[float], bool]] = None
                norm_view = ""
                if use_acct_filter:
                    norm_view = _mask_short(raw_view) or raw_view
                    timeline = self._ledger.get_timeline(min(today_start, first_ts or (now - 7 * 86400.0)))
                    fb_lbl = self._account_cache.short_label if self._account_cache.online else ""
                    acct_filter_fn = self._ledger.build_account_matcher(
                        norm_view,
                        timeline,
                        fallback_label=fb_lbl,
                    )

                if hr_key == "7d":
                    range_start_ts = now - 7 * 86400.0
                elif hr_key == "today":
                    range_start_ts = today_start
                elif hr_key == "6h":
                    range_start_ts = now - 6 * 3600.0
                else:
                    range_start_ts = 0.0

                filtered_pairs = [
                    (st, gen)
                    for st, gen in zip(all_steps, aligned_gens)
                    if (acct_filter_fn is None or acct_filter_fn(st.timestamp))
                    and (range_start_ts <= 0.0 or st.timestamp >= range_start_ts)
                ]
                if filtered_pairs:
                    steps = [p[0] for p in filtered_pairs]
                    sel_gens = [p[1] for p in filtered_pairs]
                    parts = []
                    if use_acct_filter:
                        parts.append(norm_view)
                    if use_range_filter:
                        parts.append(hr_short)
                    scope_label = " · ".join(parts)
                else:
                    steps = all_steps
                    sel_gens = aligned_gens
                    scope_label = "完整对话"
            else:
                steps = all_steps
                sel_gens = aligned_gens

            points: List[ConversationDetailPoint] = []
            max_ctx_limit = 0
            for st, gen in zip(steps, sel_gens):
                if gen is not None and gen.max_context_tokens > max_ctx_limit:
                    max_ctx_limit = gen.max_context_tokens
                points.append(
                    ConversationDetailPoint(
                        step_index=st.step_index,
                        timestamp=st.timestamp,
                        model=format_model_name(st.model),
                        total_tokens=st.total_tokens,
                        context_delta=st.context_delta,
                        input_tokens=st.input_tokens,
                        cache_read_tokens=st.cache_read_tokens,
                        output_tokens=st.output_tokens,
                        hit_rate=st.hit_rate,
                        classification=st.classification,
                        reason=st.reason,
                        ttft_sec=gen.ttft_sec if gen is not None else None,
                        tps=gen.tps if gen is not None else None,
                        streaming_sec=gen.streaming_sec if gen is not None else None,
                        is_revert=st.is_revert,
                        is_compact=st.is_compact,
                    )
                )

            latest = steps[-1]
            today_steps = sum(1 for s in steps if s.timestamp >= today_start)
            peak_ctx = max(s.total_tokens for s in steps)
            revert_count = sum(1 for s in steps if s.is_revert or s.reason == "revert_reset")
            compact_count = sum(
                1
                for s in steps
                if (not s.is_revert and s.reason != "revert_reset")
                and (s.is_compact or s.reason == "compact_reset")
            )
            hit_steps = sum(1 for s in steps if s.classification == "hit")
            abn_steps = sum(1 for s in steps if s.classification == "abnormal_miss")
            exp_cold_steps = sum(1 for s in steps if s.classification == "expected_cold")
            cacheable_total = hit_steps + abn_steps
            overall_ratio = (hit_steps / cacheable_total) if cacheable_total > 0 else None

            ttfts = sorted(p.ttft_sec for p in points if p.ttft_sec is not None)
            avg_ttft = (sum(ttfts) / len(ttfts)) if ttfts else None
            max_ttft = ttfts[-1] if ttfts else None
            p95_ttft = ttfts[min(len(ttfts) - 1, int(round(0.95 * (len(ttfts) - 1))))] if ttfts else None
            tpss = [p.tps for p in points if p.tps is not None]
            avg_tps = (sum(tpss) / len(tpss)) if tpss else None
            latest_tps = next((p.tps for p in reversed(points) if p.tps is not None), None)
            latest_ttft = next((p.ttft_sec for p in reversed(points) if p.ttft_sec is not None), None)

            consec_miss = 0
            for s in reversed(steps):
                if s.classification == "abnormal_miss":
                    consec_miss += 1
                else:
                    break

            fresh_rem: Optional[int] = None
            fresh_invalid = ""
            if not scope_label and latest.total_tokens >= self.config["large_context_threshold"] and latest.timestamp > 0:
                age = now - latest.timestamp
                if age <= self.config["freshness_window_sec"] + 300:
                    if latest.classification == "abnormal_miss":
                        fresh_invalid = f"无热缓存 (连掉 {consec_miss} 步)" if consec_miss >= 2 else "本步 cr=0 无热缓存"
                    else:
                        fresh_rem = int(self.config["freshness_window_sec"] - age)

            return ConversationDetailReport(
                conversation_id=conv_id,
                short_id=conv_id[:6],
                is_subagent=is_subagent,
                model=format_model_name(latest.model),
                model_raw=latest.model,
                total_steps=len(points),
                today_steps=today_steps,
                peak_context_tokens=peak_ctx,
                latest_context_tokens=latest.total_tokens,
                latest_context_delta=latest.context_delta,
                max_context_tokens=max_ctx_limit,
                compact_count=compact_count,
                cache_hit_steps=hit_steps,
                abnormal_miss_steps=abn_steps,
                expected_cold_steps=exp_cold_steps,
                overall_cacheable_hit_ratio=overall_ratio,
                avg_ttft_sec=avg_ttft,
                p95_ttft_sec=p95_ttft,
                max_ttft_sec=max_ttft,
                avg_tps=avg_tps,
                latest_tps=latest_tps,
                latest_ttft_sec=latest_ttft,
                freshness_remaining_sec=fresh_rem,
                freshness_invalid_reason=fresh_invalid,
                freshness_window_sec=self.config["freshness_window_sec"],
                points=points,
                title=self._resolve_conv_title(conv_id),
                revert_count=revert_count,
                scope_label=scope_label,
            )

    def _poll_hub_titles(self, port: int, token: str) -> None:
        """Fetch live conversation titles from the active local Hub (strictly read-only)."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": token,
                    "Connect-Protocol-Version": "1",
                },
            )
            with urllib.request.urlopen(req, timeout=1.2) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            summaries = data.get("trajectorySummaries") or {}
            if isinstance(summaries, dict):
                for cid, entry in summaries.items():
                    if not isinstance(entry, dict):
                        continue
                    summary = str(entry.get("summary") or "").strip()
                    if summary:
                        self._conv_titles[str(cid)] = summary
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            pass

    def _load_vscdb_titles_cached(self) -> Dict[str, str]:
        """Read-only offline fallback for conversation titles from Antigravity state.vscdb."""
        if self.vscdb_path is None or not self.vscdb_path.is_file():
            return self._vscdb_titles_cache
        try:
            st = self.vscdb_path.stat()
        except OSError:
            return self._vscdb_titles_cache
        stamp = (st.st_mtime_ns, st.st_size)
        if self._vscdb_titles_stamp == stamp:
            return self._vscdb_titles_cache

        titles: Dict[str, str] = {}
        conn: Optional[sqlite3.Connection] = None
        try:
            resolved = self.vscdb_path.resolve().as_posix()
            encoded = urllib.parse.quote(resolved, safe="/:")
            uri = f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.3)
            for key in ("antigravityUnifiedStateSync.trajectorySummaries", "unifiedStateSync.trajectorySummaries"):
                row = conn.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)).fetchone()
                if not row or not row[0]:
                    continue
                val_str = row[0] if isinstance(row[0], str) else bytes(row[0]).decode("utf-8", errors="ignore")
                try:
                    raw = base64.b64decode(val_str)
                except Exception:
                    continue
                top = parse_pb_map(raw)
                for item in top.get(1) or []:
                    if not isinstance(item, bytes):
                        continue
                    em = parse_pb_map(item)
                    cid_b = em.get(1, [b""])[0]
                    inner_b = em.get(2, [b""])[0]
                    if not (isinstance(cid_b, bytes) and isinstance(inner_b, bytes)):
                        continue
                    cid = cid_b.decode("utf-8", errors="ignore").strip()
                    if not cid or cid in titles:
                        continue
                    im = parse_pb_map(inner_b)
                    b64_b = im.get(1, [b""])[0]
                    if not isinstance(b64_b, bytes):
                        continue
                    try:
                        sub = parse_pb_map(base64.b64decode(b64_b))
                    except Exception:
                        continue
                    t_b = sub.get(1, [b""])[0]
                    if isinstance(t_b, bytes):
                        title = t_b.decode("utf-8", errors="ignore").strip()
                        if title:
                            titles[cid] = title
        except sqlite3.Error:
            return self._vscdb_titles_cache
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

        self._vscdb_titles_stamp = stamp
        self._vscdb_titles_cache = titles
        return titles

    def _load_annotation_title_cached(self, conv_id: str) -> str:
        """Read-only per-conversation title from ~/.gemini/antigravity/annotations/<conv_id>.pbtxt."""
        if self.annotations_dir is None or not conv_id:
            return ""
        if "/" in conv_id or "\\" in conv_id or ".." in conv_id:
            return ""
        pbtxt = self.annotations_dir / f"{conv_id}.pbtxt"
        try:
            st = pbtxt.stat()
        except OSError:
            self._annotation_title_cache.pop(conv_id, None)
            return ""
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._annotation_title_cache.get(conv_id)
        if cached is not None and cached[0] == stamp:
            self._annotation_title_cache.move_to_end(conv_id)
            return cached[1]
        try:
            # Annotation files are tiny metadata files (<16 KB); bound read defensively
            with pbtxt.open("rb") as fh:
                raw = fh.read(65536)
        except OSError:
            return cached[1] if cached is not None else ""
        title = parse_pbtxt_title(raw)
        self._annotation_title_cache[conv_id] = (stamp, title)
        self._annotation_title_cache.move_to_end(conv_id)
        while len(self._annotation_title_cache) > 512:
            self._annotation_title_cache.popitem(last=False)
        return title

    def _resolve_conv_title(self, conv_id: str) -> str:
        if not conv_id:
            return ""
        # 1. Local annotations/<conv_id>.pbtxt (persists user renames & survives Hub restart / account switch)
        ann_title = self._load_annotation_title_cached(conv_id)
        if ann_title:
            return ann_title
        # 2. Live Hub GetAllCascadeTrajectories summary (covers auto-named conversations without pbtxt title)
        title = self._conv_titles.get(conv_id, "")
        if title:
            return title
        # 3. Offline state.vscdb double-base64 protobuf fallback
        vtitles = self._load_vscdb_titles_cached()
        return vtitles.get(conv_id, "")

    @staticmethod
    def _classify_model_pool(label: str, model_id: str = "") -> str:
        low = f"{label} {model_id}".lower()
        if any(k in low for k in ("claude", "fable", "opus", "sonnet", "gpt")):
            return "3p"
        return "gemini"

    @staticmethod
    def _sync_account_active_pool(info: AccountQuotaInfo, active_family: str) -> None:
        info.quota_family = active_family
        chosen = info.pools.get(active_family)
        if chosen is None and info.pools:
            chosen = next(iter(info.pools.values()))
        if chosen is not None:
            info.five_hour_pct = chosen.five_hour_pct
            info.weekly_pct = chosen.weekly_pct
            info.five_hour_reset_at = chosen.five_hour_reset_at
            info.weekly_reset_at = chosen.weekly_reset_at

    def _poll_account_and_quota(self, now: float, active_family: str = "gemini") -> AccountQuotaInfo:
        info = AccountQuotaInfo(observed_at=now, quota_family=active_family)
        self._active_hub_pid = 0
        live_email = ""
        if self.daemon_dir.is_dir():
            try:
                hub_files = sorted(
                    self.daemon_dir.glob("hub_*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                hub_files = []

            for hp in hub_files[:40]:
                try:
                    hub = json.loads(hp.read_text(encoding="utf-8"))
                    pid = int(hub.get("pid", 0) or 0)
                    port = int(hub.get("httpPort", 0) or 0)
                    token = str(hub.get("csrfToken", "") or "")
                    if not (pid and port and token and _is_windows_pid_alive(pid)):
                        continue
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
                        data=b"{}",
                        headers={
                            "Content-Type": "application/json",
                            "x-codeium-csrf-token": token,
                            "Connect-Protocol-Version": "1",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                    status = payload.get("userStatus") or {}
                    live_email = str(status.get("email", "") or "")
                    info.email_masked = mask_email(live_email)
                    info.short_label = short_email_label(live_email)
                    info.tier = str((status.get("userTier") or {}).get("id", "") or "")
                    plan_info = (status.get("planStatus") or {}).get("planInfo") or {}
                    plan_name = str(plan_info.get("planName", "") or "").strip()
                    teams_tier = str(plan_info.get("teamsTier", "") or "").strip()
                    if plan_name and teams_tier and teams_tier.lower() not in plan_name.lower():
                        info.plan_status = f"{plan_name} ({teams_tier})"
                    else:
                        info.plan_status = plan_name or teams_tier
                    info.online = True
                    self._active_hub_pid = pid

                    configs = (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
                    pools: Dict[str, QuotaPoolInfo] = {}
                    for cfg in configs:
                        if not isinstance(cfg, dict):
                            continue
                        lbl = str(cfg.get("label", "") or "")
                        mid = str((cfg.get("modelOrAlias") or {}).get("model", "") or "")
                        qinfo = cfg.get("quotaInfo") or {}
                        frac = qinfo.get("remainingFraction")
                        reset_raw = qinfo.get("resetTime")
                        if frac is None and not reset_raw:
                            continue
                        pool_name = self._classify_model_pool(lbl, mid)
                        pool = pools.setdefault(pool_name, QuotaPoolInfo(name=pool_name))
                        pool.model_count += 1
                        if frac is not None:
                            pct_val = round(float(frac) * 100.0, 2)
                            if pool.five_hour_pct is None or pct_val < pool.five_hour_pct:
                                pool.five_hour_pct = pct_val
                        norm_reset = _normalize_reset_time(reset_raw)
                        if norm_reset and not pool.five_hour_reset_at:
                            pool.five_hour_reset_at = norm_reset

                    info.pools = pools
                    self._sync_account_active_pool(info, active_family)
                    self._poll_hub_titles(port, token)
                    break
                except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                    continue

        # Only enrich from Cockpit when live Antigravity session is online and email matches strictly
        if info.online and live_email and self.cockpit_quota_db.is_file():
            self._enrich_weekly_from_cockpit(info, live_email, now)
            self._sync_account_active_pool(info, active_family)
        return info

    def _enrich_weekly_from_cockpit(self, info: AccountQuotaInfo, raw_email: str, now: float) -> None:
        cockpit_dir = self.cockpit_quota_db.parent
        bound_acc_id = ""

        # 1. Exact email match in Cockpit's read-only antigravity_accounts.sqlite
        acc_db = cockpit_dir / "antigravity_accounts.sqlite"
        if acc_db.is_file():
            try:
                resolved_adb = acc_db.resolve().as_posix()
                enc_adb = urllib.parse.quote(resolved_adb, safe="/:")
                uri_adb = f"file:///{enc_adb}?mode=ro" if not enc_adb.startswith("/") else f"file://{enc_adb}?mode=ro"
                aconn = sqlite3.connect(uri_adb, uri=True, timeout=0.3)
                try:
                    row = aconn.execute(
                        "SELECT account_id FROM summaries WHERE LOWER(email) = LOWER(?) LIMIT 1",
                        (raw_email,),
                    ).fetchone()
                    if row and row[0]:
                        bound_acc_id = str(row[0])
                finally:
                    aconn.close()
            except sqlite3.Error:
                pass

        # 2. Exact email match in Cockpit accounts.json
        accounts_json = cockpit_dir / "accounts.json"
        if not bound_acc_id and accounts_json.is_file():
            try:
                data = json.loads(accounts_json.read_text(encoding="utf-8"))
                for acc in data.get("accounts") or []:
                    if isinstance(acc, dict) and str(acc.get("email", "")).lower() == raw_email.lower():
                        bound_acc_id = str(acc.get("id") or acc.get("account_id") or "")
                        if bound_acc_id:
                            break
            except (OSError, json.JSONDecodeError):
                pass

        # Never fall back to unrelated bindAccountId when email doesn't match (prevents cross-account pollution)
        if not bound_acc_id:
            return

        stale_cutoff_sec = now - 7 * 86400

        try:
            resolved = self.cockpit_quota_db.resolve().as_posix()
            encoded = urllib.parse.quote(resolved, safe="/:")
            uri = f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.3)
            try:
                cur = conn.cursor()
                cols = {str(r[1]).lower() for r in cur.execute("PRAGMA table_info(quota_observations)").fetchall()}
                has_reset_col = "reset_at" in cols
                sel_sql = (
                    "SELECT remaining, observed_at, reset_at FROM quota_observations "
                    "WHERE account_id = ? AND metric = ? ORDER BY observed_at DESC LIMIT 1"
                    if has_reset_col
                    else "SELECT remaining, observed_at, NULL FROM quota_observations "
                    "WHERE account_id = ? AND metric = ? ORDER BY observed_at DESC LIMIT 1"
                )
                for pool_name in ("gemini", "3p"):
                    for kind in ("weekly", "5h"):
                        metric = f"{pool_name}-{kind}"
                        row = cur.execute(sel_sql, (bound_acc_id, metric)).fetchone()
                        if not row or row[0] is None:
                            continue
                        obs_raw = float(row[1] or 0)
                        obs_sec = obs_raw / 1000.0 if obs_raw > 1e11 else obs_raw
                        if obs_sec and obs_sec < stale_cutoff_sec:
                            continue
                        val = round(float(row[0]), 2)
                        reset_iso = _normalize_reset_time(row[2])
                        pool = info.pools.setdefault(pool_name, QuotaPoolInfo(name=pool_name))
                        if kind == "weekly":
                            pool.weekly_pct = val
                            if reset_iso:
                                pool.weekly_reset_at = reset_iso
                        else:
                            if pool.five_hour_pct is None:
                                pool.five_hour_pct = val
                            if reset_iso and not pool.five_hour_reset_at:
                                pool.five_hour_reset_at = reset_iso
            finally:
                conn.close()
        except sqlite3.Error:
            pass


def _align_steps_with_gen_history(
    steps: List[StepRecord],
    gens: List[Any],
    max_gap_scan: int = 16,
) -> List[Optional[Any]]:
    """Align transcript StepRecords with SQLite GenTelemetry rows, resilient to Undo/revert & account slices."""
    if not steps:
        return []
    if not gens:
        return [None] * len(steps)

    aligned: List[Optional[Any]] = [None] * len(steps)
    gi = len(gens) - 1
    gap_lim = max(16, int(max_gap_scan))
    for si in range(len(steps) - 1, -1, -1):
        if gi < 0:
            break
        st = steps[si]
        # Tight 0.2% tolerance prevents adjacent 200K vs 202K steps from colliding after Undo branch drops
        tol_in = max(16, int(st.total_tokens * 0.002))
        tol_cr = max(16, int(max(st.cache_read_tokens, st.total_tokens) * 0.005))
        matched_idx = -1
        scan_lo = max(-1, gi - gap_lim)
        # Pass 1: exact/tight match on total_in + cached_in + output_tokens
        for cand in range(gi, scan_lo, -1):
            g = gens[cand]
            out_ok = (st.output_tokens <= 0 or g.output_tokens <= 0) or abs(g.output_tokens - st.output_tokens) <= max(16, int(st.output_tokens * 0.08))
            if abs(g.total_in - st.total_tokens) <= tol_in and abs(g.cached_in - st.cache_read_tokens) <= tol_cr and out_ok:
                matched_idx = cand
                break
        # Pass 2: fallback for slight SQLite/transcript cached_in accounting drift (must still match total_in & output_tokens tightly)
        if matched_idx < 0:
            pass2_lo = max(-1, gi - min(gap_lim, 6))
            for cand in range(gi, pass2_lo, -1):
                g = gens[cand]
                out_ok = (st.output_tokens <= 0 or g.output_tokens <= 0) or abs(g.output_tokens - st.output_tokens) <= max(16, int(st.output_tokens * 0.05))
                if abs(g.total_in - st.total_tokens) <= tol_in and out_ok and abs(g.cached_in - st.cache_read_tokens) <= max(256, int(st.total_tokens * 0.05)):
                    matched_idx = cand
                    break
        if matched_idx >= 0:
            aligned[si] = gens[matched_idx]
            gi = matched_idx - 1
    return aligned


def _serialize_conversation_summary(c: ConversationSummary, include_steps: bool = False) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "conversation_id": c.conversation_id,
        "short_id": c.short_id,
        "title": c.title,
        "is_subagent": c.is_subagent,
        "model": c.model,
        "model_raw": c.model_raw,
        "context_tokens": c.context_tokens,
        "latest_context_delta": c.latest_context_delta,
        "max_context_tokens": c.max_context_tokens,
        "total_step_count": c.total_step_count,
        "latest_hit_rate": round(c.latest_hit_rate, 2),
        "latest_cached_in": c.latest_cached_in,
        "latest_uncached_in": c.latest_uncached_in,
        "latest_timestamp": round(c.latest_timestamp, 3),
        "latest_time_local": _epoch_to_iso_local(c.latest_timestamp) if c.latest_timestamp > 0 else None,
        "ttft_sec": round(c.ttft_sec, 2) if c.ttft_sec is not None else None,
        "ttft_avg_sec": round(c.ttft_avg_sec, 2) if c.ttft_avg_sec is not None else None,
        "ttft_max_sec": round(c.ttft_max_sec, 2) if c.ttft_max_sec is not None else None,
        "tps": round(c.tps, 2) if c.tps is not None else None,
        "speed_status": c.speed_status,
        "consec_miss": c.consec_miss,
        "abnormal_in_recent10": c.abnormal_in_recent10,
        "cacheable_in_recent10": c.cacheable_in_recent10,
        "recent10_hit_ratio": round(c.recent10_hit_ratio, 4) if c.recent10_hit_ratio is not None else None,
        "freshness_remaining_sec": c.freshness_remaining_sec,
        "freshness_invalid_reason": c.freshness_invalid_reason,
    }
    if include_steps:
        data["recent_steps"] = [
            {
                "step_index": s.step_index,
                "timestamp": round(s.timestamp, 3),
                "model": format_model_name(s.model),
                "total_tokens": s.total_tokens,
                "context_delta": s.context_delta,
                "input_tokens": s.input_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "output_tokens": s.output_tokens,
                "hit_rate": round(s.hit_rate, 2),
                "classification": s.classification,
                "reason": s.reason,
            }
            for s in c.recent_steps
        ]
    return data


def format_json_snapshot(snap: MonitorSnapshot) -> str:
    """Serialize MonitorSnapshot into a stable, email-masked JSON contract for CLI & RULES callers."""
    exit_code = compute_cli_exit_code(snap)
    main_convs = [c for c in snap.active_conversations if not c.is_subagent]
    sub_convs = [c for c in snap.active_conversations if c.is_subagent]

    pools_out: Dict[str, Dict[str, Any]] = {}
    for pname in ("gemini", "3p"):
        pool = snap.account.pools.get(pname)
        if pool is not None:
            pools_out[pname] = {
                "five_hour_pct": round(pool.five_hour_pct, 2) if pool.five_hour_pct is not None else None,
                "weekly_pct": round(pool.weekly_pct, 2) if pool.weekly_pct is not None else None,
                "five_hour_reset_at": pool.five_hour_reset_at,
                "weekly_reset_at": pool.weekly_reset_at,
                "model_count": pool.model_count,
            }

    alerts_out = [
        {"key": k, "title": t, "message": m}
        for k, t, m in snap.alerts
    ]
    if snap.slot_inventory is not None and snap.slot_inventory.main_at_risk:
        if not any(a["key"].startswith("slot:main_risk:") for a in alerts_out):
            alerts_out.append(
                {
                    "key": f"slot:main_risk:{snap.slot_inventory.total_db // 5}:{snap.slot_inventory.main_count}",
                    "title": "Antigravity 500 槽位告急（主对话风险）",
                    "message": snap.slot_inventory.risk_message,
                }
            )
    if not alerts_out and snap.pressure in ("warn", "red", "degraded"):
        alerts_out.append({"key": f"status:{snap.grade}", "title": snap.headline, "message": snap.sub_headline})

    payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "ok": True,
        "observed_at": _epoch_to_iso_utc(snap.generated_at),
        "observed_at_local": _epoch_to_iso_local(snap.generated_at),
        "observed_at_ts": round(snap.generated_at, 3),
        "grade": snap.grade,
        "level": snap.level,
        "pressure": snap.pressure,
        "exit_code": exit_code,
        "headline": snap.headline,
        "sub_headline": snap.sub_headline,
        "active_model": snap.active_model,
        "view": {
            "view_account": snap.view_account,
            "resolved_account": snap.resolved_account,
            "available_accounts": list(snap.available_accounts),
            "is_custom_account_view": snap.is_custom_account_view,
            "history_range": snap.history_range,
            "history_range_label": snap.history_range_label,
            "is_historical_rating": snap.is_historical_rating,
            "rating_anchor_time_local": _epoch_to_iso_local(snap.rating_anchor_ts) if snap.rating_anchor_ts > 0 else None,
            "range_steps": snap.range_steps,
        },
        "account": {
            "short_label": snap.account.short_label,
            "email_masked": snap.account.email_masked,
            "online": snap.account.online,
            "tier": snap.account.tier,
            "plan_status": snap.account.plan_status,
            "quota_family": snap.account.quota_family,
            "five_hour_pct": round(snap.account.five_hour_pct, 2) if snap.account.five_hour_pct is not None else None,
            "weekly_pct": round(snap.account.weekly_pct, 2) if snap.account.weekly_pct is not None else None,
            "five_hour_reset_at": snap.account.five_hour_reset_at,
            "weekly_reset_at": snap.account.weekly_reset_at,
            "pools": pools_out,
        },
        "steps": {
            "scope_label": snap.steps_scope_label,
            "today_steps": snap.today_steps,
            "account_today_steps": snap.account_today_steps,
            "total_today_steps": snap.total_today_steps,
            "main_today_steps": snap.main_today_steps,
            "subagent_today_steps": snap.subagent_today_steps,
            "total_main_today_steps": snap.total_main_today_steps,
            "total_subagent_today_steps": snap.total_subagent_today_steps,
            "daily_steps_warn": snap.daily_steps_warn,
            "daily_steps_alert": snap.daily_steps_alert,
            "remaining_to_warn": max(0, snap.daily_steps_warn - snap.today_steps),
            "remaining_to_alert": max(0, snap.daily_steps_alert - snap.today_steps),
            "usage_pct": round((snap.today_steps / max(1, snap.daily_steps_alert)) * 100.0, 1),
            "last_1h_steps": snap.last_1h_steps,
            "peak_hour_steps_today": snap.peak_hour_steps_today,
            "peak_hour_label": snap.peak_hour_label,
        },
        "cache": {
            "primary_hit_rate": round(snap.primary_hit_rate, 2) if snap.primary_hit_rate is not None else None,
            "primary_hit_classification": snap.primary_hit_classification,
            "consec_miss": snap.max_consec_miss,
            "max_abnormal_recent10": snap.max_abnormal_recent10,
            "worst_recent10_hit_ratio": round(snap.worst_recent10_hit_ratio, 4) if snap.worst_recent10_hit_ratio is not None else None,
            "wasted_cold_tokens_total": snap.wasted_cold_tokens_total,
            "min_freshness_sec": snap.min_freshness_sec,
            "min_freshness_conv": snap.min_freshness_conv,
            "min_freshness_ctx": snap.min_freshness_ctx,
            "min_freshness_invalid_reason": snap.min_freshness_invalid_reason,
            "freshness_window_sec": snap.freshness_window_sec,
        },
        "perf": {
            "tps": round(snap.primary_tps, 2) if snap.primary_tps is not None else None,
            "ttft_sec": round(snap.primary_ttft, 2) if snap.primary_ttft is not None else None,
            "ttft_avg_sec": round(snap.primary_ttft_avg, 2) if snap.primary_ttft_avg is not None else None,
            "ttft_max_sec": round(snap.primary_ttft_max, 2) if snap.primary_ttft_max is not None else None,
            "speed_status": snap.primary_speed_status,
        },
        "errors_30m": {
            "scope_label": snap.log_scope_label,
            "window_min": snap.sliding_window_min,
            "503": snap.recent_503,
            "timeout": snap.recent_timeout,
            "403": snap.recent_403,
            "retry": snap.recent_retry,
            "t2_recovery_note": snap.t2_recovery_note,
        },
        "errors_today": {
            "scope_label": snap.log_scope_label,
            "503": snap.today_503,
            "timeout": snap.today_timeout,
            "403": snap.today_403,
            "retry": snap.today_retry,
            "machine_503": snap.machine_today_503,
            "machine_timeout": snap.machine_today_timeout,
            "machine_403": snap.machine_today_403,
            "machine_retry": snap.machine_today_retry,
        },
        "models_recent": [
            {
                "model": m.model,
                "step_count": m.step_count,
                "hit_count": m.hit_count,
                "abnormal_miss_count": m.abnormal_miss_count,
                "ttft_avg_sec": round(m.ttft_avg_sec, 2) if m.ttft_avg_sec is not None else None,
                "ttft_max_sec": round(m.ttft_max_sec, 2) if m.ttft_max_sec is not None else None,
                "latest_ttft_sec": round(m.latest_ttft_sec, 2) if m.latest_ttft_sec is not None else None,
                "tps_avg": round(m.tps_avg, 2) if m.tps_avg is not None else None,
                "steps_after_last_t2": m.steps_after_last_t2,
                "ttft_after_last_t2_median": round(m.ttft_after_last_t2_median, 2) if m.ttft_after_last_t2_median is not None else None,
            }
            for m in snap.model_stats
        ],
        "slots": (
            {
                "total_db": snap.slot_inventory.total_db,
                "cap": snap.slot_inventory.cap,
                "remaining_slots": snap.slot_inventory.remaining_slots,
                "headroom": snap.slot_inventory.remaining_slots,
                "usage_pct": snap.slot_inventory.usage_pct,
                "main_count": snap.slot_inventory.main_count,
                "cleanable_sub_count": snap.slot_inventory.cleanable_sub_count,
                "cleanable_30m_count": snap.slot_inventory.cleanable_30m_count,
                "protected_sub_count": snap.slot_inventory.protected_sub_count,
                "unknown_count": snap.slot_inventory.unknown_count,
                "pinned_floor_count": snap.slot_inventory.pinned_floor_count,
                "total_bytes": snap.slot_inventory.total_bytes,
                "cleanable_bytes": snap.slot_inventory.cleanable_bytes,
                "protect_age_min": snap.slot_inventory.protect_age_min,
                "status": snap.slot_inventory.status,
                "main_at_risk": snap.slot_inventory.main_at_risk,
                "risk_message": snap.slot_inventory.risk_message,
                "risk_headline": snap.slot_inventory.risk_message,
                "summary_db_ok": snap.slot_inventory.summary_db_ok,
                "last_auto_clean_summary": snap.slot_inventory.last_auto_clean_summary,
            }
            if snap.slot_inventory is not None
            else None
        ),
        "alerts": alerts_out,
        "counts": {
            "active_main": len(main_convs),
            "active_subagent": len(sub_convs),
            "active_total": len(snap.active_conversations),
        },
        "active_conversations": [
            _serialize_conversation_summary(c, include_steps=False)
            for c in snap.active_conversations
        ],
    }

    if snap.target_conversation_query:
        payload["target_conversation_query"] = snap.target_conversation_query
        payload["target_conversation_found"] = snap.target_conversation is not None
        payload["target_conversation"] = (
            _serialize_conversation_summary(snap.target_conversation, include_steps=True)
            if snap.target_conversation is not None
            else None
        )
    else:
        payload["target_conversation"] = None

    return mask_email(json.dumps(payload, ensure_ascii=False, indent=2))


def format_detail_text_report(report: ConversationDetailReport) -> str:
    """Build a concise masked long-range diagnostic report for one conversation."""
    role = "子代理" if report.is_subagent else "主对话"
    hit_ratio = (
        f"{report.overall_cacheable_hit_ratio * 100:.1f}%"
        if report.overall_cacheable_hit_ratio is not None
        else "—"
    )
    avg_t = f"{report.avg_ttft_sec:.1f}s" if report.avg_ttft_sec is not None else "—"
    p95_t = f"{report.p95_ttft_sec:.1f}s" if report.p95_ttft_sec is not None else "—"
    max_t = f"{report.max_ttft_sec:.1f}s" if report.max_ttft_sec is not None else "—"
    avg_p = f"{report.avg_tps:.0f} t/s" if report.avg_tps is not None else "—"
    distinct_models = list(dict.fromkeys(pt.model for pt in report.points if pt.model))
    model_overview = f"{report.model} (含 {len(distinct_models)} 种模型: {', '.join(distinct_models[:4])})" if len(distinct_models) >= 2 else report.model
    title_part = f"{report.title} ({report.short_id})" if report.title else report.short_id
    scope_part = f" · 视角 {report.scope_label}" if getattr(report, "scope_label", "") else ""
    rev_s = f" | 回溯 {report.revert_count} 次" if report.revert_count > 0 else ""
    lines = [
        f"Antigravity 单对话长线体检 ({title_part} · {role} · {model_overview}{scope_part})",
        f"步数概况: 总计 {report.total_steps} 步 (今日 {report.today_steps} 步) | 压缩 {report.compact_count} 次{rev_s}",
        f"请求体规模: 当前 {format_tokens(report.latest_context_tokens)} ({format_delta_tokens(report.latest_context_delta)}) | 峰值 {format_tokens(report.peak_context_tokens)}",
        f"缓存统计: 有效命中率 {hit_ratio} (命中 {report.cache_hit_steps} / 异常归零 {report.abnormal_miss_steps} / 正常冷启 {report.expected_cold_steps})",
        f"速度统计: TTFT 均 {avg_t} / P95 {p95_t} / 峰 {max_t} | 平均吞吐 {avg_p}",
        "最近 12 步明细（新 → 旧）:",
    ]
    for pt in reversed(report.points[-12:]):
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%m-%d %H:%M:%S") if pt.timestamp > 0 else "-- --:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f}t/s" if pt.tps is not None else "—"
        if pt.is_revert and pt.reason == "cached":
            r_tag = "回溯·命中"
        elif getattr(pt, "is_compact", False) and pt.reason == "cached":
            r_tag = "压缩·命中"
        else:
            r_tag = pt.reason
        lines.append(
            f"  #{pt.step_index:<4} {hm} │ [{pt.model}] │ {format_tokens(pt.total_tokens):>6} ({format_delta_tokens(pt.context_delta):>7}) "
            f"│ 缓存 {pt.hit_rate:5.1f}% │ 首字 {t_s:>6} │ {p_s:>6} [{r_tag}]"
        )
    return mask_email("\n".join(lines))


def format_text_summary(snap: MonitorSnapshot) -> str:
    """Build a clean, email-masked plain-text health summary for sharing or CLI output."""
    ts_str = datetime.datetime.fromtimestamp(snap.generated_at).strftime("%Y-%m-%d %H:%M:%S")
    hit_str = f"{snap.primary_hit_rate:.1f}%" if snap.primary_hit_rate is not None else "—"
    tps_str = f"{snap.primary_tps:.1f} tok/s" if snap.primary_tps is not None else "—"
    ttft_str = f"{snap.primary_ttft:.1f}s" if snap.primary_ttft is not None else "—"
    if snap.primary_ttft_avg is not None and snap.primary_ttft_max is not None:
        ttft_str += f" (近10步均 {snap.primary_ttft_avg:.1f}s / 峰 {snap.primary_ttft_max:.1f}s)"
    acc_5h = f"{snap.account.five_hour_pct:.1f}%" if snap.account.five_hour_pct is not None else "—"
    acc_wk = f"{snap.account.weekly_pct:.0f}%" if snap.account.weekly_pct is not None else "—"
    steps_str = f"{snap.steps_scope_label} {snap.today_steps}/{snap.daily_steps_alert}"
    steps_str += f" (主 {snap.main_today_steps} / 子 {snap.subagent_today_steps} | 近1h {snap.last_1h_steps}"
    if snap.peak_hour_steps_today > 0 and snap.peak_hour_label:
        steps_str += f" | 峰值 {snap.peak_hour_steps_today}@{snap.peak_hour_label}"
    steps_str += ")"
    if snap.total_today_steps > snap.today_steps:
        steps_str += f" · 全机 {snap.total_today_steps}"

    lines = [
        f"Antigravity 实时体检摘要 ({ts_str})",
        f"健康评级: [{snap.grade}] {snap.headline} | 压力: {snap.pressure}",
        f"状态说明: {snap.sub_headline}",
    ]
    if snap.is_custom_account_view:
        online_s = snap.account.short_label if snap.account.online else "离线"
        lines.append(f"观察视角: {snap.resolved_account or '全机'} ({snap.history_range_label}) | 当前在线: {online_s}")
    else:
        range_suffix = f" | 范围 {snap.history_range_label}" if snap.history_range != "30m" else ""
        lines.append(f"当前账号: {snap.account.short_label} | 5h 额度 {acc_5h} | 周额度 {acc_wk}{range_suffix}")
        if snap.account.pools:
            pool_parts = []
            for pname in ("gemini", "3p"):
                pool = snap.account.pools.get(pname)
                if pool is None:
                    continue
                p5 = f"{pool.five_hour_pct:.1f}%" if pool.five_hour_pct is not None else "—"
                pw = f"{pool.weekly_pct:.0f}%" if pool.weekly_pct is not None else "—"
                rst = f" 重置 {pool.five_hour_reset_at}" if pool.five_hour_reset_at else ""
                pool_parts.append(f"{pname}: 5h {p5} / 周 {pw}{rst}")
            if pool_parts:
                lines.append("分池配额: " + " | ".join(pool_parts))

    lines.extend(
        [
            f"实时性能: 缓存 {hit_str} | 吞吐 {tps_str} | TTFT {ttft_str}",
            f"步数统计: {steps_str}",
            f"底层日志 ({snap.log_scope_label} · {snap.history_range_label} / 今日): 503={snap.recent_503}/{snap.today_503} | 流超时={snap.recent_timeout}/{snap.today_timeout} | 403={snap.recent_403}/{snap.today_403} | 代理重试={snap.recent_retry}/{snap.today_retry}",
        ]
    )
    if snap.slot_inventory is not None:
        si = snap.slot_inventory
        slot_line = (
            f"本地槽位: {si.total_db}/{si.cap} ({si.usage_pct:.0f}% · 余 {si.headroom}) | "
            f"主对话 {si.main_count} / 可清子代理 {si.cleanable_sub_count} ({format_bytes_human(si.cleanable_bytes)}) / 保护中 {si.protected_sub_count}"
        )
        if si.unknown_count > 0:
            slot_line += f" / 未知 {si.unknown_count}"
        lines.append(slot_line)
        if si.main_at_risk and si.risk_headline:
            lines.append(f"槽位警报: {si.risk_headline}")
    if len(snap.model_stats) >= 2:
        ms_parts = []
        for ms in snap.model_stats[:4]:
            ta = f"均{ms.ttft_avg_sec:.1f}s/峰{ms.ttft_max_sec:.1f}s" if (ms.ttft_avg_sec is not None and ms.ttft_max_sec is not None) else "—"
            ms_parts.append(f"{ms.model}({ms.step_count}步 TTFT {ta})")
        lines.append(f"{snap.history_range_label}分模型 TTFT: " + " | ".join(ms_parts))
    if snap.active_conversations:
        main_list = [c for c in snap.active_conversations if not c.is_subagent]
        sub_list = [c for c in snap.active_conversations if c.is_subagent]
        lines.append(f"活跃对话 (主对话 {len(main_list)} / 子代理 {len(sub_list)}，展示近 10 步):")
        ordered = main_list[:6] + sub_list[:4]
        for c in ordered:
            tag = "[子]" if c.is_subagent else "[主]"
            bar = "".join(
                "█" if s.classification == "hit"
                else ("◇" if s.reason == "compact_reset" else ("▒" if s.classification == "expected_cold" else "×"))
                for s in c.recent_steps
            )
            delta_s = f"({format_delta_tokens(c.latest_context_delta)})" if c.latest_context_delta != 0 else ""
            sp = f" {c.tps:.0f}t/s" if c.tps is not None else ""
            tf = f" TTFT {c.ttft_sec:.1f}s" if c.ttft_sec is not None else ""
            if c.ttft_avg_sec is not None and c.ttft_max_sec is not None and len([x for x in c.recent_ttfts if x is not None]) >= 2:
                tf += f"(均{c.ttft_avg_sec:.0f}/峰{c.ttft_max_sec:.0f}s)"
            title_s = f" {truncate_display_width(c.title, 20)}" if c.title else ""
            lines.append(
                f"  - {tag} {c.short_id}{title_s} {c.model} {format_tokens(c.context_tokens)}{delta_s} [{bar}] {c.latest_hit_rate:.1f}%{tf}{sp}"
            )
    if snap.target_conversation_query:
        tc = snap.target_conversation
        if tc is not None:
            fresh_s = tc.freshness_invalid_reason or format_countdown(tc.freshness_remaining_sec)
            lines.append(
                f"定向对话 ({tc.short_id}): {tc.model} | 上下文 {format_tokens(tc.context_tokens)} ({format_delta_tokens(tc.latest_context_delta)}) "
                f"| 缓存 {tc.latest_hit_rate:.1f}% | 保鲜 {fresh_s}"
            )
        else:
            lines.append(f"定向对话 ({snap.target_conversation_query}): 未找到匹配记录")
    if snap.wasted_cold_tokens_total > 0 and (snap.max_consec_miss >= 1 or snap.max_abnormal_recent10 >= 2):
        lines.append(f"冷算提醒: 近 10 步异常归零累计多耗约 {format_tokens(snap.wasted_cold_tokens_total)} Token")
    return mask_email("\n".join(lines))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    engine = AntigravityMonitorEngine(read_only=True)
    snap = engine.poll(force_account=True)
    print(format_text_summary(snap))


if __name__ == "__main__":
    main()
