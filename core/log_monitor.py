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
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from core.account_ledger import AccountSwitchLedger
from core.config_store import DEFAULT_CONFIG, sanitize_config
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
    r"(?:stream idle timeout|timed out after 5m)",
    re.IGNORECASE,
)
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
    reason: str          # "cached" | "first_turn" | "idle_rewarm" | "model_switch" | "compact_reset" | "small_ctx" | "first_cacheable" | "abnormal_zero"
    context_delta: int = 0


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

        after_checkpoint = bool(item.get("after_checkpoint", False))
        is_real_compact = prev_total is not None and prev_total >= min_cacheable_tokens and (
            (after_checkpoint and total < prev_total * 0.85) or (total < prev_total * 0.75)
        )
        if cr > 0:
            cls, reason = "hit", "cached"
        elif prev_ts is None:
            cls, reason = "expected_cold", "first_turn"
        elif crossed_switch:
            cls, reason = "expected_cold", "account_switch"
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
            )
        )
        prev_ts = ts
        prev_model = model
        prev_total = total

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
        if TOS_403_RE.search(body):
            category = "403"
            label = "403 TOS/权限拒绝"
        elif STREAM_TIMEOUT_RE.search(body):
            category = "timeout"
            label = "5m 流超时"
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
    ) -> Tuple[Dict[str, int], Dict[str, int], List[LogEvent]]:
        cutoff = max(now_ts - max(1, window_min) * 60, account_since_ts)
        today_start = datetime.datetime.fromtimestamp(now_ts).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        recent = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        today = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        in_window_notable: List[LogEvent] = []

        for ev in self._events:
            if ev.timestamp >= today_start:
                if account_filter_fn is None or account_filter_fn(ev.timestamp):
                    today[ev.category] = today.get(ev.category, 0) + 1
            if ev.timestamp >= cutoff:
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
    ) -> float:
        cutoff = max(now_ts - max(1, window_min) * 60, account_since_ts)
        latest = 0.0
        for ev in self._events:
            if ev.timestamp >= cutoff and ev.category in categories and ev.timestamp > latest:
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
        self._transcript_cache: Dict[str, Tuple[Tuple[int, int], List[Dict[str, Any]], bool, bool]] = {}
        self._subagent_hint_cache: Dict[str, Tuple[int, bool]] = {}
        self._conv_titles: Dict[str, str] = {}
        self._annotation_title_cache: Dict[str, Tuple[Tuple[int, int], str]] = {}
        self._vscdb_titles_stamp: Optional[Tuple[int, int]] = None
        self._vscdb_titles_cache: Dict[str, str] = {}
        self._db_reader = ConversationDbReader(self.conversations_dir, max_cache_entries=36, tail_rows=16)
        self._log_tailer = LanguageServerLogTailer(self.ls_log_path, max_events=200)
        self._ledger = AccountSwitchLedger(
            ledger_path=ledger_path,
            cockpit_logs_dir=cockpit_logs_dir,
            read_only=self.read_only,
        )
        self._account_cache: AccountQuotaInfo = AccountQuotaInfo()
        self._active_hub_pid: int = 0
        self._last_account_poll: float = 0.0
        self._last_quota_family: str = "gemini"
        self._baseline_ready: bool = False
        self._emitted_alert_keys: Deque[str] = deque(maxlen=256)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = sanitize_config(config)

    def _check_subagent_hint_cached(self, conv_id: str) -> bool:
        db_path = self.conversations_dir / f"{conv_id}.db"
        try:
            mtime_ns = db_path.stat().st_mtime_ns
        except OSError:
            self._subagent_hint_cache.pop(conv_id, None)
            return False
        cached = self._subagent_hint_cache.get(conv_id)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        hint = self._db_reader.check_subagent_hint(conv_id)
        self._subagent_hint_cache[conv_id] = (mtime_ns, hint)
        if len(self._subagent_hint_cache) > 512:
            self._subagent_hint_cache.pop(next(iter(self._subagent_hint_cache)), None)
        return hint

    def _build_conversation_summary(
        self,
        conv_id: str,
        raw_steps: List[Dict[str, Any]],
        has_user_explicit: bool,
        has_subagent_marker: bool,
        now: float,
        switch_timestamps: List[float],
        current_acct_since_ts: float,
    ) -> Optional[ConversationSummary]:
        steps = classify_steps(
            raw_steps,
            idle_rewarm_sec=self.config["idle_rewarm_sec"],
            account_switches=switch_timestamps,
        )
        if not steps:
            return None

        latest = steps[-1]
        recent10 = steps[-10:]
        eval_steps = (
            [s for s in steps if s.timestamp >= current_acct_since_ts - 2.0]
            if current_acct_since_ts > 0.0
            else steps
        )
        eval_recent10 = (
            [s for s in recent10 if s.timestamp >= current_acct_since_ts - 2.0]
            if current_acct_since_ts > 0.0
            else recent10
        )

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
            # Keep expired items visible for only 5 min past freshness window so stale sessions clear out cleanly
            if age <= self.config["freshness_window_sec"] + 300:
                if latest.classification == "abnormal_miss" and (
                    current_acct_since_ts <= 0.0 or latest.timestamp >= current_acct_since_ts - 2.0
                ):
                    fresh_invalid = f"无热缓存 (连掉 {consec_miss} 步)" if consec_miss >= 2 else "本步 cr=0 无热缓存"
                elif current_acct_since_ts <= 0.0 or latest.timestamp >= current_acct_since_ts - 2.0:
                    fresh_rem = int(self.config["freshness_window_sec"] - age)

        return ConversationSummary(
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
            total_step_count=len(steps),
            title=self._resolve_conv_title(conv_id),
        )

    def _enrich_conv_from_db(self, c: ConversationSummary) -> None:
        snap: Optional[ConversationDbSnapshot] = self._db_reader.read(c.conversation_id)
        if not snap:
            return
        if snap.is_subagent_hint and not self._transcript_cache.get(c.conversation_id, (None, [], False, False))[2]:
            c.is_subagent = True
        valid_gens = [g for g in snap.recent if g.total_in > 0 or g.output_tokens > 0][-10:]
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
            c.max_context_tokens = snap.max_context_tokens
            c.shard_tag = gen.shard_tag
            c.speed_status = self._classify_speed(gen.output_tokens, gen.tps, gen.ttft_sec, c.context_tokens)

    def poll(
        self,
        now_ts: Optional[float] = None,
        force_account: bool = False,
        target_conversation_id: str = "",
    ) -> MonitorSnapshot:
        now = now_ts if now_ts is not None else time.time()
        self._log_tailer.poll(now)

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
        lookback_cutoff = min(today_start - 3600, now - 24 * 3600)

        timeline = self._ledger.get_timeline(today_start)
        switch_timestamps = self._ledger.get_switch_timestamps(lookback_cutoff)
        cur_acct_label = self._account_cache.short_label if self._account_cache.online else ""
        # When switched between >=2 accounts today, evaluate T1 / wasted-cold only on steps after the latest switch
        # so a previous degraded account's red streak never penalizes a freshly switched healthy account.
        current_acct_since_ts = timeline[-1][0] if (cur_acct_label and len(timeline) >= 2) else 0.0

        target_query = (target_conversation_id or "").strip()
        target_query_low = target_query.lower()

        # Scan all today-touched transcripts without truncating at 40 so swarm runs never undercount
        all_recent = self._iter_recent_transcripts(lookback_cutoff, today_start)
        convs: List[ConversationSummary] = []
        target_conv: Optional[ConversationSummary] = None
        collected_main_count = 0

        total_today_steps = 0
        total_main_today_steps = 0
        total_subagent_today_steps = 0
        account_today_steps = 0
        account_main_today_steps = 0
        account_subagent_today_steps = 0
        last_1h_steps = 0
        hourly_counts: Dict[int, int] = {}
        one_hour_cutoff = now - 3600.0

        for idx_conv, (conv_id, tp) in enumerate(all_recent):
            raw_steps, has_user_explicit, has_subagent_marker = self._read_transcript_cached(conv_id, tp)
            if not raw_steps:
                continue

            today_timestamps = [
                float(s.get("timestamp", 0.0) or 0.0)
                for s in raw_steps
                if float(s.get("timestamp", 0.0) or 0.0) >= today_start
            ]
            if has_user_explicit:
                is_sub_conv = False
            elif has_subagent_marker:
                is_sub_conv = True
            elif today_timestamps:
                is_sub_conv = self._check_subagent_hint_cached(conv_id)
            else:
                is_sub_conv = False

            for sts in today_timestamps:
                total_today_steps += 1
                if is_sub_conv:
                    total_subagent_today_steps += 1
                else:
                    total_main_today_steps += 1

                in_scope = True
                if cur_acct_label:
                    owner = self._ledger.attribute_timestamp(sts, timeline, fallback_label=cur_acct_label)
                    in_scope = owner.lower() == cur_acct_label.lower()
                if in_scope:
                    if cur_acct_label:
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
            # Keep top 24 overall AND always protect up to 12 MAIN conversations + target conversation
            # so a burst of swarm subagents can never push active main conversations out of T1 evaluation!
            want_summary = idx_conv < 24 or (not is_sub_conv and collected_main_count < 12) or matches_target
            if not want_summary:
                continue

            summary = self._build_conversation_summary(
                conv_id=conv_id,
                raw_steps=raw_steps,
                has_user_explicit=has_user_explicit,
                has_subagent_marker=has_subagent_marker or is_sub_conv,
                now=now,
                switch_timestamps=switch_timestamps,
                current_acct_since_ts=current_acct_since_ts,
            )
            if summary is None:
                continue
            convs.append(summary)
            if not summary.is_subagent:
                collected_main_count += 1
            if matches_target and (target_conv is None or conv_id.lower() == target_query_low):
                target_conv = summary

        # Fallback: if --conversation-id targets an older conversation outside lookback_cutoff, load it directly
        if target_query_low and target_conv is None:
            resolved_tp = self._resolve_conversation_transcript(target_query)
            if resolved_tp is not None:
                res_id, res_path = resolved_tp
                raw_steps, has_ue, has_sm = self._read_transcript_cached(res_id, res_path)
                if raw_steps:
                    target_conv = self._build_conversation_summary(
                        conv_id=res_id,
                        raw_steps=raw_steps,
                        has_user_explicit=has_ue,
                        has_subagent_marker=has_sm,
                        now=now,
                        switch_timestamps=switch_timestamps,
                        current_acct_since_ts=current_acct_since_ts,
                    )

        convs.sort(key=lambda c: c.latest_timestamp, reverse=True)
        # Preserve up to 12 main conversations + fill remaining slots up to 24 by recency
        protected_mains = [c for c in convs if not c.is_subagent][:12]
        seen_ids = {c.conversation_id for c in protected_mains}
        top_convs: List[ConversationSummary] = list(protected_mains)
        for c in convs:
            if len(top_convs) >= 24:
                break
            if c.conversation_id not in seen_ids:
                top_convs.append(c)
                seen_ids.add(c.conversation_id)
        top_convs.sort(key=lambda c: c.latest_timestamp, reverse=True)

        for c in top_convs:
            self._enrich_conv_from_db(c)
        if target_conv is not None and target_conv.conversation_id not in seen_ids:
            self._enrich_conv_from_db(target_conv)

        # When switched between >=2 accounts today, anchor primary & recent health strictly to steps
        # executed under the CURRENT active account so a freshly switched healthy account never inherits
        # the previous account's 0.0% cache bar, T1 misses, or T2 503/timeout counts.
        acct_cutoff_ts = (current_acct_since_ts - 2.0) if current_acct_since_ts > 0.0 else 0.0
        acct_top_convs = (
            [c for c in top_convs if c.latest_timestamp >= acct_cutoff_ts]
            if current_acct_since_ts > 0.0
            else top_convs
        )

        # Prefer latest active MAIN conversation of the CURRENT account for headline/speed/cache bar
        main_convs = [c for c in acct_top_convs if not c.is_subagent]
        primary = main_convs[0] if main_convs else (acct_top_convs[0] if acct_top_convs else None)
        active_family = (
            "3p"
            if (primary and any(k in primary.model_raw.lower() for k in ("claude", "fable", "opus", "sonnet", "gpt")))
            else "gemini"
        )
        if active_family != self._last_quota_family:
            self._last_quota_family = active_family
            if self._account_cache.pools:
                self._sync_account_active_pool(self._account_cache, active_family)
            else:
                self._account_cache = self._poll_account_and_quota(now, active_family)
                self._last_account_poll = now
                if self._account_cache.online and self._account_cache.short_label:
                    self._ledger.observe_online(self._account_cache.short_label, now)

        if cur_acct_label:
            steps_scope_label = cur_acct_label
            log_scope_label = cur_acct_label
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

        # Large-context freshness tracks MAIN conversations only (retired subagents never hijack freshness)
        # Priority: 0) active invalid cr=0 -> 1) still-warm (remaining > 0, soonest expiring first) -> 2) recently expired (most recent first)
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

        acct_log_filter: Optional[Callable[[float], bool]] = None
        if cur_acct_label and timeline:
            acct_log_filter = (
                lambda ev_ts: self._ledger.attribute_timestamp(ev_ts, timeline, fallback_label=cur_acct_label).lower()
                == cur_acct_label.lower()
            )
        recent_log, today_log, notable_events = self._log_tailer.get_counts(
            now,
            self.config["log_sliding_window_min"],
            account_since_ts=current_acct_since_ts,
            account_filter_fn=acct_log_filter,
        )
        machine_today_log = self._log_tailer.get_machine_today_counts(now)
        last_t2_error_ts = self._log_tailer.get_latest_error_ts(
            now,
            self.config["log_sliding_window_min"],
            categories=("503", "timeout"),
            account_since_ts=current_acct_since_ts,
        )

        active_model = primary.model if primary else "等待请求"
        primary_hit = primary.latest_hit_rate if primary else None
        primary_hit_cls = primary.recent_steps[-1].classification if (primary and primary.recent_steps) else ""
        primary_tps = primary.tps if primary else None
        primary_ttft = primary.ttft_sec if primary else None
        primary_speed = primary.speed_status if primary else "unknown"

        recent_cutoff = max(now - self.config["log_sliding_window_min"] * 60, acct_cutoff_ts)
        recent_active = [c for c in acct_top_convs if c.latest_timestamp >= recent_cutoff]
        has_live_transcript = any((now - c.latest_timestamp) <= 120.0 for c in top_convs)
        max_consec_miss = max((c.consec_miss for c in recent_active), default=0)
        any_single_miss = any(c.single_miss for c in recent_active)
        any_slow = any(c.speed_status == "slow" for c in recent_active[:2])
        wasted_total = sum(c.wasted_cold_tokens for c in recent_active)
        max_abnormal_r10 = max((c.abnormal_in_recent10 for c in recent_active), default=0)
        # Prevent intermittent T1 accounts (e.g. 8/10 misses + 1 lucky hit) from washing back to T0 green
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

        model_stats = self._compute_recent_model_stats(
            recent_active=recent_active,
            cutoff_ts=recent_cutoff,
            last_t2_error_ts=last_t2_error_ts,
        )

        level, grade, headline, sub_headline, t2_recovery_note = self._evaluate_health(
            active_model=active_model,
            recent_log=recent_log,
            today_log=today_log,
            max_consec_miss=max_consec_miss,
            any_single_miss=any_single_miss,
            any_slow=any_slow,
            today_steps=today_steps,
            primary=primary,
            is_online=self._account_cache.online,
            has_live_transcript=has_live_transcript,
            severe_frequent_miss_conv=severe_frequent_miss_conv,
            unstable_recovery_conv=unstable_recovery_conv,
            model_stats=model_stats,
            last_t2_error_ts=last_t2_error_ts,
            now_ts=now,
            switched_acct_label=cur_acct_label if current_acct_since_ts > 0.0 else "",
        )

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
        )

        primary_ttft_avg = primary.ttft_avg_sec if primary else None
        primary_ttft_max = primary.ttft_max_sec if primary else None
        primary_recent_ttfts = list(primary.recent_ttfts) if primary else []

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
        )

    @staticmethod
    def _compute_recent_model_stats(
        recent_active: List[ConversationSummary],
        cutoff_ts: float,
        last_t2_error_ts: float = 0.0,
    ) -> List[ModelPerfStats]:
        """Aggregate per-model TTFT, TPS, cache hits, and post-503 recovery evidence."""
        buckets: Dict[str, Dict[str, Any]] = {}
        for c in recent_active:
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

        out: List[ModelPerfStats] = []
        for mname, b in buckets.items():
            tl = b["ttfts"]
            pl = b["tpss"]
            ptl = b["post_ttfts"]
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
    ) -> Tuple[str, str, str, str, str]:
        win = self.config["log_sliding_window_min"]
        # When Antigravity desktop client is not running (no live hub) and no very recent (<2m) active steps,
        # stay quietly in standby instead of alerting on stale pre-shutdown log disconnects.
        if not is_online and not has_live_transcript:
            return ("green", "IDLE", "离线待机 · Antigravity 未运行", "客户端未启动，已暂停实时异常告警", "")

        # Only evaluate live language_server.log T2/T3 alerts while Antigravity LS is actually online.
        eff_403 = recent_log["403"] if is_online else 0
        eff_timeout = recent_log["timeout"] if is_online else 0
        eff_503 = recent_log["503"] if is_online else 0

        if eff_403 > 0:
            return ("red", "T3", f"T3 封禁告警 · {active_model}", "检测到 403 TOS/权限拒绝，请立即停止使用当前账号", "")

        # Dynamic T2 evaluation: inspect post-error successful steps & per-model TTFT instead of permanently
        # locking the account in T2 for 30m after transient/single-model 503 bursts.
        t2_active = False
        t2_recovering_warn = ""
        t2_recovered_info = ""
        if eff_timeout > 0 or eff_503 >= 2:
            stats_list = model_stats or []
            primary_ms = next((m for m in stats_list if m.model == active_model), None)
            if primary_ms is None and stats_list:
                primary_ms = max(stats_list, key=lambda m: (m.steps_after_last_t2, m.latest_timestamp))
            post_steps = primary_ms.steps_after_last_t2 if primary_ms is not None else 0
            post_ttft_avg = primary_ms.ttft_after_last_t2_avg if primary_ms is not None else None
            post_ttft_latest = primary_ms.ttft_after_last_t2_latest if primary_ms is not None else None
            post_model_name = primary_ms.model if primary_ms is not None else active_model
            elapsed_since_err = (now_ts - last_t2_error_ts) if (now_ts > 0 and last_t2_error_ts > 0) else 0.0
            ttft_severe = (
                (post_ttft_latest is not None and post_ttft_latest >= 60.0)
                or (post_ttft_avg is not None and post_ttft_avg >= 60.0)
            )
            ttft_ok = (
                (post_ttft_latest is None or post_ttft_latest < 45.0)
                and (post_ttft_avg is None or post_ttft_avg < 45.0)
            )
            err_desc = f"{eff_503} 次 503" + (f" / {eff_timeout} 次超时" if eff_timeout > 0 else "")
            ttft_desc = f"，TTFT 均 {post_ttft_avg:.1f}s" if post_ttft_avg is not None else ""

            if post_steps >= 6 and elapsed_since_err >= 600.0 and ttft_ok:
                t2_recovered_info = f"前序 {err_desc} 已平复（{post_model_name} 事后连跑 {post_steps} 步正常{ttft_desc}）"
            elif post_steps >= 2 and not ttft_severe and ttft_ok:
                t2_recovering_warn = f"前序 {err_desc}，{post_model_name} 事后已连跑 {post_steps} 步正常{ttft_desc}"
            else:
                t2_active = True

        if t2_active:
            extra = ""
            if primary is not None and primary.ttft_sec is not None:
                extra = f"（当前 TTFT {primary.ttft_sec:.1f}s）"
            return (
                "red",
                "T2",
                f"T2 限流黑洞 · {active_model}",
                f"近 {win} 分钟触发 {eff_503} 次 503 / {eff_timeout} 次 5m 超时，事后未恢复稳定响应{extra}",
                "",
            )
        if max_consec_miss >= 2:
            return (
                "red",
                "T1",
                f"T1 连续掉缓存 · {active_model}",
                f"主活跃会话已连续 {max_consec_miss} 步 cr=0，建议检查账号或切回主力号",
                t2_recovering_warn or t2_recovered_info,
            )
        if severe_frequent_miss_conv is not None:
            pct = int(round((severe_frequent_miss_conv.recent10_hit_ratio or 0.0) * 100))
            return (
                "red",
                "T1",
                f"T1 频繁掉缓存 · {active_model}",
                f"近 10 步异常归零 {severe_frequent_miss_conv.abnormal_in_recent10}/{severe_frequent_miss_conv.cacheable_in_recent10} 步（有效命中仅 {pct}%），单步命中未恢复健康",
                t2_recovering_warn or t2_recovered_info,
            )
        if today_steps >= self.config["daily_steps_alert"]:
            return (
                "red",
                "WARN",
                f"步数超红线 · {active_model}",
                f"今日累计 {today_steps} 步已达 {self.config['daily_steps_alert']} 步保护红线，建议减速防降权",
                t2_recovering_warn or t2_recovered_info,
            )
        if (
            t2_recovering_warn
            or any_single_miss
            or unstable_recovery_conv is not None
            or eff_503 == 1
            or any_slow
            or today_steps >= self.config["daily_steps_warn"]
        ):
            reasons = []
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
            if today_steps >= self.config["daily_steps_warn"]:
                reasons.append(f"今日步数 {today_steps}")
            return ("orange", "WARN", f"留意预警 · {active_model}", " / ".join(reasons), t2_recovering_warn)

        if primary is None:
            if switched_acct_label:
                return (
                    "green",
                    "T0",
                    f"T0 待命 · 已切入 {switched_acct_label}",
                    "新账号尚无新请求，前序账号异常与缓存状态已自动隔离",
                    "",
                )
            return ("green", "T0", "T0 待命 · 暂无活跃会话", "纯本地只读监控已就绪", "")

        if t2_recovered_info:
            return ("green", "T0", f"T0 健康 · {active_model}", t2_recovered_info, t2_recovered_info)

        hist_503 = today_log["503"]
        if hist_503 > 0 and eff_503 == 0:
            sub = f"近 {win} 分钟无新异常（当前号今日历史 {hist_503} 条 503 已自动衰减）"
        else:
            sub = f"近 {win} 分钟缓存与底层日志均健康"
        return ("green", "T0", f"T0 健康 · {active_model}", sub, "")

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
    ) -> List[Tuple[str, str, str]]:
        if grade == "IDLE" or (not is_online and not recent_active):
            self._baseline_ready = True
            return []
        candidates: List[Tuple[str, str, str]] = []
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
            for key, _, _ in candidates:
                self._emitted_alert_keys.append(key)
            self._baseline_ready = True
            return []

        fresh: List[Tuple[str, str, str]] = []
        for item in candidates:
            if item[0] not in self._emitted_alert_keys:
                self._emitted_alert_keys.append(item[0])
                fresh.append(item)
        return fresh

    def _iter_recent_transcripts(self, lookback_cutoff: float, today_start: float) -> List[Tuple[str, Path]]:
        if not self.brain_dir.is_dir():
            return []
        today_items: List[Tuple[float, str, Path]] = []
        older_items: List[Tuple[float, str, Path]] = []
        try:
            with os.scandir(self.brain_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    tp = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
                    try:
                        st = tp.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_mtime < lookback_cutoff:
                        continue
                    if st.st_mtime >= today_start:
                        today_items.append((st.st_mtime, entry.name, tp))
                    else:
                        older_items.append((st.st_mtime, entry.name, tp))
        except OSError:
            return []

        today_items.sort(key=lambda item: item[0], reverse=True)
        older_items.sort(key=lambda item: item[0], reverse=True)
        merged = today_items + older_items[:16]
        return [(cid, tp) for _, cid, tp in merged]

    def _read_transcript_cached(self, conv_id: str, tp: Path) -> Tuple[List[Dict[str, Any]], bool, bool]:
        try:
            st = tp.stat()
        except OSError:
            self._transcript_cache.pop(conv_id, None)
            return [], False, False

        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._transcript_cache.get(conv_id)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2], cached[3]

        parsed: List[Dict[str, Any]] = []
        has_user_explicit = False
        has_subagent_marker = False
        pending_checkpoint = False
        try:
            with tp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not has_user_explicit and '"USER_EXPLICIT"' in line:
                        has_user_explicit = True
                    if not has_subagent_marker and (
                        '"SUBAGENT"' in line or '"SWARM"' in line or "[swarm]" in line or '"source":"AGENT"' in line
                    ):
                        has_subagent_marker = True
                    if '"CHECKPOINT"' in line:
                        try:
                            chk_obj = json.loads(line)
                            if chk_obj.get("type") == "CHECKPOINT":
                                pending_checkpoint = True
                        except json.JSONDecodeError:
                            pass
                    if '"PLANNER_RESPONSE"' not in line or '"input_tokens"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "PLANNER_RESPONSE" or "input_tokens" not in obj:
                        continue
                    ts = parse_iso_timestamp(str(obj.get("created_at", "") or "")) or st.st_mtime
                    parsed.append(
                        {
                            "step_index": obj.get("step_index") if obj.get("step_index") is not None else len(parsed),
                            "timestamp": ts,
                            "model": obj.get("model", ""),
                            "input_tokens": obj.get("input_tokens", 0),
                            "cache_read_tokens": obj.get("cache_read_tokens", 0),
                            "output_tokens": obj.get("output_tokens", 0),
                            "after_checkpoint": pending_checkpoint,
                        }
                    )
                    pending_checkpoint = False
        except OSError:
            return (cached[1], cached[2], cached[3]) if cached is not None else ([], False, False)

        self._transcript_cache[conv_id] = (stamp, parsed, has_user_explicit, has_subagent_marker)
        if len(self._transcript_cache) > 256:
            oldest_key = next(iter(self._transcript_cache))
            self._transcript_cache.pop(oldest_key, None)
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
    ) -> Optional[ConversationDetailReport]:
        """On-demand full-history report (context growth, TTFT, TPS, cache) for one conversation."""
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
        switch_timestamps = self._ledger.get_switch_timestamps(min(today_start - 3600, first_ts or now - 86400))

        steps = classify_steps(
            raw_steps,
            idle_rewarm_sec=self.config["idle_rewarm_sec"],
            account_switches=switch_timestamps,
        )
        if not steps:
            return None

        gens = self._db_reader.read_full_history(conv_id)
        is_subagent = (not has_user_explicit) and (has_subagent_marker or self._check_subagent_hint_cached(conv_id))
        aligned_gens = _align_steps_with_gen_history(steps, gens)

        points: List[ConversationDetailPoint] = []
        max_ctx_limit = 0
        for st, gen in zip(steps, aligned_gens):
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
                )
            )

        latest = steps[-1]
        today_steps = sum(1 for s in steps if s.timestamp >= today_start)
        peak_ctx = max(s.total_tokens for s in steps)
        compact_count = sum(1 for s in steps if s.reason == "compact_reset" or (s.context_delta <= -32000 and s.total_tokens < (s.total_tokens - s.context_delta) * 0.75))
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

        fresh_rem: Optional[int] = None
        fresh_invalid = ""
        if latest.total_tokens >= self.config["large_context_threshold"] and latest.timestamp > 0:
            age = now - latest.timestamp
            if age <= self.config["freshness_window_sec"] + 300:
                if latest.classification == "abnormal_miss":
                    fresh_invalid = "本步 cr=0 无热缓存"
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
            return cached[1]
        try:
            # Annotation files are tiny metadata files (<16 KB); bound read defensively
            with pbtxt.open("rb") as fh:
                raw = fh.read(65536)
        except OSError:
            return cached[1] if cached is not None else ""
        title = parse_pbtxt_title(raw)
        self._annotation_title_cache[conv_id] = (stamp, title)
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


