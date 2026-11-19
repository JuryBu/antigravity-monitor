"""Pure read-only telemetry engine for Antigravity desktop monitor.

Reads only local files and localhost Language Server status endpoints:
  1. ~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl
  2. ~/.gemini/antigravity/conversations/<id>.db  (read-only SQLite + WAL stamp cache)
  3. %APPDATA%/Antigravity/logs/language_server.log (incremental seek + rotation guard)
  4. ~/.gemini/antigravity/daemon/hub_*.json (localhost 127.0.0.1 read-only GetUserStatus)
     + ~/.antigravity_cockpit/platform-quota-history-v1.sqlite (optional read-only fallback)
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from core.config_store import DEFAULT_CONFIG, sanitize_config
from core.sqlite_reader import ConversationDbReader, ConversationDbSnapshot

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
RETRY_RE = re.compile(r"run\.go:\d+\].*attempt\s+\d+\s+failed", re.IGNORECASE)
# Supports both ISO-8601 (YYYY-MM-DD...) and Go stdlog slash dates (YYYY/MM/DD HH:MM:SS)
FULL_TS_RE = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
# Anchor bare HH:MM:SS fallback near line prefix (e.g. glog W1120 02:14:39) to avoid body text numbers
PREFIX_HMS_RE = re.compile(r"^.{0,36}?\b(\d{2}:\d{2}:\d{2})\b")

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


def format_countdown(seconds: Optional[int]) -> str:
    if seconds is None:
        return "闲置"
    if seconds <= 0:
        return "已过期"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"


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
    ttft_sec: Optional[float] = None
    streaming_sec: Optional[float] = None
    tps: Optional[float] = None
    shard_tag: str = ""
    speed_status: str = "unknown"  # "ok" | "slow" | "unknown"


@dataclass(frozen=True)
class LogEvent:
    timestamp: float
    category: str   # "503" | "timeout" | "403" | "retry"
    summary: str
    event_key: str


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


@dataclass
class MonitorSnapshot:
    generated_at: float
    level: str            # "green" | "orange" | "red"
    grade: str            # "T0" | "WARN" | "T1" | "T2" | "T3"
    headline: str
    sub_headline: str
    active_model: str
    primary_hit_rate: Optional[float]
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


def default_antigravity_paths() -> Dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    return {
        "brain_dir": home / ".gemini" / "antigravity" / "brain",
        "conversations_dir": home / ".gemini" / "antigravity" / "conversations",
        "daemon_dir": home / ".gemini" / "antigravity" / "daemon",
        "ls_log_path": appdata / "Antigravity" / "logs" / "language_server.log",
        "cockpit_quota_db": home / ".antigravity_cockpit" / "platform-quota-history-v1.sqlite",
    }


def classify_steps(
    raw_steps: List[Dict[str, Any]],
    idle_rewarm_sec: int = 180,
    min_cacheable_tokens: int = 32000,
) -> List[StepRecord]:
    """Classify consecutive PLANNER_RESPONSE steps, filtering normal zero-cache causes."""
    classified: List[StepRecord] = []
    prev_ts: Optional[float] = None
    prev_model: Optional[str] = None
    prev_total: Optional[int] = None

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

        if cr > 0:
            cls, reason = "hit", "cached"
        elif prev_ts is None:
            cls, reason = "expected_cold", "first_turn"
        elif ts > 0 and prev_ts > 0 and (ts - prev_ts) > idle_rewarm_sec:
            cls, reason = "expected_cold", "idle_rewarm"
        elif prev_model and model and model.lower() != prev_model.lower():
            cls, reason = "expected_cold", "model_switch"
        elif prev_total is not None and prev_total >= min_cacheable_tokens and total < prev_total * 0.75:
            cls, reason = "expected_cold", "compact_reset"
        elif total < min_cacheable_tokens:
            cls, reason = "expected_cold", "small_ctx"
        elif prev_total is not None and prev_total < min_cacheable_tokens:
            # First turn crossing the >=32K implicit cache threshold cannot have a prior warm prefix yet
            cls, reason = "expected_cold", "first_cacheable"
        else:
            cls, reason = "abnormal_miss", "abnormal_zero"

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

        if size == self._offset and self._initialized:
            return

        discard_first_partial = False
        if not self._initialized and size > 2 * 1024 * 1024:
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

        # Only consume up to the last complete newline so concurrent half-written lines are never split
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
            event = self._parse_line(line, now)
            if event is not None:
                self._events.append(event)

    def _parse_line(self, line: str, fallback_ts: float) -> Optional[LogEvent]:
        if not line:
            return None
        low = line.lower()
        if "127.0.0.1" in low or "localhost" in low or "tls handshake error" in low:
            return None

        category: Optional[str] = None
        label = ""
        if "tos_violation" in low or ("403" in low and "permission_denied" in low):
            category = "403"
            label = "403 TOS/权限拒绝"
        elif "stream idle timeout" in low or "timed out after 5m" in low or (
            "context deadline exceeded" in low
            and not any(
                noise in low
                for noise in ("fetchconfig", "code_assist_client", "remote_config", "metadata", "health", "getuserstatus")
            )
        ):
            category = "timeout"
            label = "5m 流超时"
        elif "no capacity" in low or ("503" in low and ("unavailable" in low or "service" in low or "error" in low or "status" in low)):
            category = "503"
            label = "服务端 503 No capacity"
        elif RETRY_RE.search(line):
            category = "retry"
            label = "代理/流断连重试"

        if category is None:
            return None

        ts = self._extract_timestamp(line, fallback_ts)
        hm = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        clean_detail = mask_email(line.strip())
        if len(clean_detail) > 96:
            clean_detail = clean_detail[:93] + "..."
        summary = f"[{hm}] {label}"
        event_key = f"log:{category}:{int(ts)}:{hash(clean_detail) & 0xFFFF:04x}"
        return LogEvent(timestamp=ts, category=category, summary=summary, event_key=event_key)

    @staticmethod
    def _extract_timestamp(line: str, fallback_ts: float) -> float:
        m_full = FULL_TS_RE.search(line)
        if m_full:
            iso_norm = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}T{m_full.group(4)}"
            parsed = parse_iso_timestamp(iso_norm)
            if parsed is not None:
                return parsed
        m_hms = PREFIX_HMS_RE.search(line)
        if m_hms:
            try:
                now_dt = datetime.datetime.fromtimestamp(fallback_ts)
                h, mi, s = (int(p) for p in m_hms.group(1).split(":"))
                cand = now_dt.replace(hour=h, minute=mi, second=s, microsecond=0)
                if cand.timestamp() > fallback_ts + 300:
                    cand -= datetime.timedelta(days=1)
                return cand.timestamp()
            except ValueError:
                pass
        return fallback_ts

    def get_counts(self, now_ts: float, window_min: int) -> Tuple[Dict[str, int], Dict[str, int], List[LogEvent]]:
        cutoff = now_ts - max(1, window_min) * 60
        today_start = datetime.datetime.fromtimestamp(now_ts).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        recent = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        today = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
        in_window_notable: List[LogEvent] = []

        for ev in self._events:
            if ev.timestamp >= today_start:
                today[ev.category] = today.get(ev.category, 0) + 1
            if ev.timestamp >= cutoff:
                recent[ev.category] = recent.get(ev.category, 0) + 1
                if ev.category in ("503", "timeout", "403"):
                    in_window_notable.append(ev)

        return recent, today, in_window_notable[-3:]


class AntigravityMonitorEngine:
    """Stateful incremental read-only monitor combining all 4 local data sources."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        paths: Optional[Dict[str, Path]] = None,
    ) -> None:
        self.config = sanitize_config(config or DEFAULT_CONFIG)
        base_paths = default_antigravity_paths()
        if paths:
            base_paths.update(paths)
        self.brain_dir = Path(base_paths["brain_dir"])
        self.conversations_dir = Path(base_paths["conversations_dir"])
        self.daemon_dir = Path(base_paths["daemon_dir"])
        self.ls_log_path = Path(base_paths["ls_log_path"])
        self.cockpit_quota_db = Path(base_paths["cockpit_quota_db"])

        self._transcript_cache: Dict[str, Tuple[Tuple[int, int], List[Dict[str, Any]]]] = {}
        self._db_reader = ConversationDbReader(self.conversations_dir, max_cache_entries=12, tail_rows=12)
        self._log_tailer = LanguageServerLogTailer(self.ls_log_path, max_events=200)
        self._account_cache: AccountQuotaInfo = AccountQuotaInfo()
        self._last_account_poll: float = 0.0
        self._last_quota_family: str = "gemini"
        self._baseline_ready: bool = False
        self._emitted_alert_keys: Deque[str] = deque(maxlen=256)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = sanitize_config(config)

    def poll(self, now_ts: Optional[float] = None, force_account: bool = False) -> MonitorSnapshot:
        now = now_ts if now_ts is not None else time.time()
        self._log_tailer.poll(now)

        today_start = datetime.datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        lookback_cutoff = min(today_start - 3600, now - 24 * 3600)

        # Scan all today-touched transcripts without truncating at 40 so swarm runs never undercount
        all_recent = self._iter_recent_transcripts(lookback_cutoff, today_start)
        convs: List[ConversationSummary] = []
        today_steps = 0

        for idx_conv, (conv_id, tp) in enumerate(all_recent):
            raw_steps = self._read_transcript_cached(conv_id, tp)
            if not raw_steps:
                continue

            for s in raw_steps:
                if float(s.get("timestamp", 0.0) or 0.0) >= today_start:
                    today_steps += 1

            # Only build full ConversationSummary for the top 16 most recently updated conversations
            if idx_conv >= 16:
                continue

            steps = classify_steps(
                raw_steps,
                idle_rewarm_sec=self.config["idle_rewarm_sec"],
            )
            if not steps:
                continue

            latest = steps[-1]
            recent10 = steps[-10:]

            consec_miss = 0
            consec_start_step = latest.step_index
            for s in reversed(steps):
                if s.classification == "abnormal_miss":
                    consec_miss += 1
                    consec_start_step = s.step_index
                else:
                    break
            single_miss = consec_miss == 1
            wasted_cold = sum(s.input_tokens for s in recent10 if s.classification == "abnormal_miss")

            fresh_rem: Optional[int] = None
            fresh_invalid = ""
            if latest.total_tokens >= self.config["large_context_threshold"] and latest.timestamp > 0:
                age = now - latest.timestamp
                if age <= 15 * 60:
                    if latest.classification == "abnormal_miss":
                        fresh_invalid = f"无热缓存 (连掉 {consec_miss} 步)" if consec_miss >= 2 else "本步 cr=0 无热缓存"
                    else:
                        fresh_rem = int(self.config["freshness_window_sec"] - age)

            convs.append(
                ConversationSummary(
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
                )
            )

        convs.sort(key=lambda c: c.latest_timestamp, reverse=True)
        top_convs = convs[:4]

        for c in top_convs:
            snap: Optional[ConversationDbSnapshot] = self._db_reader.read(c.conversation_id)
            if snap and snap.latest:
                gen = snap.latest
                c.ttft_sec = gen.ttft_sec
                c.streaming_sec = gen.streaming_sec
                c.tps = gen.tps
                c.max_context_tokens = snap.max_context_tokens
                c.shard_tag = gen.shard_tag
                c.speed_status = self._classify_speed(gen.output_tokens, gen.tps, gen.ttft_sec, c.context_tokens)

        primary = top_convs[0] if top_convs else None
        active_family = "3p" if (primary and any(k in primary.model_raw.lower() for k in ("claude", "fable", "opus", "sonnet", "gpt"))) else "gemini"
        if force_account or active_family != self._last_quota_family or (now - self._last_account_poll >= 15.0):
            self._account_cache = self._poll_account_and_quota(now, active_family)
            self._last_account_poll = now
            self._last_quota_family = active_family

        # Large-context freshness candidates (include invalid cr=0 large conversations so UI never hides them)
        large_freshness = [
            c for c in convs
            if c.freshness_remaining_sec is not None or c.freshness_invalid_reason
        ]
        large_freshness.sort(
            key=lambda c: (
                0 if c.freshness_invalid_reason else 1,
                c.freshness_remaining_sec if c.freshness_remaining_sec is not None else 999999,
            )
        )

        min_fresh_sec: Optional[int] = None
        min_fresh_conv = ""
        min_fresh_ctx = 0
        min_fresh_invalid = ""
        if large_freshness:
            head_f = large_freshness[0]
            min_fresh_sec = head_f.freshness_remaining_sec
            min_fresh_conv = head_f.short_id
            min_fresh_ctx = head_f.context_tokens
            min_fresh_invalid = head_f.freshness_invalid_reason

        recent_log, today_log, notable_events = self._log_tailer.get_counts(
            now, self.config["log_sliding_window_min"]
        )

        active_model = primary.model if primary else "等待请求"
        primary_hit = primary.latest_hit_rate if primary else None
        primary_tps = primary.tps if primary else None
        primary_ttft = primary.ttft_sec if primary else None
        primary_speed = primary.speed_status if primary else "unknown"

        recent_cutoff = now - self.config["log_sliding_window_min"] * 60
        recent_active = [c for c in top_convs if c.latest_timestamp >= recent_cutoff]
        max_consec_miss = max((c.consec_miss for c in recent_active), default=0)
        any_single_miss = any(c.single_miss for c in recent_active)
        any_slow = any(c.speed_status == "slow" for c in recent_active[:2])
        wasted_total = sum(c.wasted_cold_tokens for c in recent_active)

        level, grade, headline, sub_headline = self._evaluate_health(
            active_model=active_model,
            recent_log=recent_log,
            today_log=today_log,
            max_consec_miss=max_consec_miss,
            any_single_miss=any_single_miss,
            any_slow=any_slow,
            today_steps=today_steps,
            primary=primary,
        )

        alerts = self._collect_new_alerts(
            grade=grade,
            notable_events=notable_events,
            recent_active=recent_active,
            max_consec_miss=max_consec_miss,
            today_steps=today_steps,
        )

        return MonitorSnapshot(
            generated_at=now,
            level=level,
            grade=grade,
            headline=headline,
            sub_headline=sub_headline,
            active_model=active_model,
            primary_hit_rate=primary_hit,
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
            large_freshness_list=large_freshness[:4],
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
        )

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
    ) -> Tuple[str, str, str, str]:
        win = self.config["log_sliding_window_min"]
        if recent_log["403"] > 0:
            return ("red", "T3", f"T3 封禁告警 · {active_model}", "检测到 403 TOS/权限拒绝，请立即停止使用当前账号")
        if recent_log["timeout"] > 0 or recent_log["503"] >= 2:
            return (
                "red",
                "T2",
                f"T2 限流黑洞 · {active_model}",
                f"近 {win} 分钟触发 {recent_log['503']} 次 503 / {recent_log['timeout']} 次 5m 超时",
            )
        if max_consec_miss >= 2:
            return (
                "red",
                "T1",
                f"T1 连续掉缓存 · {active_model}",
                f"主活跃会话已连续 {max_consec_miss} 步 cr=0，建议检查账号或切回主力号",
            )
        if today_steps >= self.config["daily_steps_alert"]:
            return (
                "red",
                "WARN",
                f"步数超红线 · {active_model}",
                f"今日累计 {today_steps} 步已达 {self.config['daily_steps_alert']} 步保护红线，建议减速防降权",
            )
        if any_single_miss or recent_log["503"] == 1 or any_slow or today_steps >= self.config["daily_steps_warn"]:
            reasons = []
            if any_single_miss:
                reasons.append("单步异常归零")
            if any_slow:
                reasons.append("疑似暗限速")
            if recent_log["503"] == 1:
                reasons.append("偶发 1 次 503")
            if today_steps >= self.config["daily_steps_warn"]:
                reasons.append(f"今日步数 {today_steps}")
            return ("orange", "WARN", f"留意预警 · {active_model}", " / ".join(reasons))

        if primary is None:
            return ("green", "T0", "T0 待命 · 暂无活跃会话", "纯本地只读监控已就绪")

        hist_503 = today_log["503"]
        if hist_503 > 0 and recent_log["503"] == 0:
            sub = f"近 {win} 分钟无新异常（今日历史 {hist_503} 条 503 已自动衰减）"
        else:
            sub = f"近 {win} 分钟缓存与底层日志均健康"
        return ("green", "T0", f"T0 健康 · {active_model}", sub)

    def _collect_new_alerts(
        self,
        grade: str,
        notable_events: List[LogEvent],
        recent_active: List[ConversationSummary],
        max_consec_miss: int,
        today_steps: int,
    ) -> List[Tuple[str, str, str]]:
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
            # Deduplicate per contiguous cr=0 streak start so repeated consecutive misses notify only once
            offending = next((c for c in recent_active if c.consec_miss >= 2), None)
            if offending is not None:
                key = f"t1:{offending.conversation_id}:{offending.consec_miss_start_step}"
                candidates.append(
                    (key, "Antigravity 疑似 T1 掉缓存", f"{offending.short_id} ({offending.model}) 已连续 {max_consec_miss} 步 cr=0")
                )
        elif today_steps >= self.config["daily_steps_alert"]:
            day = datetime.date.today().isoformat()
            candidates.append(
                (f"steps:{day}:{self.config['daily_steps_alert']}", "Antigravity 今日步数红线", f"今日已达 {today_steps} 步，注意控频护号")
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

    def _read_transcript_cached(self, conv_id: str, tp: Path) -> List[Dict[str, Any]]:
        try:
            st = tp.stat()
        except OSError:
            self._transcript_cache.pop(conv_id, None)
            return []

        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._transcript_cache.get(conv_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        parsed: List[Dict[str, Any]] = []
        try:
            with tp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
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
                        }
                    )
        except OSError:
            return cached[1] if cached is not None else []

        self._transcript_cache[conv_id] = (stamp, parsed)
        if len(self._transcript_cache) > 256:
            oldest_key = next(iter(self._transcript_cache))
            self._transcript_cache.pop(oldest_key, None)
        return parsed

    def _poll_account_and_quota(self, now: float, active_family: str = "gemini") -> AccountQuotaInfo:
        info = AccountQuotaInfo(observed_at=now, quota_family=active_family)
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

            for hp in hub_files[:8]:
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
                    info.online = True

                    configs = (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
                    preferred_keys = ("Claude", "Fable", "Opus", "Sonnet") if active_family == "3p" else ("Neon", "Carbon", "Hydrogen", "Argon", "Gemini")
                    fallback_frac = None
                    for cfg in configs:
                        lbl = str(cfg.get("label", "") or "")
                        frac = (cfg.get("quotaInfo") or {}).get("remainingFraction")
                        if frac is None:
                            continue
                        if fallback_frac is None:
                            fallback_frac = float(frac) * 100.0
                        if any(k in lbl for k in preferred_keys):
                            info.five_hour_pct = float(frac) * 100.0
                            break
                    if info.five_hour_pct is None and fallback_frac is not None:
                        info.five_hour_pct = fallback_frac
                    break
                except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                    continue

        # Only enrich from Cockpit when live Antigravity session is online and email matches strictly
        if info.online and live_email and self.cockpit_quota_db.is_file():
            self._enrich_weekly_from_cockpit(info, live_email, now)
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

        weekly_metric = "3p-weekly" if info.quota_family == "3p" else "gemini-weekly"
        fiveh_metric = "3p-5h" if info.quota_family == "3p" else "gemini-5h"
        stale_cutoff_sec = now - 7 * 86400

        try:
            resolved = self.cockpit_quota_db.resolve().as_posix()
            encoded = urllib.parse.quote(resolved, safe="/:")
            uri = f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.3)
            try:
                cur = conn.cursor()
                for metric in (weekly_metric, fiveh_metric):
                    row = cur.execute(
                        "SELECT remaining, observed_at FROM quota_observations WHERE account_id = ? AND metric = ? "
                        "ORDER BY observed_at DESC LIMIT 1",
                        (bound_acc_id, metric),
                    ).fetchone()
                    if row and row[0] is not None:
                        obs_raw = float(row[1] or 0)
                        obs_sec = obs_raw / 1000.0 if obs_raw > 1e11 else obs_raw
                        if obs_sec and obs_sec < stale_cutoff_sec:
                            continue
                        val = float(row[0])
                        if metric == weekly_metric:
                            info.weekly_pct = val
                        elif info.five_hour_pct is None:
                            info.five_hour_pct = val
            finally:
                conn.close()
        except sqlite3.Error:
            pass