def _align_steps_with_gen_history(steps: List[StepRecord], gens: List[Any]) -> List[Optional[Any]]:
    """Align full transcript StepRecords with SQLite GenTelemetry rows using reverse token matching."""
    if not steps:
        return []
    if not gens:
        return [None] * len(steps)
    if len(steps) == len(gens):
        return list(gens)

    aligned: List[Optional[Any]] = [None] * len(steps)
    gi = len(gens) - 1
    for si in range(len(steps) - 1, -1, -1):
        if gi < 0:
            break
        st = steps[si]
        tol = max(64, int(st.total_tokens * 0.02))
        matched_idx = -1
        scan_lo = max(-1, gi - 8)
        for cand in range(gi, scan_lo, -1):
            g = gens[cand]
            if abs(g.total_in - st.total_tokens) <= tol and abs(g.cached_in - st.cache_read_tokens) <= tol:
                matched_idx = cand
                break
        if matched_idx >= 0:
            aligned[si] = gens[matched_idx]
            gi = matched_idx - 1
        elif abs(len(steps) - len(gens)) <= 3:
            aligned[si] = gens[gi]
            gi -= 1
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
            }
            for m in snap.model_stats
        ],
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
    lines = [
        f"Antigravity 单对话长线体检 ({title_part} · {role} · {model_overview})",
        f"步数概况: 总计 {report.total_steps} 步 (今日 {report.today_steps} 步) | 压缩 {report.compact_count} 次",
        f"请求体规模: 当前 {format_tokens(report.latest_context_tokens)} ({format_delta_tokens(report.latest_context_delta)}) | 峰值 {format_tokens(report.peak_context_tokens)}",
        f"缓存统计: 有效命中率 {hit_ratio} (命中 {report.cache_hit_steps} / 异常归零 {report.abnormal_miss_steps} / 正常冷启 {report.expected_cold_steps})",
        f"速度统计: TTFT 均 {avg_t} / P95 {p95_t} / 峰 {max_t} | 平均吞吐 {avg_p}",
        "最近 12 步明细（新 → 旧）:",
    ]
    for pt in reversed(report.points[-12:]):
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%m-%d %H:%M:%S") if pt.timestamp > 0 else "-- --:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f}t/s" if pt.tps is not None else "—"
        lines.append(
            f"  #{pt.step_index:<4} {hm} │ [{pt.model}] │ {format_tokens(pt.total_tokens):>6} ({format_delta_tokens(pt.context_delta):>7}) "
            f"│ 缓存 {pt.hit_rate:5.1f}% │ 首字 {t_s:>6} │ {p_s:>6} [{pt.reason}]"
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
        f"当前账号: {snap.account.short_label} | 5h 额度 {acc_5h} | 周额度 {acc_wk}",
    ]
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
            f"底层日志 ({snap.log_scope_label} · 近 {snap.sliding_window_min}m / 今日): 503={snap.recent_503}/{snap.today_503} | 5m超时={snap.recent_timeout}/{snap.today_timeout} | 403={snap.recent_403}/{snap.today_403} | 代理重试={snap.recent_retry}/{snap.today_retry}",
        ]
    )
    if len(snap.model_stats) >= 2:
        ms_parts = []
        for ms in snap.model_stats[:4]:
            ta = f"均{ms.ttft_avg_sec:.1f}s/峰{ms.ttft_max_sec:.1f}s" if (ms.ttft_avg_sec is not None and ms.ttft_max_sec is not None) else "—"
            ms_parts.append(f"{ms.model}({ms.step_count}步 TTFT {ta})")
        lines.append("近 30m 分模型 TTFT: " + " | ".join(ms_parts))
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