def format_text_summary(snap: MonitorSnapshot) -> str:
    """Build a clean, email-masked plain-text health summary for sharing or CLI output."""
    ts_str = datetime.datetime.fromtimestamp(snap.generated_at).strftime("%Y-%m-%d %H:%M:%S")
    hit_str = f"{snap.primary_hit_rate:.1f}%" if snap.primary_hit_rate is not None else "—"
    tps_str = f"{snap.primary_tps:.1f} tok/s" if snap.primary_tps is not None else "—"
    ttft_str = f"{snap.primary_ttft:.1f}s" if snap.primary_ttft is not None else "—"
    acc_5h = f"{snap.account.five_hour_pct:.1f}%" if snap.account.five_hour_pct is not None else "—"
    acc_wk = f"{snap.account.weekly_pct:.0f}%" if snap.account.weekly_pct is not None else "—"

    lines = [
        f"Antigravity 实时体检摘要 ({ts_str})",
        f"健康评级: [{snap.grade}] {snap.headline}",
        f"状态说明: {snap.sub_headline}",
        f"当前账号: {snap.account.short_label} | 5h 额度 {acc_5h} | 周额度 {acc_wk}",
        f"实时性能: 缓存 {hit_str} | 吞吐 {tps_str} | TTFT {ttft_str} | 今日步数 {snap.today_steps}/{snap.daily_steps_alert}",
        f"底层日志 (近 {snap.sliding_window_min}m / 今日): 503={snap.recent_503}/{snap.today_503} | 5m超时={snap.recent_timeout}/{snap.today_timeout} | 403={snap.recent_403}/{snap.today_403} | 代理重试={snap.recent_retry}/{snap.today_retry}",
    ]
    if snap.active_conversations:
        lines.append("活跃对话 (近 10 步):")
        for c in snap.active_conversations:
            bar = "".join("█" if s.classification == "hit" else ("▒" if s.classification == "expected_cold" else "×") for s in c.recent_steps)
            sp = f" {c.tps:.0f}t/s" if c.tps is not None else ""
            lines.append(
                f"  - {c.short_id} {c.model} {format_tokens(c.context_tokens)} [{bar}] {c.latest_hit_rate:.1f}%{sp}"
            )
    if snap.wasted_cold_tokens_total > 0:
        lines.append(f"冷算提醒: 近 10 步异常归零累计多耗约 {format_tokens(snap.wasted_cold_tokens_total)} Token")
    return mask_email("\n".join(lines))


def main() -> None:
    engine = AntigravityMonitorEngine()
    snap = engine.poll(force_account=True)
    print(format_text_summary(snap))


if __name__ == "__main__":
    main()
