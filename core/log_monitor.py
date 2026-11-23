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
import bisect
import ctypes
from ctypes import wintypes
import datetime
import http.client
import json
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

_DATACLASS_SLOTS: Dict[str, Any] = {"slots": True} if sys.version_info >= (3, 10) else {}

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
RETRY_RE = re.compile(r"(?:^|[\s\[/\\])(?:run|retry)\.go:\d+\].*attempt\s+\d+\s+failed", re.IGNORECASE)
_PROXY_EOF_RE = re.compile(
    r"(?:\bunexpected\s+eof\b|:\s*eof\b|\bforcibly closed\b|\bconnection reset\b|\bbroken pipe\b)",
    re.IGNORECASE,
)
_PROXY_IGNORED_SOURCES = frozenset({"runner.go", "llm_monitor.go", "extract_searchable_content.go"})
_FAST_CREATED_AT_RE = re.compile(r'"created_at"\s*:\s*"([^"]+)"')
_FAST_STEP_IDX_RE = re.compile(r'"step_index"\s*:\s*(\d+)')
_FAST_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]*)"')
_FAST_IN_TOK_RE = re.compile(r'"input_tokens"\s*:\s*(\d+)')
_FAST_CR_TOK_RE = re.compile(r'"cache_read_tokens"\s*:\s*(\d+)')
_FAST_OUT_TOK_RE = re.compile(r'"output_tokens"\s*:\s*(\d+)')
# Strip Go/glog wrapper prefix so timestamp microseconds (e.g. .350309) and "ERROR: logging before google.Init:" never false-match!
GLOG_PREFIX_RE = re.compile(
    r"^(?:ERROR:\s+logging before google\.Init:\s+)?([IWEF])(\d{2})(\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s+(\d+)\s+([^\]]+)\]\s*(.*)$"
)
ATTEMPT_NUM_RE = re.compile(r"\battempt\s+(\d+)\b", re.IGNORECASE)
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


def compute_large_ctx_ttft_allowance(context_tokens: int, max_context_tokens: int = 512000) -> float:
    """Return bounded natural TTFT allowance (0..30s) when approaching the 512K compaction window.

    Calibrated from 6,424 local main-conversation steps: pure-text cached P90 stays flat <=32s
    up to 360K, then climbs sharply to 50.6s (360-420K) and 59.1s (420K+) even at >=200 t/s.
    Hard floor at 300K guarantees zero impact on <300K conversations and 200K-window models.
    """
    ctx = int(context_tokens or 0)
    if ctx < 300_000:
        return 0.0
    cap = int(max_context_tokens or 0)
    eff_ratio = ctx / 512_000.0
    if cap >= 300_000:
        eff_ratio = max(eff_ratio, ctx / float(cap))
    r_start = 300_000.0 / 512_000.0  # 0.5859375
    r_knee = 360_000.0 / 512_000.0   # 0.703125
    r_cap = 480_000.0 / 512_000.0    # 0.9375
    if eff_ratio <= r_start:
        return 0.0
    if eff_ratio <= r_knee:
        val = 10.0 * (eff_ratio - r_start) / (r_knee - r_start)
    else:
        val = 10.0 + 20.0 * min(1.0, (eff_ratio - r_knee) / (r_cap - r_knee))
    return round(min(30.0, max(0.0, val)), 1)


T1_CONSEC_MISS_THRESHOLD = 3
_CONSEC_INVALID_RE = re.compile(r"连掉\s*(\d+)\s*步")


def is_severe_freshness_invalid(reason: str, consec_miss: int = 0) -> bool:
    """Return True only when freshness invalidation reaches T1 severity (consec_miss >= 3)."""
    if int(consec_miss or 0) > 0:
        return int(consec_miss) >= T1_CONSEC_MISS_THRESHOLD
    m = _CONSEC_INVALID_RE.search(reason or "")
    if m:
        try:
            return int(m.group(1)) >= T1_CONSEC_MISS_THRESHOLD
        except ValueError:
            return False
    return False


def _pick_preferred_deg_at(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """Pick the most informative degradation step load: prefer smallest high-load (>=400), else smallest cold-start."""
    vals = [int(v) for v in (a, b) if v is not None and int(v) > 0]
    if not vals:
        return None
    high = [v for v in vals if v >= 400]
    return min(high) if high else min(vals)


def compute_settled_rolling_24h_records(
    hourly_buckets: Dict[int, Dict[str, Any]],
    now_ts: float,
) -> List[Dict[str, Any]]:
    """Aggregate strictly settled rolling-24h windows into daily-keyed records for adaptive limit calculation.

    Guarantees:
    - Zero overlap with the live 24h window ``(now_ts - 86400, now_ts]``: every inspected window
      ends at ``end_h <= floor((now_ts - 86400) / 3600) - 1`` (so ``(end_h + 1) * 3600 <= now_ts - 86400``),
      eliminating midnight step-cliff jumps and half-hour timezone leakage.
    - Bounded O(360) horizon (``max_settled_h - 360``) so sparse ancient buckets never cause CPU spikes.
    - Same-day post-degradation contamination guard (including unsettled later-same-day degradation
      lookahead so a day that already degraded later never briefly triggers ``adaptive_up``).
    - Non-empty day guard: a calendar day with 0 own steps never produces a phantom recovery record.
    """
    if not hourly_buckets:
        return []
    max_settled_h = int(math.floor((float(now_ts) - 86400.0) / 3600.0)) - 1
    min_horizon_h = max_settled_h - 360

    def _day_of_hour(h_epoch: int) -> str:
        # Use the closing second of the UTC hour bucket so half-hour timezones never bleed into the next day
        return datetime.datetime.fromtimestamp((h_epoch + 1) * 3600 - 1).strftime("%Y-%m-%d")

    settled_map: Dict[int, Dict[str, Any]] = {}
    unsettled_degraded_days: set[str] = set()
    unsettled_deg_hours: List[int] = []
    for raw_h, rec in hourly_buckets.items():
        try:
            hk = int(raw_h)
        except (TypeError, ValueError):
            continue
        if hk < min_horizon_h or not isinstance(rec, dict):
            continue
        deg_v = bool(rec.get("degraded", False))
        if hk > max_settled_h:
            if deg_v:
                unsettled_degraded_days.add(_day_of_hour(hk))
                unsettled_deg_hours.append(hk)
            continue
        steps_v = max(0, int(rec.get("steps", 0) or 0))
        deg_at_raw = rec.get("degraded_24h_at")
        deg_at_v = int(deg_at_raw) if (deg_at_raw is not None and not isinstance(deg_at_raw, bool) and int(deg_at_raw) > 0) else None
        if steps_v == 0 and not deg_v:
            continue
        settled_map[hk] = {"steps": steps_v, "degraded": deg_v, "degraded_24h_at": deg_at_v}

    if not settled_map:
        return []

    def _touches_unsettled_deg(h_val: int) -> bool:
        return any(u_hk - 23 <= h_val <= u_hk for u_hk in unsettled_deg_hours)

    day_own_steps: Dict[str, int] = {}
    day_first_deg_h: Dict[str, int] = {}
    day_last_deg_h: Dict[str, int] = {}
    for hk, rec in settled_map.items():
        d_str = _day_of_hour(hk)
        day_own_steps[d_str] = day_own_steps.get(d_str, 0) + rec["steps"]
        if rec["degraded"]:
            prev_h = day_first_deg_h.get(d_str)
            if prev_h is None or hk < prev_h:
                day_first_deg_h[d_str] = hk
            last_h = day_last_deg_h.get(d_str)
            if last_h is None or hk > last_h:
                day_last_deg_h[d_str] = hk

    day_post_deg_hours: Dict[str, int] = {}
    day_post_deg_steps: Dict[str, int] = {}
    day_post_deg_touched_unsettled: set[str] = set()
    for hk, rec in settled_map.items():
        d_str = _day_of_hour(hk)
        last_deg = day_last_deg_h.get(d_str)
        if last_deg is None or d_str in unsettled_degraded_days:
            continue
        if hk > last_deg and (not rec["degraded"]) and rec["steps"] > 0:
            if _touches_unsettled_deg(hk):
                day_post_deg_touched_unsettled.add(d_str)
            day_post_deg_hours[d_str] = day_post_deg_hours.get(d_str, 0) + 1
            day_post_deg_steps[d_str] = day_post_deg_steps.get(d_str, 0) + rec["steps"]

    min_h = min(settled_map.keys())
    max_h = max(settled_map.keys())
    scan_end = min(max_h + 23, max_settled_h)

    contaminated_hours: set[int] = set()
    for h in range(min_h, scan_end + 1):
        d_str = _day_of_hour(h)
        first_deg = day_first_deg_h.get(d_str)
        if first_deg is not None and h >= first_deg:
            contaminated_hours.add(h)

    by_day: Dict[str, Dict[str, Any]] = {}
    win_steps = 0
    bad_in_win = 0
    for end_h in range(min_h, scan_end + 1):
        enter_rec = settled_map.get(end_h)
        if enter_rec is not None:
            win_steps += enter_rec["steps"]
        if end_h in contaminated_hours or (enter_rec is not None and enter_rec["degraded"]):
            bad_in_win += 1

        leave_h = end_h - 24
        if leave_h >= min_h:
            leave_rec = settled_map.get(leave_h)
            if leave_rec is not None:
                win_steps -= leave_rec["steps"]
            if leave_h in contaminated_hours or (leave_rec is not None and leave_rec["degraded"]):
                bad_in_win -= 1

        d_str = _day_of_hour(end_h)
        # Check degradation BEFORE win_steps <= 0 guard so 0-step degraded buckets with explicit degraded_24h_at are never dropped!
        if enter_rec is not None and enter_rec["degraded"]:
            deg_load = enter_rec["degraded_24h_at"] if enter_rec["degraded_24h_at"] is not None else win_steps
            if deg_load is not None and deg_load > 0:
                slot = by_day.setdefault(d_str, {"date": d_str, "safe_steps": 0, "degraded_at": None})
                slot["degraded_at"] = _pick_preferred_deg_at(slot["degraded_at"], deg_load)
            continue

        if win_steps <= 0:
            continue

        if (
            bad_in_win == 0
            and day_own_steps.get(d_str, 0) > 0
            and d_str not in unsettled_degraded_days
            and not _touches_unsettled_deg(end_h)
        ):
            slot = by_day.setdefault(d_str, {"date": d_str, "safe_steps": 0, "degraded_at": None})
            if win_steps > slot["safe_steps"]:
                slot["safe_steps"] = win_steps

    out: List[Dict[str, Any]] = []
    for d_str in sorted(by_day.keys()):
        rec = by_day[d_str]
        deg_at = rec["degraded_at"]
        safe_s = int(rec["safe_steps"])
        if deg_at is not None:
            safe_s = min(safe_s, max(0, deg_at - 1))
        entry: Dict[str, Any] = {"date": d_str, "safe_steps": safe_s, "degraded_at": deg_at}
        if (
            deg_at is not None
            and d_str not in unsettled_degraded_days
            and d_str not in day_post_deg_touched_unsettled
        ):
            post_h_cnt = day_post_deg_hours.get(d_str, 0)
            post_steps = day_post_deg_steps.get(d_str, 0)
            if post_h_cnt >= 3 and post_steps >= max(1200, int((deg_at or 0) * 1.5)):
                entry["post_deg_safe_steps"] = post_steps
        out.append(entry)
    return out


def compute_adaptive_daily_steps_limits(
    base_warn: int,
    base_alert: int,
    history_days: List[Dict[str, Any]],
    *,
    recent_recovery_steps: int = 0,
) -> Tuple[int, int, str, str, int]:
    """Adapt per-account daily step protection thresholds from strictly completed historical days.

    Uses ``base_alert`` (default 1200) / ``base_warn`` (default 960) as the initial baseline:
    - Never chases today's live step count (only completed days strictly earlier than today qualify).
    - Scales up smoothly (+15% rounded up to nearest 100, capped at max(3x base, 5000)) when an
      account has proven healthy full-day capacity above ``base_alert`` (including settled same-day
      post-degradation sustained healthy recovery ``post_deg_safe_steps``).
    - Tightens conservatively (85% of first high-volume degradation step >=400, floor 600) if an
      account degraded after running high volume and has not subsequently surpassed it healthily;
      when ``recent_recovery_steps`` proves sustained recent healthy throughput >= max(base_alert,
      2 * latest_high_deg_steps), lifts stale tightening back to ``default`` (never chasing ``adaptive_up`` early).
    Returns ``(eff_warn, eff_alert, mode, note, hist_safe_peak)``.
    """
    b_alert = max(50, int(base_alert))
    b_warn = max(1, min(b_alert - 1, int(base_warn)))
    warn_ratio = min(0.95, max(0.50, float(b_warn) / float(b_alert)))

    hist_safe_peak = 0
    latest_high_deg_date = ""
    latest_high_deg_steps: Optional[int] = None
    ordered = sorted(
        (d for d in (history_days or []) if isinstance(d, dict) and d.get("date")),
        key=lambda d: str(d.get("date", "")),
    )
    for rec in ordered:
        d_str = str(rec.get("date", ""))
        safe_s = max(0, int(rec.get("safe_steps", 0) or 0))
        post_safe = max(0, int(rec.get("post_deg_safe_steps", 0) or 0))
        deg_raw = rec.get("degraded_at")
        deg_at = max(1, int(deg_raw)) if deg_raw is not None else None
        if deg_at is None:
            if safe_s > hist_safe_peak:
                hist_safe_peak = safe_s
            if latest_high_deg_steps is not None and d_str > latest_high_deg_date and safe_s >= latest_high_deg_steps:
                latest_high_deg_steps = None
                latest_high_deg_date = ""
        else:
            if deg_at >= 400:
                latest_high_deg_date = d_str
                latest_high_deg_steps = deg_at
            if post_safe >= max(b_alert, deg_at):
                if post_safe > hist_safe_peak:
                    hist_safe_peak = post_safe
                if (
                    latest_high_deg_steps is not None
                    and d_str >= latest_high_deg_date
                    and post_safe >= latest_high_deg_steps
                ):
                    latest_high_deg_steps = None
                    latest_high_deg_date = ""

    if hist_safe_peak > b_alert and latest_high_deg_steps is None:
        raw_target = int(math.ceil((hist_safe_peak * 1.15) / 100.0) * 100)
        eff_alert = min(max(b_alert * 3, 5000), max(b_alert, raw_target))
        eff_warn = max(1, min(eff_alert - 1, int(round((eff_alert * warn_ratio) / 10.0) * 10)))
        return eff_warn, eff_alert, "adaptive_up", f"按历史安全峰值 {hist_safe_peak} 步自适应上调", hist_safe_peak

    if latest_high_deg_steps is not None and latest_high_deg_steps < b_alert:
        eff_alert = max(600, min(b_alert, int(math.floor((latest_high_deg_steps * 0.85) / 50.0) * 50)))
        if eff_alert < b_alert:
            recov_s = max(0, int(recent_recovery_steps or 0))
            if recov_s >= max(b_alert, latest_high_deg_steps * 2):
                return (
                    b_warn,
                    b_alert,
                    "default",
                    f"近期已连续健康跑过 {recov_s} 步，已解除历史 {latest_high_deg_steps} 步旧收紧（待退出24h窗后自动校准）",
                    hist_safe_peak,
                )
            eff_warn = max(1, min(eff_alert - 1, int(round((eff_alert * warn_ratio) / 10.0) * 10)))
            return eff_warn, eff_alert, "adaptive_down", f"按历史 {latest_high_deg_steps} 步曾降档保守收紧", hist_safe_peak

    return b_warn, b_alert, "default", "初始建议值", hist_safe_peak
TOS_403_RE = re.compile(
    r"(?:"
    r"tos_violation"
    r"|account[\s_]+(?:suspended|disabled|banned)"
    r"|permission_denied[^a-z0-9]{0,48}(?:code\s*[:=]?\s*)?403\b"
    r"|(?:status|http|code)[^0-9]{0,16}403\b.{0,48}permission_denied"
    r"|(?:streamgeneratecontent|generatecontent)\b.{0,80}(?:permission_denied|(?:http(?:\s+status)?|status(?:\s+code)?|error\s+code|code)\s*[:=]?\s*403\b|\b403\s+forbidden\b)"
    r")",
    re.IGNORECASE,
)
IGNORED_LOG_SOURCES = (
    "extract_searchable_content.go",
    "llm_monitor.go",
    "runner.go",
    "code_assist_client.go",
    "conversationsearch",
    "telemetry.go",
)

# Non-inference background RPCs (analytics, telemetry, experiments, profile, metadata) must never trigger model 503/T2 alerts
NON_INFERENCE_RPC_MARKERS = (
    "recordtrajectoryanalytics",
    "recording trajectory segment analytics",
    "listexperiments",
    "fetchconfig",
    "loadcodeassist",
    "fetchavailablemodels",
    "retrieveuserquota",
    "recordsidecarevent",
    "counttokens",
    "clearcut",
    "peopleinfo",
)

# Narrow cloud-sync poll 403 markers on poll.go (missing optional aicode OAuth scope on older tokens, NOT account TOS ban)
NON_INFERENCE_403_POLL_MARKERS = (
    "cloud projects poll",
    "cloud summaries poll",
    "insufficient authentication scopes",
)
# Hard ban / real inference 403 markers that MUST NEVER be muted by poll.go / errorreport.go 403 filters
_HARD_403_RE = re.compile(
    r"(?:tos_violation|account[\s_]+(?:suspended|disabled|banned)|streamgeneratecontent|generatecontent)",
    re.I,
)

# Static offline fallback for Antigravity protobuf model_enum values (merged with live GetUserStatus clientModelConfigs)
MODEL_ENUM_FALLBACK_MAP: Dict[str, str] = {
    "MODEL_PLACEHOLDER_M535": "Gemini 4 Neon (XHigh)",
    "MODEL_PLACEHOLDER_M532": "Gemini 4 Neon (High)",
    "MODEL_PLACEHOLDER_M533": "Gemini 4 Neon (Medium)",
    "MODEL_PLACEHOLDER_M534": "Gemini 4 Neon (Low)",
    "MODEL_PLACEHOLDER_M427": "Gemini 4 Carbon (High)",
    "MODEL_PLACEHOLDER_M437": "Gemini 4 Carbon (Medium)",
    "MODEL_PLACEHOLDER_M438": "Gemini 4 Carbon (Low)",
    "MODEL_PLACEHOLDER_M417": "Gemini 4 Argon (High)",
    "MODEL_PLACEHOLDER_M418": "Gemini 4 Argon (Medium)",
    "MODEL_PLACEHOLDER_M419": "Gemini 4 Argon (Low)",
    "MODEL_PLACEHOLDER_M528": "Gemini 4 Hydrogen (XHigh)",
    "MODEL_PLACEHOLDER_M525": "Gemini 4 Hydrogen (High)",
    "MODEL_PLACEHOLDER_M530": "Gemini 4 Hydrogen (High)",
    "MODEL_PLACEHOLDER_M526": "Gemini 4 Hydrogen (Medium)",
    "MODEL_PLACEHOLDER_M527": "Gemini 4 Hydrogen (Low)",
    "MODEL_PLACEHOLDER_M537": "Orchestrator (Neon)",
    "MODEL_PLACEHOLDER_M667": "Orchestrator (Carbon)",
    "MODEL_PLACEHOLDER_M681": "Orchestrator (Argon)",
    "MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High)",
    "MODEL_PLACEHOLDER_M319": "Gemini 3.8 Flash (Medium)",
    "MODEL_PLACEHOLDER_M320": "Gemini 3.8 Flash (Low)",
    "MODEL_PLACEHOLDER_M26": "Claude Opus 4.6 (Thinking)",
    "MODEL_PLACEHOLDER_M35": "Claude Sonnet 4.6 (Thinking)",
    "MODEL_PLACEHOLDER_M402": "Claude Opus 5.5 (High)",
    "MODEL_OPENAI_GPT_OSS_120B_MEDIUM": "GPT-OSS 120B (Medium)",
}

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
    ("gemini-4-hydrogen-medium", "Hydrogen M"),
    ("gemini-4-hydrogen-low", "Hydrogen L"),
    ("gemini-4-hydrogen", "Hydrogen"),
    ("gemini-4-argon-high", "Argon High"),
    ("gemini-4-argon-medium", "Argon Med"),
    ("gemini-4-argon-low", "Argon Low"),
    ("gemini-4-argon", "Argon"),
    ("gemini-3-8-flash-high", "3.8 Flash H"),
    ("gemini-3-8-flash-medium", "3.8 Flash M"),
    ("gemini-3-8-flash-low", "3.8 Flash L"),
    ("gemini-3-8-flash", "3.8 Flash"),
    ("orchestrator-neon", "Orch Neon"),
    ("orchestrator-carbon", "Orch Carbon"),
    ("orchestrator-argon", "Orch Argon"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-opus-4-6", "Opus 4.6"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-fable", "Fable"),
    ("gpt-oss-120b", "GPT-OSS 120B"),
)

CACHE_REASON_CN_MAP: Dict[str, str] = {
    "cached": "命中",
    "cached_diluted": "命中·新输入稀释",
    "compact_reset": "压缩重热",
    "revert_reset": "回溯重热",
    "first_turn": "首轮冷启",
    "account_switch": "换号待启",
    "idle_rewarm": "闲置重热",
    "model_switch": "切模冷启",
    "small_ctx": "<32K免缓",
    "first_cacheable": "入32K首建",
    "rule_change": "改规冷启",
    "abnormal_zero": "异常掉缓存",
    "partial_miss": "部分失效",
}

COLD_SHORT_BADGE_MAP: Dict[str, str] = {
    "small_ctx": "<32K",
    "first_cacheable": "首建缓",
    "account_switch": "切号冷",
    "model_switch": "切模冷",
    "compact_reset": "压缩冷",
    "revert_reset": "回溯冷",
    "first_turn": "首轮冷",
    "idle_rewarm": "闲置冷",
    "rule_change": "规则冷",
    "partial_miss": "部分失",
}


def resolve_cache_visual_tier(
    classification: str,
    reason: str = "",
    *,
    is_compact: bool = False,
    is_revert: bool = False,
) -> str:
    """Return canonical visual color tier: 'red' | 'purple' | 'orange' | 'green'."""
    if classification == "abnormal_miss":
        return "red"
    if (is_compact or reason == "compact_reset") and not is_revert:
        return "purple"
    if is_revert or classification == "expected_cold":
        return "orange"
    return "green"


def _format_freshness_invalid_reason(consec_miss: int, latest_reason: str = "") -> str:
    """Format freshness invalidation banner, distinguishing partial_miss from pure cr=0."""
    if consec_miss >= 2:
        return f"无热缓存 (连掉 {consec_miss} 步)"
    if latest_reason == "partial_miss":
        return "本步部分掉缓存"
    return "本步 cr=0 无热缓存"

_NORM_MODEL_RE = re.compile(r"[^a-z0-9]+")


def format_media_size(num_bytes: int) -> str:
    b = max(0, int(num_bytes or 0))
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f}M"
    if b >= 1024:
        return f"{b / 1024:.0f}K"
    return f"{b}B"


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


def resolve_model_label(raw: str, enum_map: Optional[Dict[str, str]] = None) -> str:
    """Resolve protobuf model_enum (e.g. MODEL_PLACEHOLDER_M535) to a readable label if known."""
    clean = (raw or "").strip()
    if not clean:
        return ""
    up = clean.upper()
    if enum_map and up in enum_map:
        return enum_map[up]
    if up in MODEL_ENUM_FALLBACK_MAP:
        return MODEL_ENUM_FALLBACK_MAP[up]
    return clean


def format_model_name(raw: str, enum_map: Optional[Dict[str, str]] = None) -> str:
    resolved = resolve_model_label(raw, enum_map=enum_map)
    if not resolved:
        return "未知模型"
    low = resolved.strip().lower()
    norm = _NORM_MODEL_RE.sub("-", low).strip("-")
    for prefix, short in MODEL_SHORT_MAP:
        if prefix in low or prefix in norm:
            return short
    if len(resolved) > 18:
        return resolved[:17] + "…"
    return resolved


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


APP_ROOT = Path(__file__).resolve().parent.parent
_DIAG_LOG_MAX_BYTES = 64 * 1024


def _build_localhost_opener() -> urllib.request.OpenerDirector:
    """Build a minimal OpenerDirector pinned to direct localhost HTTP (zero proxy, no redirects/file/ftp)."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.ProxyHandler({}))
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    return opener


_LOCALHOST_OPENER: urllib.request.OpenerDirector = _build_localhost_opener()


def _open_localhost_url(req: urllib.request.Request, timeout: float):
    """Open an HTTP request strictly targeting localhost/127.0.0.1, bypassing all proxies & redirects."""
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme != "http" or host not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError(f"Refused non-localhost HTTP request: scheme={scheme!r}, host={host!r}")
    resp = _LOCALHOST_OPENER.open(req, timeout=timeout)
    if resp is None:
        raise urllib.error.URLError("Localhost opener returned no response")
    return resp


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
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
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


def _inspect_windows_pid(pid: int) -> Tuple[bool, str, int]:
    """Single-handle zero-subprocess Win32 inspection: returns (alive, exe_basename_lower, creation_ft)."""
    if pid <= 0:
        return False, "", 0
    if os.name != "nt":
        return _is_windows_pid_alive(pid), "", 0
    try:
        kernel32 = _get_kernel32()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False, "", 0
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                return False, "", 0
            ct = wintypes.FILETIME()
            et = wintypes.FILETIME()
            kt = wintypes.FILETIME()
            ut = wintypes.FILETIME()
            creation_ft = 0
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(ct),
                ctypes.byref(et),
                ctypes.byref(kt),
                ctypes.byref(ut),
            ):
                creation_ft = (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)
            exe_name = ""
            buf_len = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                exe_name = os.path.basename(buf.value).lower()
            return True, exe_name, creation_ft
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False, "", 0


def _get_windows_pid_creation_ft(pid: int) -> int:
    """Return Win32 process creation FILETIME (64-bit int) or 0 if unavailable/non-Windows."""
    if os.name != "nt" or pid <= 0:
        return 0
    alive, _exe, cft = _inspect_windows_pid(pid)
    return cft if alive else 0


def _is_hub_language_server_pid(
    pid: int,
    expected_active_pid: int = 0,
    expected_creation_ft: int = 0,
) -> bool:
    """Verify an alive Hub PID is genuinely Antigravity's language_server (immune to Windows PID reuse).

    Uses OpenProcess(0x1000) + QueryFullProcessImageNameW + GetProcessTimes on a single handle.
    On non-Windows or when image name cannot be queried, conservatively allows only expected_active_pid
    (with matching creation_ft when known).
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        if not _is_windows_pid_alive(pid):
            return False
        return bool(expected_active_pid > 0 and pid == expected_active_pid)
    alive, exe_name, creation_ft = _inspect_windows_pid(pid)
    if not alive:
        return False
    if exe_name and ("language_server" in exe_name or exe_name in ("antigravity.exe", "antigravity")):
        return True
    if expected_active_pid > 0 and pid == expected_active_pid:
        if expected_creation_ft > 0:
            return creation_ft > 0 and creation_ft == expected_creation_ft
        return not exe_name
    return False


class _RawStepSlot(Mapping):
    """Memory-compact slotted Mapping replacing 10-key raw step dicts (~112B vs ~272B)."""

    __slots__ = (
        "step_index",
        "timestamp",
        "model",
        "input_tokens",
        "cache_read_tokens",
        "output_tokens",
        "after_checkpoint",
        "after_revert",
        "active_media_count",
        "active_media_bytes",
        "is_error_retry",
    )
    _KEYS = (
        "step_index",
        "timestamp",
        "model",
        "input_tokens",
        "cache_read_tokens",
        "output_tokens",
        "after_checkpoint",
        "after_revert",
        "active_media_count",
        "active_media_bytes",
    )
    _KEY_SET = frozenset(_KEYS)

    def __init__(
        self,
        step_index: Optional[int] = None,
        timestamp: float = 0.0,
        model: str = "",
        input_tokens: int = 0,
        cache_read_tokens: int = 0,
        output_tokens: int = 0,
        after_checkpoint: bool = False,
        after_revert: bool = False,
        active_media_count: int = 0,
        active_media_bytes: int = 0,
        is_error_retry: bool = False,
    ) -> None:
        self.step_index = step_index
        self.timestamp = timestamp
        self.model = model
        self.input_tokens = input_tokens
        self.cache_read_tokens = cache_read_tokens
        self.output_tokens = output_tokens
        self.after_checkpoint = after_checkpoint
        self.after_revert = after_revert
        self.active_media_count = active_media_count
        self.active_media_bytes = active_media_bytes
        self.is_error_retry = bool(is_error_retry)

    def __getitem__(self, key: str) -> Any:
        if key in self._KEY_SET:
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._KEY_SET:
            return getattr(self, key)
        return default

    def __contains__(self, key: object) -> bool:
        return key in self._KEY_SET

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _RawStepSlot):
            return (
                self.step_index == other.step_index
                and self.timestamp == other.timestamp
                and self.model == other.model
                and self.input_tokens == other.input_tokens
                and self.cache_read_tokens == other.cache_read_tokens
                and self.output_tokens == other.output_tokens
                and self.after_checkpoint == other.after_checkpoint
                and self.after_revert == other.after_revert
                and self.active_media_count == other.active_media_count
                and self.active_media_bytes == other.active_media_bytes
            )
        if isinstance(other, Mapping):
            if len(other) != len(self._KEYS):
                return False
            for k in self._KEYS:
                if k not in other or other[k] != getattr(self, k):
                    return False
            return True
        return NotImplemented


@dataclass(frozen=True, **_DATACLASS_SLOTS)
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
    reason: str          # "cached" | "cached_diluted" | "first_turn" | "idle_rewarm" | "model_switch" | "compact_reset" | "revert_reset" | "small_ctx" | "first_cacheable" | "rule_change" | "abnormal_zero" | "partial_miss"
    context_delta: int = 0
    is_revert: bool = False
    is_compact: bool = False
    is_account_switch: bool = False
    active_media_count: int = 0
    active_media_bytes: int = 0


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
    latest_cache_reason: str = ""
    active_media_count: int = 0
    active_media_bytes: int = 0
    media_ttft_exempted: bool = False
    is_carryover_preview: bool = False
    large_ctx_ttft_allowance_sec: float = 0.0
    tail_consec_hits_after_abnormal: int = 0
    last_abnormal_ts: float = 0.0
    _scoped_steps_all: List[StepRecord] = field(default_factory=list, repr=False, compare=False)
    _scoped_gens_all: List[Optional[Any]] = field(default_factory=list, repr=False, compare=False)


@dataclass(frozen=True, **_DATACLASS_SLOTS)
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
    is_account_switch: bool = False
    active_media_count: int = 0
    active_media_bytes: int = 0


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
    consec_miss: int = 0
    cacheable_token_hit_ratio: Optional[float] = None


@dataclass(frozen=True, **_DATACLASS_SLOTS)
class LogEvent:
    timestamp: float
    category: str   # "503" | "timeout" | "403" | "retry"
    summary: str
    event_key: str
    model: str = ""
    duration_sec: Optional[float] = None
    is_heavy_media_timeout: bool = False
    media_count: int = 0
    media_bytes: int = 0
    culprit_cid: str = ""


@dataclass(frozen=True, **_DATACLASS_SLOTS)
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
    main_adj_ttft_after_last_t2_median: Optional[float] = None
    main_adj_ttft_after_last_t2_latest: Optional[float] = None
    main_adj_ttft_after_last_t2_recent2_max: Optional[float] = None
    main_media_exempt_post_steps: int = 0
    main_unexempt_ttft_steps_after_last_t2: int = 0
    main_large_ctx_calibrated: bool = False


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
    primary_hit_reason: str = ""
    carryover_conversations: List[ConversationSummary] = field(default_factory=list)
    heavy_media_timeout_recent: int = 0
    proxy_outage_recent: int = 0
    proxy_outage_note: str = ""
    daily_steps_limit_mode: str = "default"
    daily_steps_limit_note: str = ""
    daily_steps_base_alert: int = 1200
    account_hist_safe_peak: int = 0
    rolling_24h_steps: Optional[int] = None
    account_24h_steps: Optional[int] = None
    total_24h_steps: Optional[int] = None
    main_24h_steps: int = 0
    subagent_24h_steps: int = 0
    total_main_24h_steps: int = 0
    total_subagent_24h_steps: int = 0
    peak_hour_steps_24h: int = 0
    peak_hour_label_24h: str = ""
    current_24h_has_degradation: bool = False
    hub_pid_alive: bool = False
    primary_hit_is_compact: bool = False
    primary_hit_is_revert: bool = False
    cache_culprit_summary: str = ""
    log_session_start_ts: float = 0.0


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
    rolling_24h_steps: Optional[int] = None,
) -> str:
    """Map raw monitor state into a clean 5-tier pressure code for RULES automation."""
    if grade == "IDLE":
        return "offline_unknown"
    if grade in ("T1", "T2", "T3"):
        return "degraded"
    eff_steps = int(rolling_24h_steps) if rolling_24h_steps is not None else int(today_steps)
    if eff_steps >= daily_steps_alert:
        return "red"
    if grade == "WARN" or eff_steps >= daily_steps_warn:
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
        "ledger_path": APP_ROOT / "account_ledger.json",
        "diag_log_path": APP_ROOT / "logs" / "monitor_diag.log",
        "vscdb_path": appdata / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
        "global_rules_path": home / ".gemini" / "GEMINI.md",
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
    raw_steps: List[Any],
    idle_rewarm_sec: int = 180,
    min_cacheable_tokens: int = 32000,
    account_switches: Optional[List[float]] = None,
    rule_change_timestamps: Optional[List[float]] = None,
) -> List[StepRecord]:
    """Classify consecutive PLANNER_RESPONSE steps, filtering normal zero/partial-cache causes."""
    classified: List[StepRecord] = []
    prev_ts: Optional[float] = None
    prev_model: Optional[str] = None
    prev_total: Optional[int] = None
    prev_step_idx: Optional[int] = None
    prev_cls: str = ""
    prev_reason: str = ""
    has_prior_cache: bool = False
    has_prior_compact: bool = False
    first_non_ooo_ts: Optional[float] = None
    consumed_rule_watermark: float = 0.0
    non_ooo_hit_ts: List[float] = []
    switches = sorted(account_switches) if account_switches else []
    rule_ts = (
        sorted(
            float(t)
            for t in rule_change_timestamps
            if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(float(t)) and float(t) > 0.0
        )
        if rule_change_timestamps
        else []
    )

    for idx, item in enumerate(raw_steps):
        if isinstance(item, _RawStepSlot):
            inp = max(0, int(item.input_tokens or 0))
            cr = max(0, int(item.cache_read_tokens or 0))
            out = max(0, int(item.output_tokens or 0))
            ts = float(item.timestamp or 0.0)
            model = str(item.model or "").strip()
            raw_si = item.step_index
            after_revert = bool(item.after_revert)
            after_checkpoint = bool(item.after_checkpoint)
            media_cnt = int(item.active_media_count or 0)
            media_bytes = int(item.active_media_bytes or 0)
            is_error_retry = bool(item.is_error_retry)
        else:
            inp = max(0, int(item.get("input_tokens", 0) or 0))
            cr = max(0, int(item.get("cache_read_tokens", 0) or 0))
            out = max(0, int(item.get("output_tokens", 0) or 0))
            ts = float(item.get("timestamp", 0.0) or 0.0)
            model = str(item.get("model", "") or "").strip()
            raw_si = item.get("step_index")
            after_revert = bool(item.get("after_revert", False))
            after_checkpoint = bool(item.get("after_checkpoint", False))
            media_cnt = int(item.get("active_media_count", 0) or 0)
            media_bytes = int(item.get("active_media_bytes", 0) or 0)
            is_error_retry = bool(item.get("is_error_retry", False))

        total = inp + cr
        if total <= 0:
            continue
        step_idx = int(raw_si) if raw_si is not None else idx
        hit_rate = (cr / total) * 100.0

        # Identify delayed out-of-order steps BEFORE classification so they never trigger rule_change or pollute state
        is_delayed_ooo = prev_ts is not None and ts > 0.0 and prev_ts > 0.0 and ts < prev_ts - 5.0

        crossed_switch = False
        if prev_ts is not None and ts > prev_ts and switches:
            for sw_ts in switches:
                if prev_ts < sw_ts <= ts + 2.0:
                    crossed_switch = True
                    break

        latest_t_rule: Optional[float] = None
        if rule_ts and ts > 0.0:
            r_idx = bisect.bisect_right(rule_ts, ts + 2.0)
            if r_idx > 0:
                latest_t_rule = rule_ts[r_idx - 1]

        is_ckpt_shrink = (
            (not after_revert)
            and after_checkpoint
            and prev_total is not None
            and prev_total >= min_cacheable_tokens
            and total < prev_total * 0.85
        )
        if (
            not after_revert
            and not is_ckpt_shrink
            and prev_step_idx is not None
            and raw_si is not None
            and step_idx <= prev_step_idx
            and (prev_ts is None or ts <= 0.0 or prev_ts <= 0.0 or ts >= prev_ts - 5.0)
        ):
            # Stream-error auto-retry on the exact same step_index (explicit ERROR_MESSAGE retry or tight <=2% cr>0 match)
            is_same_step_retry = is_error_retry or (
                step_idx == prev_step_idx
                and cr > 0
                and prev_total is not None
                and prev_total > 0
                and abs(total - prev_total) <= max(2048, int(prev_total * 0.02))
            )
            if not is_same_step_retry:
                after_revert = True

        is_real_compact = (
            not after_revert
            and prev_total is not None
            and prev_total >= min_cacheable_tokens
            and ((after_checkpoint and total < prev_total * 0.85) or (total < prev_total * 0.75))
        )
        is_expected_partial = (
            cr > 0
            and prev_total is not None
            and prev_total >= min_cacheable_tokens
            and cr < prev_total * 0.50
            and hit_rate < 50.0
        )
        is_expected_cold_candidate = (cr == 0) or is_expected_partial
        is_idle_rewarm = prev_ts is not None and ts > 0 and prev_ts > 0 and (ts - prev_ts) >= idle_rewarm_sec
        is_model_switch = bool(prev_model and model and model.lower() != prev_model.lower())
        is_rule_change_eligible = (
            not is_delayed_ooo
            and latest_t_rule is not None
            and latest_t_rule > consumed_rule_watermark
            and ts <= latest_t_rule + 600.0
            and first_non_ooo_ts is not None
            and first_non_ooo_ts < latest_t_rule
            and prev_cls != "abnormal_miss"
            and prev_reason != "rule_change"
            and prev_ts is not None
            and prev_ts <= latest_t_rule + 90.0
            and (len(non_ooo_hit_ts) - bisect.bisect_left(non_ooo_hit_ts, latest_t_rule)) <= 1
        )

        if prev_ts is None:
            if cr > 0:
                cls, reason = "hit", "cached"
            else:
                cls, reason = "expected_cold", "first_turn"
        elif crossed_switch and is_expected_cold_candidate:
            cls, reason = "expected_cold", "account_switch"
        elif after_revert and is_expected_cold_candidate:
            cls, reason = "expected_cold", "revert_reset"
        elif is_real_compact:
            if cr == 0 or hit_rate < 75.0:
                cls, reason = "expected_cold", "compact_reset"
            else:
                cls, reason = "hit", "cached"
        elif is_expected_cold_candidate and is_idle_rewarm:
            cls, reason = "expected_cold", "idle_rewarm"
        elif is_expected_cold_candidate and is_model_switch:
            cls, reason = "expected_cold", "model_switch"
        elif cr == 0 and total < min_cacheable_tokens:
            cls, reason = "expected_cold", "small_ctx"
        elif (
            cr == 0
            and prev_total is not None
            and prev_total < min_cacheable_tokens
            and (not has_prior_cache)
            and (not has_prior_compact)
        ):
            # First turn crossing >=32K implicit cache threshold (only before any prior cache or compaction)
            cls, reason = "expected_cold", "first_cacheable"
        elif is_expected_cold_candidate and is_rule_change_eligible:
            # Global GEMINI.md rule edit invalidates prompt prefix once per conversation; monotonic watermark blocks multi-step whitewashing
            cls, reason = "expected_cold", "rule_change"
        elif cr == 0:
            cls, reason = "abnormal_miss", "abnormal_zero"
        elif (
            prev_total is not None
            and prev_total >= 50000
            and cr < prev_total * 0.50
            and hit_rate < 50.0
        ):
            cls, reason = "abnormal_miss", "partial_miss"
        elif prev_total is not None and prev_total > 0 and cr >= prev_total * 0.85 and hit_rate < 70.0:
            cls, reason = "hit", "cached_diluted"
        else:
            cls, reason = "hit", "cached"

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
                is_account_switch=crossed_switch,
                active_media_count=media_cnt,
                active_media_bytes=media_bytes,
            )
        )
        # Do not let an out-of-order delayed historical step clobber the forward cursor or rule watermark
        if not is_delayed_ooo:
            if latest_t_rule is not None and latest_t_rule > consumed_rule_watermark:
                # Advance monotonic watermark on ANY expected_cold after t_rule (including <32K)
                # OR when an abnormal_miss was explicitly blocked from reusing t_rule by an ongoing miss/rule_change chain
                if cls == "expected_cold" or (
                    cls == "abnormal_miss" and (prev_cls == "abnormal_miss" or prev_reason == "rule_change")
                ):
                    consumed_rule_watermark = latest_t_rule
            if cls == "hit" and ts > 0.0:
                bisect.insort(non_ooo_hit_ts, ts)
            if cr > 0:
                has_prior_cache = True
            if is_real_compact or reason == "compact_reset":
                has_prior_compact = True
            if first_non_ooo_ts is None and ts > 0.0:
                first_non_ooo_ts = ts
            prev_ts = ts if (prev_ts is None or ts <= 0.0) else max(prev_ts, ts)
            prev_model = model
            prev_total = total
            prev_step_idx = step_idx
            prev_cls = cls
            prev_reason = reason

    return classified


# Fast O(1) lowercase substring pre-filter covering every necessary literal of TOS_403_RE,
# STREAM_TIMEOUT_RE, HTTP_503_RE, RETRY_RE, proxy-EOF capture, and non-inference error guards.
_LOG_FAST_KEYWORDS: Tuple[str, ...] = (
    "503",
    "429",
    "403",
    "timed out",
    "timeout",
    "attempt ",
    "eof",
    "broken pipe",
    "forcibly closed",
    "connection reset",
    "unavailable",
    "no capacity",
    "resource_exhausted",
    "permission_denied",
    "tos_violation",
    "suspended",
    "disabled",
    "banned",
    "error",
    "failed",
)


class LanguageServerLogTailer:
    """Incremental seek(offset) tailer with rotation, partial-line, and sliding-window guards."""

    def __init__(self, log_path: Path, max_events: int = 200) -> None:
        self.log_path = Path(log_path)
        self.max_events = max(20, int(max_events))
        self._offset: int = 0
        self._events: Deque[LogEvent] = deque(maxlen=self.max_events)
        self._initialized: bool = False
        self._last_seen_ts: Optional[float] = None
        self._last_seen_ts_token: Optional[Tuple[Any, ...]] = None
        self._cached_fallback_ts: float = -1.0
        self._cached_fallback_dt: Optional[datetime.datetime] = None
        # Bounded O(1) dedup & non-inference goroutine tracking (prevents errorreport.go double counting)
        self._ignored_goroutines: OrderedDict[str, float] = OrderedDict()
        self._ignored_non_inference_403_gids: OrderedDict[str, float] = OrderedDict()
        self._recent_error_sigs: OrderedDict[Tuple[str, str], Tuple[float, str, str]] = OrderedDict()
        self._last_event_by_cat: Dict[str, Tuple[float, str, str]] = {}
        self._last_header_gid: str = ""
        self._last_header_src: str = ""
        self._block_emitted_cats: set[str] = set()
        self._skip_continuation_lines: int = 0
        self._skip_brace_depth: int = 0
        # Bounded cross-endpoint proxy outage burst tracking (separated from notable events so single retry stays clean)
        self._proxy_eof_events: Deque[Tuple[float, str, str]] = deque(maxlen=256)
        self._proxy_eof_dedup: "OrderedDict[Tuple[str, str, int], float]" = OrderedDict()
        self._proxy_eof_gid_ts: "OrderedDict[str, float]" = OrderedDict()
        self._session_start_ts: float = 0.0

    def get_session_start_ts(self) -> float:
        """Return the earliest parsed header timestamp observed in the current log file (0.0 if none)."""
        return float(self._session_start_ts)

    @staticmethod
    def _classify_proxy_bucket(src_base: str, body_low: str) -> str:
        if (
            "streamgeneratecontent" in body_low
            or "generatecontent" in body_low
            or "stream reading error" in body_low
            or src_base in ("run.go", "retry.go")
        ):
            return "stream"
        if (
            "fetchuserinfo" in body_low
            or "retrieveuserquotasummary" in body_low
            or src_base == "cache.go"
        ):
            return "userinfo"
        if "play.googleapis.com" in body_low:
            return "playlog"
        if "recordtrajectoryanalytics" in body_low or src_base == "telemetry.go" or "clearcut" in body_low:
            return "telemetry"
        if (
            "listexperiments" in body_low
            or "fetchconfig" in body_low
            or "loadcodeassist" in body_low
            or "fetchavailablemodels" in body_low
        ):
            return "experiments"
        return "other_google"

    def _record_proxy_eof_if_matched(
        self,
        ts: float,
        goroutine_id: str,
        src_base: str,
        body: str,
        body_low: str,
    ) -> None:
        if src_base == "errorreport.go":
            return
        if not (
            "googleapis.com" in body_low
            or "stream reading error" in body_low
            or "streamgeneratecontent" in body_low
            or "fetchuserinfo" in body_low
        ):
            return
        if _PROXY_EOF_RE.search(body) is None:
            return
        bucket = self._classify_proxy_bucket(src_base, body_low)
        gid_tag = goroutine_id or src_base
        # Collapse singleflight fanout storms (e.g. cache.go:189 emitting 17 fetchUserInfo EOFs in the same millisecond)
        if bucket == "stream":
            dedup_key = ("stream", gid_tag, int(ts))
        else:
            dedup_key = (bucket, "", int(ts // 2))
        if dedup_key in self._proxy_eof_dedup:
            return
        if goroutine_id:
            prev_gid_ts = self._proxy_eof_gid_ts.get(goroutine_id)
            if prev_gid_ts is not None and abs(ts - prev_gid_ts) <= 2.0:
                return
        self._proxy_eof_dedup[dedup_key] = ts
        self._proxy_eof_dedup.move_to_end(dedup_key)
        while len(self._proxy_eof_dedup) > 128:
            self._proxy_eof_dedup.popitem(last=False)
        if goroutine_id:
            self._proxy_eof_gid_ts[goroutine_id] = ts
            self._proxy_eof_gid_ts.move_to_end(goroutine_id)
            while len(self._proxy_eof_gid_ts) > 128:
                self._proxy_eof_gid_ts.popitem(last=False)
        self._proxy_eof_events.append((ts, bucket, gid_tag))

    @staticmethod
    def _source_base_name(source: str) -> str:
        low = (source or "").strip().lower()
        return low.split(":", 1)[0]

    def _mark_ignored_goroutine(self, goroutine_id: str, ts: float) -> None:
        if not goroutine_id:
            return
        self._ignored_goroutines[goroutine_id] = ts
        self._ignored_goroutines.move_to_end(goroutine_id)
        while len(self._ignored_goroutines) > 64:
            self._ignored_goroutines.popitem(last=False)

    def _is_recent_ignored_goroutine(self, goroutine_id: str, ts: float, window_sec: float = 3.0) -> bool:
        if not goroutine_id:
            return False
        prev = self._ignored_goroutines.get(goroutine_id)
        if prev is None:
            return False
        return abs(ts - prev) <= window_sec

    def _mark_ignored_403_goroutine(self, goroutine_id: str, ts: float) -> None:
        if not goroutine_id:
            return
        self._ignored_non_inference_403_gids[goroutine_id] = ts
        self._ignored_non_inference_403_gids.move_to_end(goroutine_id)
        while len(self._ignored_non_inference_403_gids) > 64:
            self._ignored_non_inference_403_gids.popitem(last=False)

    def _is_recent_ignored_403_goroutine(self, goroutine_id: str, ts: float, window_sec: float = 3.0) -> bool:
        if not goroutine_id:
            return False
        prev = self._ignored_non_inference_403_gids.get(goroutine_id)
        if prev is None:
            return False
        return abs(ts - prev) <= window_sec

    def _mark_error_sig(self, goroutine_id: str, category: str, ts: float, source_base: str, attempt_num: str = "") -> None:
        if not goroutine_id:
            return
        key = (goroutine_id, category)
        self._recent_error_sigs[key] = (ts, source_base, attempt_num)
        self._recent_error_sigs.move_to_end(key)
        while len(self._recent_error_sigs) > 64:
            self._recent_error_sigs.popitem(last=False)

    def _is_cross_layer_echo(
        self,
        goroutine_id: str,
        category: str,
        ts: float,
        source_base: str,
        attempt_num: str = "",
        window_sec: float = 3.0,
    ) -> bool:
        """Suppress cross-layer duplicate echoes on the same goroutine (e.g. caller.go + errorreport.go)
        while preserving distinct retry attempts emitted repeatedly by the same source file (e.g. run.go attempt 1..4)."""
        if not goroutine_id:
            return False
        prev = self._recent_error_sigs.get((goroutine_id, category))
        if prev is None:
            return False
        prev_ts, prev_src, prev_attempt = prev
        if abs(ts - prev_ts) > window_sec:
            return False
        # Distinct explicit retry attempt number (e.g. attempt 1 -> attempt 2) is always a genuine new upstream try
        if attempt_num and prev_attempt and attempt_num != prev_attempt:
            return False
        # Cross-layer echo: errorreport.go echoing a prior non-errorreport source, or a caller echoing an earlier
        # errorreport/http_helpers log from a different source file on the exact same goroutine.
        if source_base == "errorreport.go" and prev_src and prev_src != "errorreport.go":
            return True
        if prev_src and source_base and prev_src != source_base:
            return True
        return False

    def poll(self, now_ts: Optional[float] = None) -> None:
        now = now_ts if now_ts is not None else time.time()
        if not self.log_path.is_file():
            self._offset = 0
            self._session_start_ts = 0.0
            self._initialized = True
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return

        if size < self._offset:
            self._offset = 0
            self._session_start_ts = 0.0
            self._events.clear()
            self._last_seen_ts = None
            self._last_seen_ts_token = None
            self._ignored_goroutines.clear()
            self._ignored_non_inference_403_gids.clear()
            self._recent_error_sigs.clear()
            self._last_event_by_cat.clear()
            self._last_header_gid = ""
            self._last_header_src = ""
            self._block_emitted_cats.clear()
            self._skip_continuation_lines = 0
            self._skip_brace_depth = 0
            self._proxy_eof_events.clear()
            self._proxy_eof_dedup.clear()
            self._proxy_eof_gid_ts.clear()

        if size == self._offset and self._initialized:
            return

        is_cold_start = not self._initialized
        discard_first_partial = False
        if is_cold_start and size > 2 * 1024 * 1024:
            self._offset = size - 2 * 1024 * 1024
            discard_first_partial = True

        try:
            with self.log_path.open("rb") as fh:
                if is_cold_start and discard_first_partial and self._session_start_ts <= 0.0:
                    head_bytes = fh.read(65536)
                    if head_bytes:
                        for h_line in head_bytes.decode("utf-8", errors="replace").splitlines():
                            if not h_line:
                                continue
                            h_ok, h_tok, h_eager, _sv, _gid, _src, _bd = self._split_header_meta_lazy(h_line, now)
                            if h_ok:
                                first_ts = (
                                    h_eager
                                    if h_eager is not None
                                    else (self._materialize_ts_token(h_tok, now) if h_tok is not None else None)
                                )
                                if first_ts is not None and first_ts > 0.0:
                                    self._session_start_ts = first_ts
                                    break
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

    def _materialize_ts_token(self, token: Tuple[Any, ...], fallback_ts: float) -> Optional[float]:
        kind = token[0]
        if kind == "glog":
            _k, mm, dd, h, mi, s, usec = token
            if self._cached_fallback_dt is None or self._cached_fallback_ts != fallback_ts:
                self._cached_fallback_dt = datetime.datetime.fromtimestamp(fallback_ts)
                self._cached_fallback_ts = fallback_ts
            now_dt = self._cached_fallback_dt
            try:
                cand = now_dt.replace(month=mm, day=dd, hour=h, minute=mi, second=s, microsecond=usec)
                ts_val = cand.timestamp()
                if ts_val > fallback_ts + 86400:
                    ts_val = cand.replace(year=now_dt.year - 1).timestamp()
                return ts_val
            except ValueError:
                return None
        if kind == "iso":
            return parse_iso_timestamp(token[1])
        return None

    def _resolve_effective_ts(self, fallback_ts: float, is_cold_start: bool) -> float:
        if self._last_seen_ts is None and self._last_seen_ts_token is not None:
            self._last_seen_ts = self._materialize_ts_token(self._last_seen_ts_token, fallback_ts)
            self._last_seen_ts_token = None
        if self._last_seen_ts is not None:
            return self._last_seen_ts
        return 0.0 if is_cold_start else fallback_ts

    def _split_header_meta_lazy(
        self,
        line: str,
        fallback_ts: float,
    ) -> Tuple[bool, Optional[Tuple[Any, ...]], Optional[float], str, str, str, str]:
        """Parse log header quickly, deferring datetime.replace().timestamp() until a line actually needs a timestamp."""
        c0 = line[0]
        if c0 in ("I", "W", "E", "F"):
            m_glog = GLOG_PREFIX_RE.match(line)
            if m_glog:
                sev = m_glog.group(1)
                mm, dd, hms = int(m_glog.group(2)), int(m_glog.group(3)), m_glog.group(4)
                frac = (m_glog.group(5) or "")[:6]
                usec = int(frac.ljust(6, "0")) if frac else 0
                goroutine_id, source, body = m_glog.group(6), m_glog.group(7), m_glog.group(8)
                h, mi, s = int(hms[0:2]), int(hms[3:5]), int(hms[6:8])
                if 1 <= mm <= 12 and 1 <= dd <= 31 and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59:
                    tok = ("glog", mm, dd, h, mi, s, usec)
                    # Only eagerly validate rare month/day edge combinations (e.g. Feb 30)
                    if dd >= 29 and mm in (2, 4, 6, 9, 11):
                        ts_val = self._materialize_ts_token(tok, fallback_ts)
                        if ts_val is None:
                            return False, None, None, sev, goroutine_id, source, body
                        return True, None, ts_val, sev, goroutine_id, source, body
                    return True, tok, None, sev, goroutine_id, source, body
                return False, None, None, sev, goroutine_id, source, body

        if len(line) >= 19:
            m_full = FULL_TS_RE.match(line)
            if m_full:
                iso_norm = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}T{m_full.group(4)}"
                parsed = parse_iso_timestamp(iso_norm)
                if parsed is not None:
                    return True, None, parsed, "", "", "", m_full.group(5)
                return False, None, None, "", "", "", m_full.group(5)

        return False, None, None, "", "", "", line

    def _parse_line(self, line: str, fallback_ts: float, is_cold_start: bool = False) -> Optional[LogEvent]:
        if not line:
            return None
        low_full = line.lower()
        if "tls handshake error" in low_full or (
            ("127.0.0.1" in low_full or "localhost" in low_full) and "googleapis.com" not in low_full
        ):
            if self._session_start_ts <= 0.0:
                h_ok, h_tok, h_eager, _sv, _gid, _src, _bd = self._split_header_meta_lazy(line, fallback_ts)
                if h_ok:
                    first_ts = (
                        h_eager
                        if h_eager is not None
                        else (self._materialize_ts_token(h_tok, fallback_ts) if h_tok is not None else None)
                    )
                    if first_ts is not None and first_ts > 0.0:
                        self._session_start_ts = first_ts
            return None

        has_header_ts, ts_token, eager_ts, severity_ch, goroutine_id, source, body = self._split_header_meta_lazy(
            line, fallback_ts
        )
        is_continuation = not has_header_ts
        if not is_continuation:
            self._last_seen_ts = eager_ts
            self._last_seen_ts_token = ts_token
            self._last_header_gid = goroutine_id
            self._last_header_src = self._source_base_name(source)
            self._block_emitted_cats.clear()
            self._skip_continuation_lines = 0
            self._skip_brace_depth = 0
            if self._session_start_ts <= 0.0:
                first_ts = (
                    eager_ts
                    if eager_ts is not None
                    else (self._materialize_ts_token(ts_token, fallback_ts) if ts_token is not None else None)
                )
                if first_ts is not None and first_ts > 0.0:
                    self._session_start_ts = first_ts
        else:
            if self._skip_continuation_lines > 0:
                self._skip_continuation_lines -= 1
                self._skip_brace_depth += body.count("{") - body.count("}")
                if self._skip_brace_depth <= 0 and "}" in body:
                    self._skip_continuation_lines = 0
                    self._skip_brace_depth = 0
                return None
            # Inherit originating goroutine & source base for multi-line JSON error payloads (e.g. http_helpers.go)
            goroutine_id = self._last_header_gid
            if not source and self._last_header_src:
                source = self._last_header_src

        # Cheap lowercase keyword pre-filter: skip heavy regexes and lazy datetime parsing on 95%+ benign INFO lines
        has_event_kw = any(kw in low_full for kw in _LOG_FAST_KEYWORDS)
        if not has_event_kw:
            # Still honor state-machine side effects (multi-line JSON block skip / ignored goroutine tagging)
            if (
                severity_ch not in ("E", "W", "F")
                and (is_continuation or not body.rstrip().endswith("{"))
                and not any(marker in low_full for marker in NON_INFERENCE_RPC_MARKERS)
            ):
                return None

        src_low = source.lower()
        src_base = self._source_base_name(source)
        body_low = body.lower()

        effective_ts = self._resolve_effective_ts(fallback_ts, is_cold_start=is_cold_start)

        # Capture cross-endpoint proxy disconnect bursts BEFORE non-inference early returns
        # (with O(1) keyword short-circuit and strict ResponseID exclusion).
        if (
            effective_ts > 0.0
            and src_base not in _PROXY_IGNORED_SOURCES
            and "responseid:" not in body_low
            and "context canceled" not in body_low
            and "shutting down" not in body_low
            and (
                "eof" in body_low
                or "forcibly closed" in body_low
                or "connection reset" in body_low
                or "broken pipe" in body_low
            )
        ):
            self._record_proxy_eof_if_matched(effective_ts, goroutine_id, src_base, body, body_low)
        is_ignored_source = any(ign in src_low or ign in body_low[:80] for ign in IGNORED_LOG_SOURCES)
        is_non_inference_rpc = any(marker in body_low for marker in NON_INFERENCE_RPC_MARKERS)
        is_non_inference_403_poll = (
            src_base == "poll.go"
            and any(marker in body_low for marker in NON_INFERENCE_403_POLL_MARKERS)
            and (
                severity_ch in ("E", "W", "F")
                or ("403" in body_low)
                or ("permission_denied" in body_low)
                or ("failed" in body_low)
                or ("error" in body_low)
            )
            and (_HARD_403_RE.search(body) is None)
        )
        if is_non_inference_403_poll:
            if goroutine_id and effective_ts > 0.0:
                self._mark_ignored_403_goroutine(goroutine_id, effective_ts)
            if not is_continuation and body.rstrip().endswith("{"):
                self._skip_continuation_lines = 64
                self._skip_brace_depth = max(1, body.count("{") - body.count("}"))
            return None
        if is_ignored_source or is_non_inference_rpc or "monitor sir_" in body_low or "llm monitor" in body_low:
            # Only tag goroutine as ignored non-inference when this line is actually an error/warning or failed RPC,
            # preventing normal INFO telemetry lines from ever muting a later real error on the same goroutine.
            is_err_like = (
                severity_ch in ("E", "W", "F")
                or is_non_inference_rpc
                or ("error" in body_low)
                or ("failed" in body_low)
                or ("unavailable" in body_low)
                or ("503" in body_low)
            )
            if goroutine_id and effective_ts > 0.0 and is_err_like:
                self._mark_ignored_goroutine(goroutine_id, effective_ts)
            if not is_continuation and body.rstrip().endswith("{"):
                self._skip_continuation_lines = 64
                self._skip_brace_depth = max(1, body.count("{") - body.count("}"))
            return None

        if not has_event_kw:
            return None

        category: Optional[str] = None
        label = ""
        parsed_dur_sec: Optional[float] = None
        m_to = STREAM_TIMEOUT_RE.search(body) if ("timeout" in body_low or "timed out" in body_low) else None
        if (
            (
                "403" in body_low
                or "tos_violation" in body_low
                or "permission_denied" in body_low
                or "suspended" in body_low
                or "disabled" in body_low
                or "banned" in body_low
            )
            and TOS_403_RE.search(body)
        ):
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
                parsed_dur_sec = dur_sec
                norm_dur = re.sub(r"^(\d+h)?(\d+m)0s$", lambda mm: (mm.group(1) or "") + mm.group(2), raw_dur) if raw_dur else ""
                if norm_dur.endswith("0m") and "h" in norm_dur:
                    norm_dur = norm_dur[:-2]
                label = f"流超时 ({norm_dur})" if norm_dur else "流超时"
        elif (
            ("503" in body_low or "no capacity" in body_low or "resource_exhausted" in body_low)
            and HTTP_503_RE.search(body)
        ):
            category = "503"
            if "capacity" in body_low:
                label = "服务端 503 No capacity"
            elif "resource_exhausted" in body_low:
                label = "服务端 503 资源耗尽"
            else:
                label = "服务端 503 Unavailable"
        elif (
            "attempt " in low_full
            and "failed" in low_full
            and RETRY_RE.search(line)
            and "context canceled" not in body_low
            and "shutting down" not in body_low
        ):
            category = "retry"
            label = "代理/流断连重试"

        if category is None:
            return None

        # On cold start, ignore lines that have no verifiable timestamp at all rather than stamping them as "now"
        if effective_ts <= 0.0:
            return None

        # Within a single multi-line JSON block, emit at most one event per category
        if is_continuation and category in self._block_emitted_cats:
            return None

        # Suppress follow-up errorreport.go echoes for an ignored non-inference goroutine (never mute real 403/timeout)
        if (
            goroutine_id
            and src_base == "errorreport.go"
            and category in ("503", "retry")
            and self._is_recent_ignored_goroutine(goroutine_id, effective_ts)
        ):
            return None
        # Suppress narrow errorreport.go echoes exclusively for poll.go cloud-sync scope 403s (never mute hard ban/inference 403s)
        if (
            goroutine_id
            and src_base == "errorreport.go"
            and category == "403"
            and (_HARD_403_RE.search(body) is None)
            and self._is_recent_ignored_403_goroutine(goroutine_id, effective_ts)
        ):
            return None

        m_att = ATTEMPT_NUM_RE.search(body)
        attempt_num = m_att.group(1) if m_att else ""

        # Deduplicate cross-layer echoes of the exact same goroutine error (e.g. http_helpers/caller + errorreport.go)
        if goroutine_id and self._is_cross_layer_echo(goroutine_id, category, effective_ts, src_base, attempt_num=attempt_num):
            return None

        # Fallback echo suppression when prior line had no goroutine ID and errorreport.go echoes within 1.5s
        if src_base == "errorreport.go":
            last_cat = self._last_event_by_cat.get(category)
            if last_cat is not None:
                last_ts, last_gid, last_src_base = last_cat
                if abs(effective_ts - last_ts) <= 1.5 and last_src_base != "errorreport.go" and (not last_gid or last_gid == goroutine_id):
                    return None

        self._block_emitted_cats.add(category)
        self._mark_error_sig(goroutine_id, category, effective_ts, src_base, attempt_num=attempt_num)
        self._last_event_by_cat[category] = (effective_ts, goroutine_id, src_base)

        hm = datetime.datetime.fromtimestamp(effective_ts).strftime("%H:%M:%S")
        clean_detail = mask_email(body.strip())
        if len(clean_detail) > 96:
            clean_detail = clean_detail[:93] + "..."
        summary = f"[{hm}] {label}"
        event_key = f"log:{category}:{int(effective_ts)}:{hash(clean_detail) & 0xFFFF:04x}"
        return LogEvent(
            timestamp=effective_ts,
            category=category,
            summary=summary,
            event_key=event_key,
            duration_sec=parsed_dur_sec,
        )

    @staticmethod
    def _split_header_meta(line: str, fallback_ts: float, is_cold_start: bool = False) -> Tuple[Optional[float], str, str, str, str]:
        # 1. glog format: [ERROR: logging before google.Init: ]W1120 05:08:41.350309 607 file.go:54] body
        m_glog = GLOG_PREFIX_RE.match(line)
        if m_glog:
            sev = m_glog.group(1)
            mm, dd, hms = int(m_glog.group(2)), int(m_glog.group(3)), m_glog.group(4)
            frac = (m_glog.group(5) or "")[:6]
            usec = int(frac.ljust(6, "0")) if frac else 0
            goroutine_id, source, body = m_glog.group(6), m_glog.group(7), m_glog.group(8)
            now_dt = datetime.datetime.fromtimestamp(fallback_ts)
            try:
                h, mi, s = (int(p) for p in hms.split(":"))
                cand = now_dt.replace(month=mm, day=dd, hour=h, minute=mi, second=s, microsecond=usec)
                if cand.timestamp() > fallback_ts + 86400:
                    cand = cand.replace(year=now_dt.year - 1)
                return cand.timestamp(), sev, goroutine_id, source, body
            except ValueError:
                return None, sev, goroutine_id, source, body

        # 2. ISO / Go stdlog slash format: 2026/11/20 02:18:54 body
        m_full = FULL_TS_RE.match(line)
        if m_full:
            iso_norm = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}T{m_full.group(4)}"
            parsed = parse_iso_timestamp(iso_norm)
            return parsed, "", "", "", m_full.group(5)

        return None, "", "", "", line

    @classmethod
    def _split_header_and_body(cls, line: str, fallback_ts: float, is_cold_start: bool = False) -> Tuple[Optional[float], str, str]:
        ts, _sev, _gid, source, body = cls._split_header_meta(line, fallback_ts, is_cold_start=is_cold_start)
        return ts, source, body

    def get_counts(
        self,
        now_ts: float,
        window_min: int,
        account_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: Optional[float] = None,
        range_end_ts: Optional[float] = None,
        max_notable: Optional[int] = 3,
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

        if max_notable is not None:
            return recent, today, (in_window_notable[-max_notable:] if max_notable > 0 else [])
        return recent, today, in_window_notable

    def get_proxy_outage_info(
        self,
        now_ts: float,
        window_min: int,
        account_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: Optional[float] = None,
        range_end_ts: Optional[float] = None,
    ) -> Tuple[int, float, float, int]:
        """Detect a 60s cross-endpoint / multi-stream proxy disconnect burst within the active account & window.

        Returns ``(burst_event_count, outage_start_ts, outage_end_ts, distinct_buckets)`` for the latest burst,
        or ``(0, 0.0, 0.0, 0)`` if no genuine proxy outage occurred.
        """
        win_end = range_end_ts if range_end_ts is not None else now_ts
        default_start = win_end - max(1, window_min) * 60
        cutoff = max(range_start_ts if range_start_ts is not None else default_start, account_since_ts)
        evs: List[Tuple[float, str, str]] = []
        for ts, bucket, gid in self._proxy_eof_events:
            if account_filter_fn is not None and not account_filter_fn(ts):
                continue
            if cutoff <= ts <= win_end + 2.0:
                evs.append((ts, bucket, gid))
        if len(evs) < 3:
            return 0, 0.0, 0.0, 0
        evs.sort(key=lambda x: x[0])

        best_end_ts = 0.0
        best_start_ts = 0.0
        best_count = 0
        best_buckets = 0
        left = 0
        for right in range(len(evs)):
            r_ts = evs[right][0]
            while left < right and (r_ts - evs[left][0]) > 60.0:
                left += 1
            cnt = right - left + 1
            if cnt < 3:
                continue
            win_slice = evs[left : right + 1]
            buckets = {b for _, b, _ in win_slice}
            stream_gids = {g for _, b, g in win_slice if b == "stream"}
            if len(buckets) >= 2 or len(stream_gids) >= 3:
                # Track the latest burst window; if extending the same 60s burst, accumulate max coverage
                if r_ts >= best_end_ts:
                    if best_end_ts > 0.0 and (r_ts - best_end_ts) <= 60.0:
                        best_end_ts = r_ts
                        best_count = max(best_count, cnt)
                        best_buckets = max(best_buckets, len(buckets))
                    else:
                        best_start_ts = evs[left][0]
                        best_end_ts = r_ts
                        best_count = cnt
                        best_buckets = len(buckets)
        if best_count < 3:
            return 0, 0.0, 0.0, 0
        return best_count, best_start_ts, best_end_ts, best_buckets

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
        should_yield_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = sanitize_config(config or DEFAULT_CONFIG)
        self.read_only: bool = bool(read_only)
        self._should_yield_fn: Optional[Callable[[], bool]] = should_yield_fn
        self._poll_yield_budget_remaining: float = 0.0
        self._poll_seq: int = 0
        self._in_poll: bool = False
        self._vscdb_last_stat_poll_seq: int = -1
        self._static_conv_agg_cache: "OrderedDict[str, Tuple[Tuple[Any, ...], Dict[str, Any]]]" = OrderedDict()
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
        diag_log_path = base_paths.get("diag_log_path")
        vscdb_path = base_paths.get("vscdb_path")
        annotations_dir = base_paths.get("annotations_dir")
        global_rules_path = base_paths.get("global_rules_path")
        # In isolated test harnesses where daemon_dir is overridden without explicit ledger_path/cockpit_logs_dir/vscdb_path/annotations_dir/global_rules_path/diag_log_path,
        # avoid reading the host's real Cockpit logs, VSCode state DB, annotations, GEMINI.md, or polluting the real local ledger/diag files.
        if paths and "ledger_path" not in paths:
            ledger_path = None
        if paths and "diag_log_path" not in paths:
            diag_log_path = None
        if paths and "cockpit_logs_dir" not in paths:
            cockpit_logs_dir = None
        if paths and "vscdb_path" not in paths:
            vscdb_path = None
        if paths and "annotations_dir" not in paths:
            annotations_dir = None
        if paths and "global_rules_path" not in paths:
            global_rules_path = None

        self.diag_log_path: Optional[Path] = (
            None if (self.read_only or diag_log_path is None) else Path(diag_log_path)
        )
        self.vscdb_path: Optional[Path] = Path(vscdb_path) if vscdb_path is not None else None
        self.annotations_dir: Optional[Path] = Path(annotations_dir) if annotations_dir is not None else None
        self.global_rules_path: Optional[Path] = Path(global_rules_path) if global_rules_path is not None else None
        self._last_observed_rules_mtime_ns: int = -1
        summaries_db_path = base_paths.get("summaries_db_path")
        if paths and "summaries_db_path" not in paths:
            summaries_db_path = self.conversations_dir.parent / "conversation_summaries.db"
        self.summaries_db_path: Path = Path(summaries_db_path) if summaries_db_path is not None else (self.conversations_dir.parent / "conversation_summaries.db")
        self._lock = threading.RLock()
        self._transcript_cache: "OrderedDict[str, Tuple[Tuple[int, int], List[Any], bool, bool]]" = OrderedDict()
        self._transcript_inc_state: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._transcript_error_ts: "OrderedDict[str, List[Tuple[float, str, int, int, int]]]" = OrderedDict()
        self._transcript_tail_meta: "OrderedDict[str, Tuple[List[Tuple[float, int, int, int]], int, int, int]]" = OrderedDict()
        self._hist_daily_profiles_cache_key: Tuple[Any, ...] = ()
        self._hist_daily_profiles_cache_ts: float = 0.0
        self._hist_daily_profiles_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._hist_hourly_buckets_cache: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._classified_cache: "OrderedDict[str, Tuple[Tuple[Any, ...], List[StepRecord]]]" = OrderedDict()
        self._full_align_cache: "OrderedDict[Tuple[str, Tuple[int, int], int, int], List[Optional[Any]]]" = OrderedDict()
        self._subagent_hint_cache: "OrderedDict[str, Tuple[int, bool]]" = OrderedDict()
        self._conv_titles: Dict[str, str] = {}
        self._annotation_title_cache: "OrderedDict[str, Tuple[Tuple[int, int], str]]" = OrderedDict()
        self._vscdb_titles_stamp: Optional[Tuple[int, int]] = None
        self._vscdb_titles_cache: Dict[str, str] = {}
        self._db_reader = ConversationDbReader(self.conversations_dir, max_cache_entries=64, tail_rows=16)
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
        self._active_hub_port: int = 0
        self._active_hub_token: str = ""
        self._active_hub_creation_ft: int = 0
        self._any_hub_pid_alive: bool = False
        self._account_rpc_fail_count: int = 0
        self._last_account_rpc_ok_ts: float = 0.0
        self._last_probe_any_hub_pid_alive: bool = False
        self._last_probe_seq: int = 0
        self._last_probe_diag: Dict[str, Any] = {}
        self._last_diag_state: str = ""
        self._last_diag_log_ts: float = 0.0
        self._last_account_poll: float = 0.0
        self._last_titles_poll_ts: float = 0.0
        self._last_weekly_poll_ts: float = 0.0
        self._last_weekly_email: str = ""
        self._last_weekly_pools: Dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]] = {}
        self._cockpit_weekly_cache_key: Tuple[Any, ...] = ()
        self._cockpit_weekly_cache_rows: Dict[str, Tuple[Any, float, Any]] = {}
        self._last_quota_family: str = "gemini"
        self._baseline_ready: bool = False
        self._emitted_alert_keys: Deque[str] = deque(maxlen=256)
        self._steps_alert_state: Dict[str, Tuple[float, bool, int]] = {}
        self._recorded_deg_anchors: "OrderedDict[Tuple[str, int], Optional[int]]" = OrderedDict()
        self._media_size_cache: "OrderedDict[str, int]" = OrderedDict()
        self._model_enum_map: "OrderedDict[str, str]" = OrderedDict()

    def set_should_yield_fn(self, fn: Optional[Callable[[], bool]]) -> None:
        """Register an optional predicate (e.g. UI dragging flag) so long scan loops yield the GIL."""
        self._should_yield_fn = fn

    def _maybe_yield_for_ui(self) -> None:
        fn = self._should_yield_fn
        if fn is None or self._poll_yield_budget_remaining <= 0.0:
            return
        try:
            if not fn():
                return
        except Exception:
            return
        step_sleep = min(0.05, self._poll_yield_budget_remaining)
        if step_sleep > 0.0:
            self._poll_yield_budget_remaining -= step_sleep
            time.sleep(step_sleep)

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
                    self._transcript_inc_state.pop(cid, None)
                    self._transcript_error_ts.pop(cid, None)
                    self._transcript_tail_meta.pop(cid, None)
                    self._classified_cache.pop(cid, None)
                    self._static_conv_agg_cache.pop(cid, None)
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
            # Decouple offline detection from UI history view filters (6h/today/7d/custom account),
            # and hard-lock against auto_offline cleanup whenever any live Antigravity Hub PID is present!
            is_offline_idle = bool(
                (not getattr(snap, "hub_pid_alive", False))
                and (not self._any_hub_pid_alive)
                and (snap.is_host_idle_offline or ((not snap.account.online) and (snap.grade == "IDLE")))
            )
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

    def _observe_and_get_rule_timestamps(self, now: float, since_ts: float = 0.0) -> List[float]:
        """Lightweight O(1) stat() check on global GEMINI.md mtime, merged into ledger rule-change history."""
        if self.global_rules_path is not None:
            try:
                st = self.global_rules_path.stat()
                if st.st_mtime_ns != self._last_observed_rules_mtime_ns:
                    self._ledger.record_rule_change(st.st_mtime, now_ts=now)
                    # Only cache mtime_ns when not rejected as future-relative to a historical now_ts call
                    if st.st_mtime <= now + 300.0:
                        self._last_observed_rules_mtime_ns = st.st_mtime_ns
            except OSError:
                pass
        return self._ledger.get_rule_change_timestamps(since_ts)

    def _classify_steps_cached(
        self,
        conv_id: str,
        raw_steps: List[Any],
        switch_timestamps: List[float],
        rule_change_timestamps: Optional[List[float]] = None,
    ) -> List[StepRecord]:
        t_entry = self._transcript_cache.get(conv_id)
        t_stamp = t_entry[0] if t_entry is not None else (0, len(raw_steps))
        if switch_timestamps and raw_steps:
            inc_meta = self._transcript_inc_state.get(conv_id)
            min_ts: Optional[float] = None
            max_ts: Optional[float] = None
            if (
                inc_meta is not None
                and t_entry is not None
                and len(raw_steps) == len(t_entry[1])
                and inc_meta.get("min_step_ts", 0.0) > 0.0
                and inc_meta.get("max_step_ts", 0.0) > 0.0
            ):
                min_ts = float(inc_meta["min_step_ts"])
                max_ts = float(inc_meta["max_step_ts"])
            else:
                for item in raw_steps:
                    ts_v = float(item.timestamp if isinstance(item, _RawStepSlot) else (item.get("timestamp", 0.0) or 0.0))
                    if ts_v > 0.0:
                        if min_ts is None or ts_v < min_ts:
                            min_ts = ts_v
                        if max_ts is None or ts_v > max_ts:
                            max_ts = ts_v
            if min_ts is not None and max_ts is not None:
                lo_sw = min_ts - 2.0
                hi_sw = max_ts + 2.0
                sw_sig = tuple(round(t, 1) for t in sorted(switch_timestamps) if lo_sw <= t <= hi_sw)
            else:
                sw_sig = ()
        else:
            sw_sig = ()
        rule_ts = rule_change_timestamps or []
        rule_sig = tuple(round(t, 1) for t in rule_ts[-64:])
        idle_sec = int(self.config["idle_rewarm_sec"])
        min_cache_tok = int(self.config.get("min_cacheable_tokens", 32000))
        cache_sig = (t_stamp, len(raw_steps), sw_sig, rule_sig, idle_sec, min_cache_tok)
        cached_slot = self._classified_cache.get(conv_id)
        if cached_slot is not None and cached_slot[0] == cache_sig:
            self._classified_cache.move_to_end(conv_id)
            return cached_slot[1]
        steps = classify_steps(
            raw_steps,
            idle_rewarm_sec=idle_sec,
            min_cacheable_tokens=min_cache_tok,
            account_switches=switch_timestamps,
            rule_change_timestamps=rule_ts,
        )
        self._classified_cache[conv_id] = (cache_sig, steps)
        self._classified_cache.move_to_end(conv_id)
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
    def _compute_eval_window_stats(
        eval_steps: List[StepRecord],
        eval_recent10: List[StepRecord],
    ) -> Dict[str, Any]:
        """Single source of truth for miss/hit/recovery counters shared by initial build & eval-floor recompute."""
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
        hit_ratio_r10 = (hits_r10 / cacheable_r10) if cacheable_r10 > 0 else None
        last_abnormal_ts = max((s.timestamp for s in abnormal_steps_r10), default=0.0)
        # Count recovered hits after the latest abnormal_miss in eval_recent10 (skipping valid expected_cold steps)
        tail_hits_after_abn = 0
        if abnormal_r10 > 0 and consec_miss == 0:
            for s in reversed(eval_recent10):
                if s.classification == "abnormal_miss":
                    break
                if s.classification == "hit":
                    tail_hits_after_abn += 1
        return {
            "consec_miss": consec_miss,
            "consec_miss_start_step": consec_start_step,
            "single_miss": consec_miss == 1,
            "wasted_cold_tokens": sum(s.input_tokens for s in eval_recent10 if s.classification == "abnormal_miss"),
            "abnormal_in_recent10": abnormal_r10,
            "cacheable_in_recent10": cacheable_r10,
            "recent10_hit_ratio": hit_ratio_r10,
            "first_abnormal_step_in_recent10": abnormal_steps_r10[0].step_index if abnormal_steps_r10 else 0,
            "tail_consec_hits_after_abnormal": tail_hits_after_abn,
            "last_abnormal_ts": last_abnormal_ts,
        }

    @staticmethod
    def _recompute_conv_eval_stats(c: ConversationSummary, eval_since_ts: float) -> None:
        """Restrict a conversation's miss/hit health counters to steps at or after ``eval_since_ts``."""
        src_steps = c._scoped_steps_all or c.recent_steps
        eval_steps = [s for s in src_steps if s.timestamp >= eval_since_ts - 2.0]
        eval_recent10 = [s for s in c.recent_steps if s.timestamp >= eval_since_ts - 2.0]
        st = AntigravityMonitorEngine._compute_eval_window_stats(eval_steps, eval_recent10)
        c.consec_miss = st["consec_miss"]
        c.consec_miss_start_step = st["consec_miss_start_step"]
        c.single_miss = st["single_miss"]
        c.wasted_cold_tokens = st["wasted_cold_tokens"]
        c.abnormal_in_recent10 = st["abnormal_in_recent10"]
        c.cacheable_in_recent10 = st["cacheable_in_recent10"]
        c.recent10_hit_ratio = st["recent10_hit_ratio"]
        c.first_abnormal_step_in_recent10 = st["first_abnormal_step_in_recent10"]
        c.tail_consec_hits_after_abnormal = st["tail_consec_hits_after_abnormal"]
        c.last_abnormal_ts = st["last_abnormal_ts"]
        if c.freshness_invalid_reason:
            c.freshness_invalid_reason = _format_freshness_invalid_reason(c.consec_miss, c.latest_cache_reason)

    @staticmethod
    def _count_steps_in_window(c: ConversationSummary, lo_ts: float, hi_ts: float) -> Tuple[int, int]:
        """Scan conversation steps within [lo_ts, hi_ts] without early break so tail-appended OOO steps never truncate."""
        src = c._scoped_steps_all or c.recent_steps
        hits = 0
        abns = 0
        for s in src:
            if lo_ts <= s.timestamp <= hi_ts:
                if s.classification == "hit":
                    hits += 1
                elif s.classification == "abnormal_miss":
                    abns += 1
        return hits, abns

    @classmethod
    def _select_cache_health_signals(
        cls,
        recent_active: List[ConversationSummary],
        *,
        now_ts: float,
        eval_cutoff_ts: float,
        eval_end_ts: float,
        is_historical_rating: bool,
        hr_key: str,
        is_custom_account_view: bool,
        is_all_accounts_mixed: bool = False,
    ) -> Tuple[int, bool, Optional[ConversationSummary], Optional[ConversationSummary]]:
        """Derive cache T1/WARN health signals with recovery clearance, 10m aging, and subagent single-step jitter denoising."""
        max_consec_miss = max((c.consec_miss for c in recent_active), default=0)

        anchor_ts = eval_end_ts if eval_end_ts > 0.0 else now_ts
        win10_lo = max(anchor_ts - 600.0, eval_cutoff_ts - 2.0)
        win10_hi = anchor_ts + 2.0
        is_live_aging = (not is_historical_rating) and (hr_key == "30m") and (not is_custom_account_view)

        eval_mains = [c for c in recent_active if not c.is_subagent]
        eval_subs = [c for c in recent_active if c.is_subagent]

        # Inspect ALL severe frequent-miss candidates (never short-circuit on the first downgraded stale conv!)
        can_downgrade_stale_severe = bool(
            is_historical_rating
            and hr_key == "7d"
            and (not is_all_accounts_mixed)
        )
        severe_frequent_miss_conv: Optional[ConversationSummary] = None
        for c in recent_active:
            if not (
                c.abnormal_in_recent10 >= 4
                and c.cacheable_in_recent10 >= 4
                and (c.recent10_hit_ratio is not None and c.recent10_hit_ratio < 0.5)
            ):
                continue
            if (
                can_downgrade_stale_severe
                and c.consec_miss == 0
                and c.last_abnormal_ts > 0.0
                and (anchor_ts - c.last_abnormal_ts) >= 86400.0
                and any(
                    m.conversation_id != c.conversation_id
                    and m.abnormal_in_recent10 == 0
                    and m.cacheable_in_recent10 >= 2
                    and m.latest_timestamp > c.last_abnormal_ts + 3600.0
                    for m in eval_mains
                )
            ):
                # Stale (>24h) historical frequent-miss conv that already recovered and was followed by
                # healthy same-account main conversations degrades to WARN via unstable_candidates below.
                continue
            if severe_frequent_miss_conv is None:
                severe_frequent_miss_conv = c

        win10_stats: Dict[str, Tuple[int, int]] = {
            c.conversation_id: cls._count_steps_in_window(c, win10_lo, win10_hi)
            for c in recent_active
        }

        def _other_pool_health(pool: List[ConversationSummary], exclude_cid: str) -> Tuple[int, int]:
            th, ta = 0, 0
            for other in pool:
                if other.conversation_id == exclude_cid:
                    continue
                oh, oa = win10_stats.get(other.conversation_id, (0, 0))
                th += oh
                ta += oa
            return th, ta

        has_verified_healthy_main = False
        for m in eval_mains:
            if m.abnormal_in_recent10 != 0:
                continue
            if not is_historical_rating:
                if win10_stats.get(m.conversation_id, (0, 0))[0] >= 1:
                    has_verified_healthy_main = True
                    break
            else:
                # m.abnormal_in_recent10 == 0 is already guaranteed; cacheable_in_recent10 >= 1 proves >=1 in-window hit!
                if m.cacheable_in_recent10 >= 1:
                    has_verified_healthy_main = True
                    break

        all_mains_zero_abn = bool(eval_mains) and all(m.abnormal_in_recent10 == 0 for m in eval_mains)
        all_mains_win10_abn = sum(win10_stats.get(m.conversation_id, (0, 0))[1] for m in eval_mains)
        mains_fully_healthy = has_verified_healthy_main and all_mains_zero_abn and (all_mains_win10_abn == 0)
        allow_sub_single_denoise = mains_fully_healthy and (not is_all_accounts_mixed)

        def _is_single_miss_aged_out(c: ConversationSummary, pool: List[ConversationSummary]) -> bool:
            if not is_live_aging:
                return False
            if c.last_abnormal_ts <= 0.0 or (anchor_ts - c.last_abnormal_ts) < 600.0:
                return False
            other_hits, other_abns = _other_pool_health(pool, c.conversation_id)
            if other_hits >= 2 and other_abns == 0:
                return True
            # For stale (>10m) subagent single_miss, verified fully-healthy mains also prove recovery
            if c.is_subagent and allow_sub_single_denoise:
                main_hits = sum(win10_stats.get(m.conversation_id, (0, 0))[0] for m in eval_mains)
                if main_hits >= 2:
                    return True
            return False

        active_main_single = any(
            c.single_miss and not _is_single_miss_aged_out(c, eval_mains)
            for c in eval_mains
        )
        active_sub_single_convs = [
            c
            for c in eval_subs
            if c.single_miss and not _is_single_miss_aged_out(c, eval_subs)
        ]
        # Concurrent subagent burst check counts distinct subagents with recent (<10m) single_miss
        recent_sub_single_count = sum(
            1
            for c in active_sub_single_convs
            if c.last_abnormal_ts <= 0.0 or (anchor_ts - c.last_abnormal_ts) < 600.0
        )
        if active_main_single:
            any_single_miss = True
        elif recent_sub_single_count >= 2:
            any_single_miss = True
        elif active_sub_single_convs and not allow_sub_single_denoise:
            any_single_miss = True
        else:
            any_single_miss = False

        unstable_candidates: List[ConversationSummary] = []
        for c in recent_active:
            if c.abnormal_in_recent10 < 2:
                continue
            if c.abnormal_in_recent10 >= 4 or c.consec_miss >= 1:
                unstable_candidates.append(c)
                continue
            # Both main and subagent conversations with 2..3 abnormal misses MUST satisfy recovery clearance (never silently bypassed!)
            need_hits = 3 if c.abnormal_in_recent10 == 2 else 4
            if is_historical_rating and hr_key != "30m":
                if c.last_abnormal_ts > 0.0:
                    c_lo = max(c.last_abnormal_ts - 600.0, eval_cutoff_ts - 2.0)
                    c_hi = min(c.last_abnormal_ts + 600.0, anchor_ts + 2.0)
                    other_main_abns = sum(
                        cls._count_steps_in_window(m, c_lo, c_hi)[1]
                        for m in eval_mains
                        if m.conversation_id != c.conversation_id
                    )
                else:
                    other_main_abns = 1
            else:
                _oh_m, other_main_abns = _other_pool_health(eval_mains, c.conversation_id)
            if c.tail_consec_hits_after_abnormal >= need_hits and other_main_abns == 0:
                continue
            if (
                is_live_aging
                and c.last_abnormal_ts > 0.0
                and (anchor_ts - c.last_abnormal_ts) >= 600.0
                and c.tail_consec_hits_after_abnormal >= 1
                and all_mains_win10_abn == 0
            ):
                continue
            unstable_candidates.append(c)

        unstable_recovery_conv: Optional[ConversationSummary] = None
        if unstable_candidates:
            unstable_candidates.sort(
                key=lambda c: (0 if not c.is_subagent else 1, -c.abnormal_in_recent10, -c.latest_timestamp)
            )
            unstable_recovery_conv = unstable_candidates[0]

        return max_consec_miss, any_single_miss, severe_frequent_miss_conv, unstable_recovery_conv

    def _build_conversation_summary(
        self,
        conv_id: str,
        raw_steps: List[Any],
        has_user_explicit: bool,
        has_subagent_marker: bool,
        now: float,
        switch_timestamps: List[float],
        current_acct_since_ts: float = 0.0,
        account_filter_fn: Optional[Callable[[float], bool]] = None,
        range_start_ts: float = 0.0,
        rule_change_timestamps: Optional[List[float]] = None,
        resolve_title: bool = True,
    ) -> Optional[Tuple[ConversationSummary, List[StepRecord], List[int]]]:
        all_steps = self._classify_steps_cached(
            conv_id,
            raw_steps,
            switch_timestamps,
            rule_change_timestamps=rule_change_timestamps,
        )
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

        st = self._compute_eval_window_stats(eval_steps, eval_recent10)
        consec_miss = st["consec_miss"]
        consec_start_step = st["consec_miss_start_step"]
        single_miss = st["single_miss"]
        wasted_cold = st["wasted_cold_tokens"]
        abnormal_r10 = st["abnormal_in_recent10"]
        cacheable_r10 = st["cacheable_in_recent10"]
        hit_ratio_r10 = st["recent10_hit_ratio"]
        first_abn_step_r10 = st["first_abnormal_step_in_recent10"]
        tail_hits_after_abn = st["tail_consec_hits_after_abnormal"]
        last_abn_ts = st["last_abnormal_ts"]

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
                    fresh_invalid = _format_freshness_invalid_reason(consec_miss, latest.reason)
                elif (
                    account_filter_fn is not None
                    or current_acct_since_ts <= 0.0
                    or latest.timestamp >= current_acct_since_ts - 2.0
                ):
                    fresh_rem = int(self.config["freshness_window_sec"] - age)

        summary = ConversationSummary(
            conversation_id=conv_id,
            short_id=conv_id[:6],
            model=format_model_name(latest.model, enum_map=self._model_enum_map),
            model_raw=resolve_model_label(latest.model, enum_map=self._model_enum_map) or latest.model,
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
            title=self._resolve_conv_title(conv_id) if resolve_title else "",
            full_step_count=len(all_steps),
            full_latest_timestamp=all_steps[-1].timestamp,
            latest_cache_reason=latest.reason,
            active_media_count=latest.active_media_count,
            active_media_bytes=latest.active_media_bytes,
            tail_consec_hits_after_abnormal=tail_hits_after_abn,
            last_abnormal_ts=last_abn_ts,
            _scoped_steps_all=scoped_steps,
        )
        return summary, all_steps, scoped_indices

    def _backfill_conv_models_from_gens(
        self,
        c: ConversationSummary,
        aligned_recent: List[Optional[Any]],
        tail_gens: List[Any],
    ) -> None:
        """Backfill missing StepRecord.model on older transcripts (e.g. 2026-11-18/19) using SQLite gen_metadata.model_label."""
        fallback_lbl = ""
        for g in reversed(aligned_recent):
            if g is not None and getattr(g, "model_label", ""):
                fallback_lbl = resolve_model_label(g.model_label, enum_map=self._model_enum_map)
                if fallback_lbl:
                    break
        if not fallback_lbl:
            for g in reversed(tail_gens):
                if g is not None and getattr(g, "model_label", ""):
                    fallback_lbl = resolve_model_label(g.model_label, enum_map=self._model_enum_map)
                    if fallback_lbl:
                        break
        if not fallback_lbl:
            return

        # Backfill c.recent_steps immutably via replace() (prefer nearest prior known model over global tail)
        if c.recent_steps:
            r_off = len(c.recent_steps) - len(aligned_recent)
            new_recent: List[StepRecord] = []
            changed_r = False
            last_known_r = ""
            for idx, st in enumerate(c.recent_steps):
                if st.model:
                    last_known_r = st.model
                    new_recent.append(st)
                    continue
                gi = idx - r_off
                g = aligned_recent[gi] if 0 <= gi < len(aligned_recent) else None
                raw_lbl = getattr(g, "model_label", "") if g is not None else ""
                res_lbl = (
                    resolve_model_label(raw_lbl, enum_map=self._model_enum_map)
                    if raw_lbl
                    else (last_known_r or fallback_lbl)
                )
                if res_lbl:
                    last_known_r = res_lbl
                    new_recent.append(replace(st, model=res_lbl))
                    changed_r = True
                else:
                    new_recent.append(st)
            if changed_r:
                c.recent_steps = new_recent

        # Backfill c._scoped_steps_all immutably via replace() (tail-offset safe; prefer nearest prior known model)
        if c._scoped_steps_all:
            gens_all = c._scoped_gens_all if c._scoped_gens_all else aligned_recent
            s_off = len(c._scoped_steps_all) - len(gens_all)
            new_all: List[StepRecord] = []
            changed_a = False
            last_known_a = ""
            for idx, st in enumerate(c._scoped_steps_all):
                if st.model:
                    last_known_a = st.model
                    new_all.append(st)
                    continue
                gi = idx - s_off
                g = gens_all[gi] if 0 <= gi < len(gens_all) else None
                raw_lbl = getattr(g, "model_label", "") if g is not None else ""
                res_lbl = (
                    resolve_model_label(raw_lbl, enum_map=self._model_enum_map)
                    if raw_lbl
                    else (last_known_a or fallback_lbl)
                )
                if res_lbl:
                    last_known_a = res_lbl
                    new_all.append(replace(st, model=res_lbl))
                    changed_a = True
                else:
                    new_all.append(st)
            if changed_a:
                c._scoped_steps_all = new_all

        if not c.model_raw or c.model == "未知模型":
            latest_m = c.recent_steps[-1].model if c.recent_steps else fallback_lbl
            c.model_raw = latest_m or fallback_lbl
            c.model = format_model_name(c.model_raw, enum_map=self._model_enum_map)

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
            c.large_ctx_ttft_allowance_sec = compute_large_ctx_ttft_allowance(c.context_tokens, c.max_context_tokens)
            return
        if snap.is_subagent_hint and not self._transcript_cache.get(c.conversation_id, (None, [], False, False))[2]:
            c.is_subagent = True
        c.max_context_tokens = snap.max_context_tokens
        c.large_ctx_ttft_allowance_sec = compute_large_ctx_ttft_allowance(c.context_tokens, c.max_context_tokens)

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
            self._backfill_conv_models_from_gens(c, aligned_recent, tail_gens)
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
                # For display avoid brief "— t/s" flash on instant <0.15s tool bursts,
                # but NEVER pass a backfilled prior TPS into _classify_speed!
                display_tps = latest_gen.tps
                if display_tps is None:
                    display_tps = next((t for t in reversed(c.recent_tps_list) if t is not None), None)
                c.tps = display_tps
                c.shard_tag = latest_gen.shard_tag
                c.speed_status, c.media_ttft_exempted = self._classify_speed_ex(
                    latest_gen.output_tokens,
                    latest_gen.tps,
                    latest_gen.ttft_sec,
                    c.context_tokens,
                    streaming_sec=latest_gen.streaming_sec,
                    active_media_count=c.active_media_count,
                    active_media_bytes=c.active_media_bytes,
                )
            return

        if account_scoped or is_tail_truncated:
            self._backfill_conv_models_from_gens(c, [], tail_gens)
            return

        self._backfill_conv_models_from_gens(c, [], tail_gens)
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
            display_tps = gen.tps
            if display_tps is None:
                display_tps = next((t for t in reversed(c.recent_tps_list) if t is not None), None)
            c.tps = display_tps
            c.shard_tag = gen.shard_tag
            c.speed_status, c.media_ttft_exempted = self._classify_speed_ex(
                gen.output_tokens,
                gen.tps,
                gen.ttft_sec,
                c.context_tokens,
                streaming_sec=gen.streaming_sec,
                active_media_count=c.active_media_count,
                active_media_bytes=c.active_media_bytes,
            )

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

    def _find_recent_cold_switch_hint(self, since_ts: float, now: float) -> Optional[float]:
        """Locate the cold-start transition step (cr==0) in [since_ts, now] for a newly observed account.

        Refreshes hot transcripts modified since ``since_ts`` first so freshly written steps are never missed,
        and allows stepping back over immediate warm follow-ups of the new account while never crossing an earlier
        warm step that preceded the cold transition.
        """
        if since_ts <= 0.0 or since_ts >= now:
            return None
        # Refresh only freshly modified transcripts (typically 1-3 hot files in the last <=65s)
        hot_cutoff = since_ts - 5.0
        if self.brain_dir.is_dir():
            try:
                with os.scandir(self.brain_dir) as it:
                    for entry in it:
                        if not entry.is_dir():
                            continue
                        tp = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
                        try:
                            st = os.stat(tp)
                        except OSError:
                            continue
                        if st.st_size > 0 and st.st_mtime >= hot_cutoff:
                            self._read_transcript_cached(entry.name, tp)
            except OSError:
                pass

        earliest_cold: Optional[float] = None
        for _cid, cached in self._transcript_cache.items():
            raw_steps = cached[1]
            if not raw_steps:
                continue
            # Scan backwards within [since_ts, now]:
            # - Before finding a cold step, skip over any tail warm steps (new account may have already run step 1+2 within 5s)
            # - Once a cacheable/first-turn cold step is found, stop immediately upon seeing an earlier warm step
            cand_cold: Optional[float] = None
            for idx in range(len(raw_steps) - 1, -1, -1):
                st = raw_steps[idx]
                ts = float(st.get("timestamp", 0.0) or 0.0)
                if ts < since_ts:
                    break
                if ts > now + 2.0:
                    continue
                cr = int(st.get("cache_read_tokens", 0) or 0)
                inp = int(st.get("input_tokens", 0) or 0)
                tot = inp + cr
                if cr > 0:
                    if cand_cold is not None:
                        # Do NOT cross back over an earlier warm hit that belonged to the previous account
                        break
                    continue
                if tot >= int(self.config.get("min_cacheable_tokens", 32000)) or idx == 0:
                    cand_cold = ts
            if cand_cold is not None and (earliest_cold is None or cand_cold < earliest_cold):
                earliest_cold = cand_cold
        return (earliest_cold - 0.5) if earliest_cold is not None else None

    def _is_cached_hub_still_same_process(self) -> bool:
        """Verify the cached Hub PID is still alive and (on Windows) has not been recycled."""
        pid = self._active_hub_pid
        if pid <= 0 or not _is_windows_pid_alive(pid):
            return False
        if os.name == "nt" and self._active_hub_creation_ft > 0:
            alive, _exe, cft = _inspect_windows_pid(pid)
            return bool(alive and cft > 0 and cft == self._active_hub_creation_ft)
        return True

    def _write_diag_log(self, now: float, state: str, detail: str, has_error: bool = False) -> None:
        """Write throttled, privacy-masked diagnostics strictly when read_only=False (<= 64 KB tail cap)."""
        if self.read_only or self.diag_log_path is None:
            return
        prev_state = self._last_diag_state
        state_changed = bool(prev_state) and (state != prev_state)
        is_initial_abnormal = (not prev_state) and (state in ("grace", "wait_pid_alive") or has_error)
        throttled_ok = (self._last_diag_log_ts <= 0.0) or ((now - self._last_diag_log_ts) >= 60.0)
        should_write = (
            state_changed
            or is_initial_abnormal
            or ((state in ("grace", "wait_pid_alive") or has_error) and throttled_ok)
        )
        self._last_diag_state = state
        if not should_write:
            return
        self._last_diag_log_ts = now
        try:
            ts_str = _epoch_to_iso_local(now)
        except (OSError, OverflowError, ValueError):
            ts_str = str(int(now))
        trans = f"{prev_state}->{state}" if (prev_state and prev_state != state) else state
        raw_line = f"[{ts_str}] [{trans}] {detail}".replace("\r", " ").replace("\n", " ")
        line = mask_email(raw_line)
        if len(line) > 320:
            line = line[:317] + "..."
        entry_bytes = (line + "\n").encode("utf-8", errors="replace")
        try:
            self.diag_log_path.parent.mkdir(parents=True, exist_ok=True)
            existing = b""
            if self.diag_log_path.is_file():
                with self.diag_log_path.open("rb") as fh:
                    existing = fh.read()
            combined = existing + entry_bytes
            if len(combined) > _DIAG_LOG_MAX_BYTES:
                keep = combined[-_DIAG_LOG_MAX_BYTES:]
                nl = keep.find(b"\n")
                if nl != -1 and nl + 1 < len(keep):
                    keep = keep[nl + 1 :]
                tmp_path = self.diag_log_path.with_name(f"{self.diag_log_path.name}.tmp.{os.getpid()}")
                try:
                    with tmp_path.open("wb") as fh:
                        fh.write(keep)
                    os.replace(tmp_path, self.diag_log_path)
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
            else:
                with self.diag_log_path.open("ab") as fh:
                    fh.write(entry_bytes)
        except OSError:
            pass

    def _refresh_account_cache(self, now: float, active_family: str) -> None:
        """Unified account/quota refresh entry with live-Hub debounce and zero-delay true-offline drop.

        All timers strictly use the passed ``now`` timestamp (never ``time.monotonic()``).
        """
        seq_before = self._last_probe_seq
        probed = self._poll_account_and_quota(now, active_family)
        self._last_account_poll = now
        ran_real_probe = self._last_probe_seq != seq_before
        diag = dict(self._last_probe_diag) if ran_real_probe else {}

        if probed.online:
            self._account_cache = probed
            self._account_rpc_fail_count = 0
            self._last_account_rpc_ok_ts = now
            self._any_hub_pid_alive = True
            self._write_diag_log(
                now,
                "online",
                (
                    f"online=1 hub_pid_alive=1 fail=0 port={self._active_hub_port} "
                    f"probes={diag.get('probes', 0)} files={diag.get('files', 0)} "
                    f"acct={probed.short_label or 'unknown'}"
                ),
                has_error=False,
            )
            return

        any_alive = bool(self._last_probe_any_hub_pid_alive) if ran_real_probe else False
        err_kind = str(diag.get("err", "") or "")
        err_port = int(diag.get("port", 0) or 0)
        probes_n = int(diag.get("probes", 0) or 0)
        files_n = int(diag.get("files", 0) or 0)

        if not any_alive:
            # No alive Antigravity Hub process -> 0-debounce immediate offline transition
            self._active_hub_pid = 0
            self._active_hub_port = 0
            self._active_hub_token = ""
            self._active_hub_creation_ft = 0
            self._last_weekly_email = ""
            self._last_weekly_poll_ts = 0.0
            self._last_weekly_pools = {}
            self._any_hub_pid_alive = False
            self._account_rpc_fail_count = 0
            self._account_cache = AccountQuotaInfo(observed_at=now, quota_family=active_family, online=False)
            self._write_diag_log(
                now,
                "offline",
                (
                    f"online=0 hub_pid_alive=0 fail=0 port={err_port} "
                    f"err={err_kind or 'none'} probes={probes_n} files={files_n}"
                ),
                has_error=bool(err_kind),
            )
            return

        # Live Antigravity Hub PID detected -> protect cached Hub & weekly data, debounce transient RPC blips
        # If the previously cached Hub PID actually exited (another Hub PID is alive), clear stale cached Hub
        # fields immediately so hub_dropped does not hammer full scans every poll cycle and old account data
        # is never held in grace for a dead process.
        cached_hub_dead = (
            self._active_hub_pid > 0
            and not self._is_cached_hub_still_same_process()
        )
        if cached_hub_dead:
            self._active_hub_pid = 0
            self._active_hub_port = 0
            self._active_hub_token = ""
            self._active_hub_creation_ft = 0
        self._any_hub_pid_alive = True
        self._account_rpc_fail_count += 1
        within_grace = (
            not cached_hub_dead
            and self._account_cache.online
            and self._last_account_rpc_ok_ts > 0.0
            and self._account_rpc_fail_count < 3
            and (now - self._last_account_rpc_ok_ts) < 15.0
        )
        if within_grace:
            if self._account_cache.quota_family != active_family:
                if self._account_cache.pools:
                    self._sync_account_active_pool(self._account_cache, active_family)
                else:
                    self._account_cache.quota_family = active_family
            self._write_diag_log(
                now,
                "grace",
                (
                    f"online=1(grace) hub_pid_alive=1 fail={self._account_rpc_fail_count} "
                    f"port={err_port or self._active_hub_port} err={err_kind or 'rpc_fail'} "
                    f"probes={probes_n} files={files_n}"
                ),
                has_error=True,
            )
        else:
            self._account_cache = AccountQuotaInfo(observed_at=now, quota_family=active_family, online=False)
            self._write_diag_log(
                now,
                "wait_pid_alive",
                (
                    f"online=0 hub_pid_alive=1 fail={self._account_rpc_fail_count} "
                    f"port={err_port or self._active_hub_port} err={err_kind or 'rpc_fail'} "
                    f"probes={probes_n} files={files_n}"
                ),
                has_error=True,
            )

    def _probe_active_hub_account_fast(self) -> Optional[str]:
        """Lightweight <3ms check of the cached Hub's current masked email without full hub scan or Cockpit DB reads."""
        port = self._active_hub_port
        token = self._active_hub_token
        if not (port and token and self._is_cached_hub_still_same_process()):
            return None
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": token,
                    "Connect-Protocol-Version": "1",
                },
            )
            with _open_localhost_url(req, timeout=0.8) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            live_email = str((payload.get("userStatus") or {}).get("email", "") or "")
            return short_email_label(live_email) if live_email else None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError):
            return None

    def _poll_locked(
        self,
        now_ts: Optional[float] = None,
        force_account: bool = False,
        target_conversation_id: str = "",
        view_account: str = "",
        history_range: str = "30m",
    ) -> MonitorSnapshot:
        self._poll_seq += 1
        self._poll_yield_budget_remaining = 2.0
        self._in_poll = True
        try:
            return self._poll_locked_body(
                now_ts=now_ts,
                force_account=force_account,
                target_conversation_id=target_conversation_id,
                view_account=view_account,
                history_range=history_range,
            )
        finally:
            self._in_poll = False

    def _poll_locked_body(
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

        # Poll account liveness first so switch ledger & per-account step attribution stay up-to-date.
        # Reduce online interval to 5s (preserves all mock-online tests where now - _last_account_poll == 0)
        # and fast-probe cached Hub when Cockpit timeline just recorded a recent switch (<120s).
        prev_acct_label = self._account_cache.short_label if self._account_cache.online else ""
        prev_poll_ts = self._last_account_poll
        hub_dropped = (
            (self._account_cache.online or self._any_hub_pid_alive)
            and self._active_hub_pid > 0
            and not self._is_cached_hub_still_same_process()
        )
        account_interval = 5.0
        need_account_poll = force_account or hub_dropped or (now - self._last_account_poll >= account_interval)
        if not need_account_poll and self._account_cache.online and (now - self._last_account_poll >= 1.0):
            quick_tl = self._ledger.get_timeline(now - 3600.0)
            if quick_tl:
                last_tl_ts, last_tl_lbl = quick_tl[-1]
                if 0.0 <= (now - last_tl_ts) <= 120.0 and last_tl_lbl.lower() != prev_acct_label.lower():
                    fast_lbl = self._probe_active_hub_account_fast()
                    if fast_lbl is None or fast_lbl.lower() != prev_acct_label.lower():
                        need_account_poll = True
        if need_account_poll:
            self._refresh_account_cache(now, self._last_quota_family)
            if self._account_cache.online and self._account_cache.short_label:
                hint_ts = None
                if prev_acct_label and self._account_cache.short_label.lower() != prev_acct_label.lower():
                    hint_ts = self._find_recent_cold_switch_hint(max(prev_poll_ts, now - 60.0), now)
                self._ledger.observe_online(self._account_cache.short_label, now, hint_switch_ts=hint_ts)

        today_start = datetime.datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        win_sec = float(max(1, int(self.config["log_sliding_window_min"])) * 60)

        timeline = self._ledger.get_timeline(min(today_start, now - 7 * 86400.0))
        timeline_times = [t for t, _ in timeline]
        cur_acct_label = self._account_cache.short_label if self._account_cache.online else ""
        available_accounts = self._ledger.list_known_accounts(
            now - 7 * 86400.0,
            current_label=cur_acct_label,
            timeline=timeline,
        )

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
            if is_custom_account_view:
                range_start_ts = now - win_sec
                lookback_cutoff = min(today_start - 3600.0, now - 48 * 3600.0)
                max_older_convs = 48
            else:
                range_start_ts = 0.0
                if acct_has_older_tl:
                    lookback_cutoff = now - 7 * 86400.0
                    max_older_convs = 160
                else:
                    lookback_cutoff = min(today_start - 3600.0, now - 48 * 3600.0)
                    max_older_convs = 48

        switch_timestamps = self._ledger.get_switch_timestamps(lookback_cutoff, timeline=timeline)
        rule_timestamps = self._observe_and_get_rule_timestamps(now, since_ts=0.0)
        current_acct_since_ts = timeline[-1][0] if (resolved_account and not is_custom_account_view and len(timeline) >= 2) else 0.0

        target_query = (target_conversation_id or "").strip()
        target_query_low = target_query.lower()

        rolling_24h_cutoff = now - 86400.0
        hot_cutoff = min(today_start, rolling_24h_cutoff) - 60.0
        all_recent = self._iter_recent_transcripts(
            lookback_cutoff,
            today_start,
            max_older=max_older_convs,
            hot_cutoff=hot_cutoff,
        )
        convs_bundle: List[Tuple[ConversationSummary, List[StepRecord], List[int]]] = []
        carryover_candidates: List[Tuple[float, str, List[Dict[str, Any]], bool, bool]] = []
        target_bundle: Optional[Tuple[ConversationSummary, List[StepRecord], List[int]]] = None
        collected_main_count = 0
        has_any_live_transcript = False
        latest_host_transcript_ts = 0.0
        want_carryover = bool(
            (not is_custom_account_view)
            and hr_key == "30m"
            and self._account_cache.online
            and acct_filter_fn is not None
        )

        total_today_steps = 0
        total_main_today_steps = 0
        total_subagent_today_steps = 0
        account_today_steps = 0
        account_main_today_steps = 0
        account_subagent_today_steps = 0
        total_24h_steps = 0
        total_main_24h_steps = 0
        total_subagent_24h_steps = 0
        account_24h_steps = 0
        account_main_24h_steps = 0
        account_subagent_24h_steps = 0
        range_steps = 0
        last_1h_steps = 0
        hourly_counts: Dict[int, int] = {}
        hourly_24h_local_counts: Dict[Tuple[int, int, int, int], int] = {}
        account_24h_utc_buckets: Dict[int, int] = {}
        one_hour_cutoff = now - 3600.0
        step_upper_bound = now + 5.0
        eff_range_step_cutoff = range_start_ts if range_start_ts > 0.0 else (now - win_sec)
        quiet_cutoff_ts = now - max(600.0, float(self.config.get("freshness_window_sec", 210)) + 305.0)
        four_bounds = (today_start, rolling_24h_cutoff, one_hour_cutoff, eff_range_step_cutoff)
        agg_env_sig = (
            resolved_account.lower(),
            bool(is_custom_account_view),
            hr_key,
            len(timeline),
            timeline[-1] if timeline else None,
            cur_acct_label.lower(),
            round(current_acct_since_ts, 1),
            tuple(round(t, 1) for t in switch_timestamps[-24:]),
            tuple(round(t, 1) for t in rule_timestamps[-64:]),
            int(self.config["idle_rewarm_sec"]),
            int(self.config.get("min_cacheable_tokens", 32000)),
            int(self.config.get("large_context_threshold", 60000)),
            len(self._model_enum_map),
        )

        def _boundary_pos(min_ts_v: float, max_ts_v: float, bound_ts: float) -> int:
            if bound_ts <= min_ts_v:
                return 0
            if bound_ts > max_ts_v:
                return 2
            return 1

        def _clone_summary_tpl(
            tpl: ConversationSummary,
            scoped_tpl: List[StepRecord],
        ) -> ConversationSummary:
            return replace(
                tpl,
                recent_steps=list(tpl.recent_steps),
                recent_ttfts=list(tpl.recent_ttfts),
                recent_tps_list=list(tpl.recent_tps_list),
                _scoped_steps_all=list(scoped_tpl),
                _scoped_gens_all=[],
            )

        for idx_conv, (conv_id, tp, prefetched_stamp) in enumerate(all_recent):
            self._maybe_yield_for_ui()
            raw_steps, has_user_explicit, has_subagent_marker = self._read_transcript_cached(
                conv_id,
                tp,
                prefetched_stamp=prefetched_stamp,
            )
            if not raw_steps:
                continue

            last_step_raw = raw_steps[-1]
            last_raw_ts = float(
                last_step_raw.timestamp
                if isinstance(last_step_raw, _RawStepSlot)
                else (last_step_raw.get("timestamp", 0.0) or 0.0)
            )
            inc_meta_ts = self._transcript_inc_state.get(conv_id)
            if inc_meta_ts is not None:
                inc_max_ts = float(inc_meta_ts.get("max_step_ts", 0.0) or 0.0)
                if inc_max_ts > last_raw_ts:
                    last_raw_ts = inc_max_ts
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

            matches_target = bool(
                target_query_low
                and (conv_id.lower() == target_query_low or conv_id.lower().startswith(target_query_low))
            )
            scan_limit = 96 if (acct_filter_fn is not None or hr_key != "30m") else 24
            main_collect_cap = 24 if hr_key in ("today", "7d") else 16
            want_summary = (
                idx_conv < scan_limit
                or (not is_sub_conv and collected_main_count < main_collect_cap)
                or matches_target
            )

            is_quiet_static = (not matches_target) and (last_raw_ts > 0.0) and (last_raw_ts < quiet_cutoff_ts)
            agg_key = (prefetched_stamp, is_sub_conv, agg_env_sig)
            cached_slot = self._static_conv_agg_cache.get(conv_id) if is_quiet_static else None
            cached_agg = cached_slot[1] if (cached_slot is not None and cached_slot[0] == agg_key) else None
            agg_hit = False
            if cached_agg is not None:
                c_min_ts = cached_agg["min_ts"]
                c_max_ts = cached_agg["max_ts"]
                if c_min_ts is not None and c_max_ts is not None and c_max_ts <= step_upper_bound:
                    cur_bstate = tuple(_boundary_pos(c_min_ts, c_max_ts, b) for b in four_bounds)
                    if (1 not in cur_bstate) and cur_bstate == cached_agg["bstate"]:
                        agg_hit = True
                elif c_min_ts is None and c_max_ts is None and cached_agg["bstate"] == ():
                    agg_hit = True

            if agg_hit and cached_agg is not None:
                self._static_conv_agg_cache.move_to_end(conv_id)
                total_today_steps += cached_agg["d_total_today"]
                total_main_today_steps += cached_agg["d_total_main_today"]
                total_subagent_today_steps += cached_agg["d_total_sub_today"]
                total_24h_steps += cached_agg["d_total_24h"]
                total_main_24h_steps += cached_agg["d_total_main_24h"]
                total_subagent_24h_steps += cached_agg["d_total_sub_24h"]
                account_today_steps += cached_agg["d_acct_today"]
                account_main_today_steps += cached_agg["d_acct_main_today"]
                account_subagent_today_steps += cached_agg["d_acct_sub_today"]
                account_24h_steps += cached_agg["d_acct_24h"]
                account_main_24h_steps += cached_agg["d_acct_main_24h"]
                account_subagent_24h_steps += cached_agg["d_acct_sub_24h"]
                range_steps += cached_agg["d_range_steps"]
                last_1h_steps += cached_agg["d_last_1h_steps"]
                for hr_k, cnt_v in cached_agg["d_hourly_counts"]:
                    hourly_counts[hr_k] = hourly_counts.get(hr_k, 0) + cnt_v
                for lkey_k, cnt_v in cached_agg["d_hourly_24h_local"]:
                    hourly_24h_local_counts[lkey_k] = hourly_24h_local_counts.get(lkey_k, 0) + cnt_v
                for hk_k, cnt_v in cached_agg["d_acct_24h_utc"]:
                    account_24h_utc_buckets[hk_k] = account_24h_utc_buckets.get(hk_k, 0) + cnt_v

                if not want_summary:
                    continue
                sum_ent = cached_agg["summary_entry"]
                if sum_ent is None:
                    built_now = self._build_conversation_summary(
                        conv_id=conv_id,
                        raw_steps=raw_steps,
                        has_user_explicit=has_user_explicit,
                        has_subagent_marker=has_subagent_marker or is_sub_conv,
                        now=now,
                        switch_timestamps=switch_timestamps,
                        current_acct_since_ts=0.0 if acct_filter_fn is not None else current_acct_since_ts,
                        account_filter_fn=acct_filter_fn,
                        range_start_ts=range_start_ts,
                        rule_change_timestamps=rule_timestamps,
                        resolve_title=False,
                    )
                    if built_now is None:
                        cached_agg["summary_entry"] = ("empty",)
                        continue
                    scoped_tpl = list(built_now[0]._scoped_steps_all)
                    tpl_copy = replace(
                        built_now[0],
                        recent_steps=list(built_now[0].recent_steps),
                        recent_ttfts=list(built_now[0].recent_ttfts),
                        recent_tps_list=list(built_now[0].recent_tps_list),
                        _scoped_steps_all=[],
                        _scoped_gens_all=[],
                    )
                    cached_agg["summary_entry"] = ("built", tpl_copy, built_now[1], built_now[2], scoped_tpl)
                    built = built_now
                elif sum_ent[0] == "empty":
                    continue
                else:
                    _tag, tpl_copy, all_st_c, sc_idx_c, scoped_tpl = sum_ent
                    built = (_clone_summary_tpl(tpl_copy, scoped_tpl), all_st_c, sc_idx_c)
                convs_bundle.append(built)
                if not built[0].is_subagent:
                    collected_main_count += 1
                continue

            d_total_today = 0
            d_total_main_today = 0
            d_total_sub_today = 0
            d_total_24h = 0
            d_total_main_24h = 0
            d_total_sub_24h = 0
            d_acct_today = 0
            d_acct_main_today = 0
            d_acct_sub_today = 0
            d_acct_24h = 0
            d_acct_main_24h = 0
            d_acct_sub_24h = 0
            d_range_steps = 0
            d_last_1h_steps = 0
            d_hourly: Dict[int, int] = {}
            d_hourly_24h: Dict[Tuple[int, int, int, int], int] = {}
            d_acct_utc: Dict[int, int] = {}
            c_min_ts: Optional[float] = None
            c_max_ts: Optional[float] = None

            for s in raw_steps:
                sts = float(s.timestamp if isinstance(s, _RawStepSlot) else (s.get("timestamp", 0.0) or 0.0))
                if sts <= 0.0 or sts > step_upper_bound:
                    continue
                if c_min_ts is None or sts < c_min_ts:
                    c_min_ts = sts
                if c_max_ts is None or sts > c_max_ts:
                    c_max_ts = sts
                if sts >= today_start:
                    d_total_today += 1
                    if is_sub_conv:
                        d_total_sub_today += 1
                    else:
                        d_total_main_today += 1
                if sts >= rolling_24h_cutoff:
                    d_total_24h += 1
                    if is_sub_conv:
                        d_total_sub_24h += 1
                    else:
                        d_total_main_24h += 1

                in_scope = True if acct_filter_fn is None else acct_filter_fn(sts)
                if not in_scope:
                    continue

                if sts >= eff_range_step_cutoff:
                    d_range_steps += 1
                if sts >= one_hour_cutoff:
                    d_last_1h_steps += 1
                if sts >= today_start:
                    if resolved_account:
                        d_acct_today += 1
                        if is_sub_conv:
                            d_acct_sub_today += 1
                        else:
                            d_acct_main_today += 1
                    hr = datetime.datetime.fromtimestamp(sts).hour
                    d_hourly[hr] = d_hourly.get(hr, 0) + 1
                if sts >= rolling_24h_cutoff:
                    if resolved_account:
                        d_acct_24h += 1
                        if is_sub_conv:
                            d_acct_sub_24h += 1
                        else:
                            d_acct_main_24h += 1
                        hk_utc = int(sts // 3600)
                        d_acct_utc[hk_utc] = d_acct_utc.get(hk_utc, 0) + 1
                    dt_s = datetime.datetime.fromtimestamp(sts)
                    lkey = (dt_s.year, dt_s.month, dt_s.day, dt_s.hour)
                    d_hourly_24h[lkey] = d_hourly_24h.get(lkey, 0) + 1

            total_today_steps += d_total_today
            total_main_today_steps += d_total_main_today
            total_subagent_today_steps += d_total_sub_today
            total_24h_steps += d_total_24h
            total_main_24h_steps += d_total_main_24h
            total_subagent_24h_steps += d_total_sub_24h
            account_today_steps += d_acct_today
            account_main_today_steps += d_acct_main_today
            account_subagent_today_steps += d_acct_sub_today
            account_24h_steps += d_acct_24h
            account_main_24h_steps += d_acct_main_24h
            account_subagent_24h_steps += d_acct_sub_24h
            range_steps += d_range_steps
            last_1h_steps += d_last_1h_steps
            for hr_k, cnt_v in d_hourly.items():
                hourly_counts[hr_k] = hourly_counts.get(hr_k, 0) + cnt_v
            for lkey_k, cnt_v in d_hourly_24h.items():
                hourly_24h_local_counts[lkey_k] = hourly_24h_local_counts.get(lkey_k, 0) + cnt_v
            for hk_k, cnt_v in d_acct_utc.items():
                account_24h_utc_buckets[hk_k] = account_24h_utc_buckets.get(hk_k, 0) + cnt_v

            summary_cache_ent: Any = None
            built = None
            if want_summary:
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
                    rule_change_timestamps=rule_timestamps,
                    resolve_title=False,
                )
                if built is None:
                    summary_cache_ent = ("empty",)
                else:
                    scoped_tpl = list(built[0]._scoped_steps_all)
                    tpl_copy = replace(
                        built[0],
                        recent_steps=list(built[0].recent_steps),
                        recent_ttfts=list(built[0].recent_ttfts),
                        recent_tps_list=list(built[0].recent_tps_list),
                        _scoped_steps_all=[],
                        _scoped_gens_all=[],
                    )
                    summary_cache_ent = ("built", tpl_copy, built[1], built[2], scoped_tpl)

            if is_quiet_static:
                bstate = (
                    tuple(_boundary_pos(c_min_ts, c_max_ts, b) for b in four_bounds)
                    if (c_min_ts is not None and c_max_ts is not None)
                    else ()
                )
                if 1 not in bstate:
                    self._static_conv_agg_cache[conv_id] = (
                        agg_key,
                        {
                            "min_ts": c_min_ts,
                            "max_ts": c_max_ts,
                            "bstate": bstate,
                            "d_total_today": d_total_today,
                            "d_total_main_today": d_total_main_today,
                            "d_total_sub_today": d_total_sub_today,
                            "d_total_24h": d_total_24h,
                            "d_total_main_24h": d_total_main_24h,
                            "d_total_sub_24h": d_total_sub_24h,
                            "d_acct_today": d_acct_today,
                            "d_acct_main_today": d_acct_main_today,
                            "d_acct_sub_today": d_acct_sub_today,
                            "d_acct_24h": d_acct_24h,
                            "d_acct_main_24h": d_acct_main_24h,
                            "d_acct_sub_24h": d_acct_sub_24h,
                            "d_range_steps": d_range_steps,
                            "d_last_1h_steps": d_last_1h_steps,
                            "d_hourly_counts": tuple(d_hourly.items()),
                            "d_hourly_24h_local": tuple(d_hourly_24h.items()),
                            "d_acct_24h_utc": tuple(d_acct_utc.items()),
                            "summary_entry": summary_cache_ent,
                        },
                    )
                    self._static_conv_agg_cache.move_to_end(conv_id)
                    while len(self._static_conv_agg_cache) > 256:
                        self._static_conv_agg_cache.popitem(last=False)

            if not want_summary:
                continue
            if built is None:
                if want_carryover and (not is_sub_conv) and (now - last_raw_ts) <= 600.0:
                    carryover_candidates.append((last_raw_ts, conv_id, raw_steps, has_user_explicit, has_subagent_marker or is_sub_conv))
                continue
            # Also track live carryover preview if this main conv was active in last 10m under another account,
            # while current account has no step in the last 10m (e.g. switched back to an older account).
            if (
                want_carryover
                and (not built[0].is_subagent)
                and (now - last_raw_ts) <= 600.0
                and (now - built[0].latest_timestamp) > 600.0
            ):
                carryover_candidates.append((last_raw_ts, conv_id, raw_steps, has_user_explicit, has_subagent_marker or is_sub_conv))
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
                        rule_change_timestamps=rule_timestamps,
                        resolve_title=False,
                    )

        convs_bundle.sort(key=lambda b: b[0].latest_timestamp, reverse=True)
        main_protect_cap = 24 if hr_key in ("today", "7d") else 12
        top_bundle_cap = 48 if hr_key in ("today", "7d") else 24
        protected_mains = [b for b in convs_bundle if not b[0].is_subagent][:main_protect_cap]
        seen_ids = {b[0].conversation_id for b in protected_mains}
        top_bundles: List[Tuple[ConversationSummary, List[StepRecord], List[int]]] = list(protected_mains)
        for b in convs_bundle:
            if len(top_bundles) >= top_bundle_cap:
                break
            if b[0].conversation_id not in seen_ids:
                top_bundles.append(b)
                seen_ids.add(b[0].conversation_id)
        top_bundles.sort(key=lambda b: b[0].latest_timestamp, reverse=True)

        is_acct_scoped = acct_filter_fn is not None
        need_wide_gens = hr_key != "30m"
        for idx_tb, (c, all_st, sc_idx) in enumerate(top_bundles):
            if not c.title:
                c.title = self._resolve_conv_title(c.conversation_id)
            use_wide_for_c = bool(
                need_wide_gens
                and (idx_tb < 24 or c.abnormal_in_recent10 > 0 or c.consec_miss > 0)
            )
            self._enrich_conv_from_db(
                c,
                all_steps=all_st,
                scoped_indices=sc_idx,
                account_scoped=is_acct_scoped,
                need_full_window_gens=use_wide_for_c,
            )
        target_conv: Optional[ConversationSummary] = target_bundle[0] if target_bundle is not None else None
        if target_conv is not None and not target_conv.title:
            target_conv.title = self._resolve_conv_title(target_conv.conversation_id)
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
                self._refresh_account_cache(now, active_family)
                if self._account_cache.online and self._account_cache.short_label:
                    self._ledger.observe_online(self._account_cache.short_label, now)

        if resolved_account:
            steps_scope_label = resolved_account
            log_scope_label = resolved_account
            today_steps = account_today_steps
            main_today_steps = account_main_today_steps
            subagent_today_steps = account_subagent_today_steps
            rolling_24h_steps = account_24h_steps
            main_24h_steps = account_main_24h_steps
            subagent_24h_steps = account_subagent_24h_steps
        else:
            steps_scope_label = "全机"
            log_scope_label = "全机"
            today_steps = total_today_steps
            main_today_steps = total_main_today_steps
            subagent_today_steps = total_subagent_today_steps
            rolling_24h_steps = total_24h_steps
            main_24h_steps = total_main_24h_steps
            subagent_24h_steps = total_subagent_24h_steps

        peak_hour_steps = 0
        peak_hour_label = ""
        if hourly_counts:
            peak_hr, peak_hour_steps = max(hourly_counts.items(), key=lambda kv: (kv[1], kv[0]))
            peak_hour_label = f"{peak_hr:02d}:00-{(peak_hr + 1) % 24:02d}:00"

        peak_hour_steps_24h = 0
        peak_hour_label_24h = ""
        if hourly_24h_local_counts:
            today_date_tup = datetime.datetime.fromtimestamp(now).timetuple()[:3]
            peak_lk, peak_hour_steps_24h = max(hourly_24h_local_counts.items(), key=lambda kv: (kv[1], kv[0]))
            p_y, p_m, p_d, p_h = peak_lk
            span_txt = f"{p_h:02d}:00-{(p_h + 1) % 24:02d}:00"
            peak_hour_label_24h = f"昨日 {span_txt}" if (p_y, p_m, p_d) < today_date_tup else span_txt

        # Large-context freshness applies to live current-account monitoring (including when online user inspects 6h/today/7d)
        if is_custom_account_view or (
            not self._account_cache.online and (hr_key != "30m" or not has_any_live_transcript)
        ):
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
            for _fc in large_freshness[:12]:
                if not _fc.title:
                    _fc.title = self._resolve_conv_title(_fc.conversation_id)
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
                max_notable=None,
            )
            live_active = [c for c in top_convs if c.latest_timestamp >= live_win_start]
            has_live_acct_activity = bool(live_active) or (live_log["503"] + live_log["timeout"] + live_log["403"] > 0)
            if (not has_live_acct_activity) and self._account_cache.online and (not is_custom_account_view):
                # Do not fall back to stale historical T0 when the user returns after >30m and hits an immediate proxy outage
                probe_cnt, _probe_s, probe_e, _probe_b = self._log_tailer.get_proxy_outage_info(
                    now,
                    self.config["log_sliding_window_min"],
                    account_filter_fn=acct_filter_fn,
                    range_start_ts=live_win_start,
                    range_end_ts=now,
                )
                if probe_cnt > 0 and probe_e > 0.0 and (now - probe_e) <= 900.0:
                    has_live_acct_activity = True

            if has_live_acct_activity:
                # Recompute miss counters strictly inside current live session floor so yesterday's misses never leak into today's live T1!
                for c in live_active:
                    self._recompute_conv_eval_stats(c, live_eval_floor)
                recent_active = live_active
                recent_log = live_log
                notable_events = live_notable
                eval_cutoff_ts = live_eval_floor
                eval_end_ts = now
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
                        p_log, _, p_notable_raw = self._log_tailer.get_counts(
                            now,
                            self.config["log_sliding_window_min"],
                            account_filter_fn=acct_filter_fn,
                            range_start_ts=p_win_s,
                            range_end_ts=p_anchor,
                            max_notable=None,
                        )
                        p_notable_ann = self._annotate_log_events_with_conversations(p_notable_raw, top_convs)
                        p_heavy_to = sum(
                            1 for ev in p_notable_ann if ev.category == "timeout" and ev.is_heavy_media_timeout
                        )
                        p_server_to = max(0, p_log["timeout"] - p_heavy_to)
                        p_consec = 0
                        p_freq_t1 = False
                        for c in top_convs:
                            p_st = [s for s in c._scoped_steps_all if p_win_s <= s.timestamp <= p_anchor + 2.0]
                            cm = 0
                            for s in reversed(p_st):
                                if s.classification == "abnormal_miss":
                                    cm += 1
                                else:
                                    break
                            p_consec = max(p_consec, cm)
                            p_r10 = p_st[-10:]
                            p_abn = sum(1 for s in p_r10 if s.classification == "abnormal_miss")
                            p_hit = sum(1 for s in p_r10 if s.classification == "hit")
                            p_cach = p_abn + p_hit
                            if p_abn >= 4 and p_cach >= 4 and (p_hit / float(p_cach)) < 0.5:
                                p_freq_t1 = True
                        if p_log["403"] > 0:
                            prior_hist_warning = f"前序历史曾触发 403（{format_relative_age(now - p_anchor)}），首步冷启尚未验真"
                        elif (p_server_to + p_log["503"]) >= 2:
                            prior_hist_warning = f"前序历史曾触发 T2 限流（{format_relative_age(now - p_anchor)}），首步冷启待次拍验真"
                        elif p_consec >= T1_CONSEC_MISS_THRESHOLD:
                            prior_hist_warning = f"前序历史曾连续 {p_consec} 步掉缓存 T1（{format_relative_age(now - p_anchor)}），首步冷启待次拍验真"
                        elif p_freq_t1:
                            prior_hist_warning = f"前序历史曾频繁掉缓存 T1（{format_relative_age(now - p_anchor)}），首步冷启待次拍验真"
                if is_custom_account_view:
                    is_historical_rating = True
                    historical_tag = f"历史视角 · {resolved_account or '全机'}"
            else:
                if is_custom_account_view:
                    recent_active = []
                    recent_log = live_log
                    notable_events = []
                    eval_cutoff_ts = live_win_start
                    eval_end_ts = now
                    is_historical_rating = True
                    rating_anchor_ts = now
                    historical_tag = f"{hr_label}汇总"
                    last_t2_error_ts = 0.0
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
                            max_notable=None,
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
                max_notable=None,
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

        # Refresh top-level min_freshness_invalid_reason after _recompute_conv_eval_stats so it always matches consec_miss & grade!
        if large_freshness:
            min_fresh_invalid = large_freshness[0].freshness_invalid_reason

        eval_mains = [c for c in recent_active if not c.is_subagent]
        eval_primary = eval_mains[0] if eval_mains else (recent_active[0] if recent_active else None)
        active_model = eval_primary.model if eval_primary else "等待请求"
        primary_hit = eval_primary.latest_hit_rate if eval_primary else None
        primary_last_step = eval_primary.recent_steps[-1] if (eval_primary and eval_primary.recent_steps) else None
        primary_hit_cls = primary_last_step.classification if primary_last_step is not None else ""
        primary_hit_reason = eval_primary.latest_cache_reason if eval_primary else ""
        primary_hit_is_compact = bool(primary_last_step.is_compact) if primary_last_step is not None else False
        primary_hit_is_revert = bool(primary_last_step.is_revert) if primary_last_step is not None else False
        primary_tps = eval_primary.tps if eval_primary else None
        primary_ttft = eval_primary.ttft_sec if eval_primary else None
        primary_speed = eval_primary.speed_status if eval_primary else "unknown"

        # Build read-only carryover previews for main windows active in the last 10m under a prior account
        # (strictly excluded from active_conversations, health rating, freshness, steps, and model_stats).
        carryover_convs: List[ConversationSummary] = []
        if carryover_candidates:
            carryover_candidates.sort(key=lambda item: item[0], reverse=True)
            seen_co: set[str] = set()
            for _lts, co_cid, co_raw, co_ue, co_sm in carryover_candidates:
                if co_cid in seen_co:
                    continue
                seen_co.add(co_cid)
                co_built = self._build_conversation_summary(
                    conv_id=co_cid,
                    raw_steps=co_raw,
                    has_user_explicit=co_ue,
                    has_subagent_marker=co_sm,
                    now=now,
                    switch_timestamps=switch_timestamps,
                    current_acct_since_ts=0.0,
                    account_filter_fn=None,
                    range_start_ts=0.0,
                    rule_change_timestamps=rule_timestamps,
                )
                if co_built is not None:
                    co_c = co_built[0]
                    self._enrich_conv_from_db(co_c, all_steps=co_built[1], scoped_indices=co_built[2], account_scoped=False)
                    co_c.is_carryover_preview = True
                    co_c.freshness_remaining_sec = None
                    co_c.freshness_invalid_reason = ""
                    carryover_convs.append(co_c)
                    if len(carryover_convs) >= 2:
                        break

        max_consec_miss, any_single_miss, severe_frequent_miss_conv, unstable_recovery_conv = (
            self._select_cache_health_signals(
                recent_active,
                now_ts=now,
                eval_cutoff_ts=eval_cutoff_ts,
                eval_end_ts=eval_end_ts,
                is_historical_rating=is_historical_rating,
                hr_key=hr_key,
                is_custom_account_view=is_custom_account_view,
                is_all_accounts_mixed=(raw_view == "__all__"),
            )
        )
        any_slow = any(c.speed_status == "slow" for c in recent_active[:2])
        wasted_total = sum(c.wasted_cold_tokens for c in recent_active)
        max_abnormal_r10 = max((c.abnormal_in_recent10 for c in recent_active), default=0)
        ratios = [c.recent10_hit_ratio for c in recent_active if c.cacheable_in_recent10 >= 2 and c.recent10_hit_ratio is not None]
        worst_hit_ratio = min(ratios) if ratios else None

        annotated_notable = self._annotate_log_events_with_conversations(notable_events, top_convs)
        heavy_media_timeout_count = sum(
            1 for ev in annotated_notable if ev.category == "timeout" and ev.is_heavy_media_timeout
        )
        # Recompute last_t2_error_ts strictly excluding heavy-media upload timeouts so they never reset T2 recovery!
        last_t2_error_ts = max(
            (
                ev.timestamp
                for ev in annotated_notable
                if ev.category == "503" or (ev.category == "timeout" and not ev.is_heavy_media_timeout)
            ),
            default=0.0,
        )
        notable_events = annotated_notable[-3:]
        latest_server_timeout_ev = next(
            (ev for ev in reversed(annotated_notable) if ev.category == "timeout" and not ev.is_heavy_media_timeout),
            None,
        )
        latest_heavy_timeout_ev = next(
            (ev for ev in reversed(annotated_notable) if ev.category == "timeout" and ev.is_heavy_media_timeout),
            None,
        )
        latest_timeout_summary = latest_server_timeout_ev.summary if latest_server_timeout_ev is not None else ""
        latest_heavy_timeout_summary = latest_heavy_timeout_ev.summary if latest_heavy_timeout_ev is not None else ""

        slot_inv = self.read_slot_inventory(now_ts=now, include_items=False, force=False)

        model_stats = self._compute_recent_model_stats(
            recent_active=recent_active,
            cutoff_ts=eval_cutoff_ts,
            last_t2_error_ts=last_t2_error_ts,
            use_full_scoped=(hr_key != "30m"),
            enum_map=self._model_enum_map,
            slow_tps_th=float(self.config["slow_tps_threshold"]),
            slow_ttft_th=float(self.config["slow_ttft_threshold_sec"]),
        )

        eff_online = bool(self._account_cache.online or is_custom_account_view)
        is_live_30m = bool(
            (not is_historical_rating)
            and (not is_custom_account_view)
            and hr_key == "30m"
            and self._account_cache.online
        )

        # Identify primary cache-miss culprit conversation & whether stale historical T1 was downgraded to WARN
        cache_culprit_conv: Optional[ConversationSummary] = None
        if max_consec_miss >= T1_CONSEC_MISS_THRESHOLD:
            consec_cands = [c for c in recent_active if c.consec_miss == max_consec_miss]
            if consec_cands:
                consec_cands.sort(key=lambda c: (0 if not c.is_subagent else 1, -c.latest_timestamp))
                cache_culprit_conv = consec_cands[0]
        elif severe_frequent_miss_conv is not None:
            cache_culprit_conv = severe_frequent_miss_conv
        elif max_consec_miss >= 2:
            consec_cands = [c for c in recent_active if c.consec_miss == max_consec_miss]
            if consec_cands:
                consec_cands.sort(key=lambda c: (0 if not c.is_subagent else 1, -c.latest_timestamp))
                cache_culprit_conv = consec_cands[0]
        elif any_single_miss:
            sm_cands = [c for c in recent_active if c.single_miss]
            if sm_cands:
                if is_live_30m:
                    fresh_sm = [
                        c for c in sm_cands
                        if c.last_abnormal_ts <= 0.0 or (eval_end_ts - c.last_abnormal_ts) < 600.0
                    ]
                    if fresh_sm:
                        sm_cands = fresh_sm
                sm_cands.sort(key=lambda c: (0 if not c.is_subagent else 1, -c.latest_timestamp))
                cache_culprit_conv = sm_cands[0]
        elif unstable_recovery_conv is not None:
            cache_culprit_conv = unstable_recovery_conv
        cache_culprit_summary = self._format_cache_culprit_summary(cache_culprit_conv)
        downgraded_stale_t1 = bool(
            is_historical_rating
            and hr_key == "7d"
            and (raw_view != "__all__")
            and severe_frequent_miss_conv is None
            and unstable_recovery_conv is not None
            and unstable_recovery_conv.consec_miss == 0
            and unstable_recovery_conv.abnormal_in_recent10 >= 4
            and unstable_recovery_conv.cacheable_in_recent10 >= 4
            and (
                unstable_recovery_conv.recent10_hit_ratio is not None
                and unstable_recovery_conv.recent10_hit_ratio < 0.5
            )
        )

        # Module 3: Account- & range-isolated proxy outage burst detection (strictly silent on IDLE & historical views)
        proxy_outage_recent = 0
        proxy_outage_active = False
        proxy_outage_note = ""
        if eff_online:
            outage_cnt, _outage_s, outage_e, _outage_b = self._log_tailer.get_proxy_outage_info(
                now,
                self.config["log_sliding_window_min"],
                account_filter_fn=acct_filter_fn,
                range_start_ts=eval_cutoff_ts,
                range_end_ts=eval_end_ts,
            )
            if outage_cnt > 0 and outage_e > 0.0:
                hm_outage = datetime.datetime.fromtimestamp(outage_e).strftime("%H:%M")
                if is_live_30m:
                    age_since_outage = now - outage_e
                    if age_since_outage <= 15.0 * 60.0:
                        post_outage_steps = sum(
                            1
                            for c in recent_active
                            for s in c._scoped_steps_all
                            if s.timestamp > outage_e + 1.0
                        )
                        if age_since_outage >= 300.0 and post_outage_steps >= 6:
                            proxy_outage_active = False
                            proxy_outage_recent = 0
                            proxy_outage_note = f"前序代理断流 ({hm_outage} {outage_cnt}次跨接口断连) 已平复"
                        else:
                            proxy_outage_active = True
                            proxy_outage_recent = outage_cnt
                            proxy_outage_note = f"代理线路疑似断流 ({hm_outage} 60s内 {outage_cnt}次跨接口断连，非服务端限流)"
                else:
                    # Historical views NEVER escalate WARN for transient proxy blips; keep note read-only
                    proxy_outage_recent = outage_cnt
                    proxy_outage_note = f"{hm_outage} 曾发生 {outage_cnt} 次跨接口代理断连"

        # Module 5: Per-account adaptive daily step protection limits (strictly from completed prior days)
        today_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        base_steps_warn = int(self.config["daily_steps_warn"])
        base_steps_alert = int(self.config["daily_steps_alert"])
        eff_steps_warn = base_steps_warn
        eff_steps_alert = base_steps_alert
        steps_limit_mode = "default"
        steps_limit_note = "初始建议值"
        acct_hist_safe_peak = 0
        hist_days: List[Dict[str, Any]] = []
        recent_recov_steps = 0
        if resolved_account:
            hist_days = self._get_account_completed_daily_history(
                account_label=resolved_account,
                now=now,
                today_start=today_start,
                today_str=today_str,
                timeline=timeline,
                fallback_label=cur_acct_label,
            )
            merged_acct_hourly = self._merge_account_hourly_buckets(
                self._ledger.get_account_hourly_buckets(resolved_account),
                self._hist_hourly_buckets_cache.get(resolved_account.lower(), {}),
            )
            recov_hourly = dict(merged_acct_hourly)
            live_cur_h = int(now // 3600)
            live_left_h = int(rolling_24h_cutoff // 3600)
            for hk_live, cnt_live in account_24h_utc_buckets.items():
                if cnt_live > 0 and live_left_h < hk_live < live_cur_h:
                    prev_r = recov_hourly.get(hk_live)
                    if prev_r is None:
                        recov_hourly[hk_live] = {"steps": cnt_live, "degraded": False, "degraded_24h_at": None}
                    elif cnt_live > int(prev_r.get("steps", 0) or 0):
                        recov_hourly[hk_live] = {
                            "steps": cnt_live,
                            "degraded": bool(prev_r.get("degraded", False)),
                            "degraded_24h_at": prev_r.get("degraded_24h_at"),
                        }
            # Reflect today's live T1 degradations even under --readonly / cold-start before ledger persistence
            today_live_deg_hours: set[int] = set()
            for c_live in convs:
                steps_live = c_live._scoped_steps_all or c_live.recent_steps
                consec_abn = 0
                for st_live in steps_live:
                    if st_live.classification == "abnormal_miss":
                        consec_abn += 1
                        if consec_abn >= T1_CONSEC_MISS_THRESHOLD and st_live.timestamp >= today_start:
                            today_live_deg_hours.add(int(st_live.timestamp // 3600))
                    elif st_live.classification == "hit":
                        consec_abn = 0
                if (
                    c_live is severe_frequent_miss_conv
                    and c_live.last_abnormal_ts >= today_start
                ):
                    today_live_deg_hours.add(int(c_live.last_abnormal_ts // 3600))
            for deg_hk in today_live_deg_hours:
                prev_r = recov_hourly.get(deg_hk)
                recov_hourly[deg_hk] = {
                    "steps": max(
                        int(prev_r.get("steps", 0) or 0) if prev_r else 0,
                        int(account_24h_utc_buckets.get(deg_hk, 0) or 0),
                        1,
                    ),
                    "degraded": True,
                    "degraded_24h_at": prev_r.get("degraded_24h_at") if prev_r else None,
                }
            recent_recov_steps = self._compute_recent_unsettled_recovery_steps(recov_hourly, now)
            (
                eff_steps_warn,
                eff_steps_alert,
                steps_limit_mode,
                steps_limit_note,
                acct_hist_safe_peak,
            ) = compute_adaptive_daily_steps_limits(
                base_steps_warn,
                base_steps_alert,
                hist_days,
                recent_recovery_steps=recent_recov_steps,
            )

        hub_pid_alive_val = bool(self._account_cache.online or self._any_hub_pid_alive)
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
            has_live_transcript=(has_any_live_transcript if hr_key == "30m" else False),
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
            heavy_media_timeout_count=heavy_media_timeout_count,
            latest_heavy_timeout_summary=latest_heavy_timeout_summary,
            proxy_outage_active=proxy_outage_active,
            proxy_outage_note=proxy_outage_note,
            daily_steps_warn_override=eff_steps_warn,
            daily_steps_alert_override=eff_steps_alert,
            rolling_24h_steps=rolling_24h_steps,
            hub_pid_alive=hub_pid_alive_val,
            cache_culprit_summary=cache_culprit_summary,
            downgraded_stale_t1=downgraded_stale_t1,
        )

        # If the live frame itself is actively degraded (T1/T2/T3), NEVER lift a prior adaptive_down tightening
        # based on unsettled pre-degradation hours (covers --readonly, cold-start, and first degraded tick).
        if (
            resolved_account
            and (not is_historical_rating)
            and (not is_custom_account_view)
            and grade in ("T1", "T2", "T3")
            and recent_recov_steps > 0
        ):
            (
                eff_steps_warn,
                eff_steps_alert,
                steps_limit_mode,
                steps_limit_note,
                acct_hist_safe_peak,
            ) = compute_adaptive_daily_steps_limits(
                base_steps_warn,
                base_steps_alert,
                hist_days,
                recent_recovery_steps=0,
            )

        # Ensure IDLE offline state never leaks stale log/outage/T2 error fields
        if grade == "IDLE" and not is_custom_account_view:
            recent_log = {"503": 0, "timeout": 0, "403": 0, "retry": 0}
            notable_events = []
            annotated_notable = []
            heavy_media_timeout_count = 0
            last_t2_error_ts = 0.0
            t2_recovery_note = ""
            proxy_outage_recent = 0
            proxy_outage_note = ""
            cache_culprit_summary = ""

        # Throttled privacy-safe persistence of verified healthy / degraded step capacity (never in read_only!)
        if is_live_30m and resolved_account and (not self.read_only):
            # Resolve true degradation timestamp once for both hourly buckets and daily compatibility profile (never fall back to blind `now`!)
            is_deg_grade = grade in ("T1", "T2", "T3")
            deg_ts = 0.0
            if is_deg_grade:
                if grade == "T1":
                    t1_ts = max(
                        (
                            c.last_abnormal_ts
                            for c in recent_active
                            if c.last_abnormal_ts > 0.0
                            and (c.consec_miss >= T1_CONSEC_MISS_THRESHOLD or c is severe_frequent_miss_conv)
                        ),
                        default=0.0,
                    )
                    if t1_ts > 0.0:
                        deg_ts = t1_ts
                elif grade == "T2" and last_t2_error_ts > 0.0:
                    deg_ts = last_t2_error_ts
                elif grade == "T3":
                    t3_ts = max((ev.timestamp for ev in annotated_notable if ev.category == "403"), default=0.0)
                    if t3_ts > 0.0:
                        deg_ts = t3_ts

            if today_steps > 0:
                # Never attribute a prior-day lingering degradation (e.g. 23:55 T2 persisting at 00:02) to today's daily profile!
                is_today_degraded = is_deg_grade and (deg_ts > 0.0) and (deg_ts >= today_start)
                self._ledger.record_daily_step_profile(
                    short_label=resolved_account,
                    date_str=today_str,
                    today_steps=today_steps,
                    is_healthy=(grade == "T0"),
                    is_degraded=is_today_degraded,
                )
            # Skip the partially elapsed left-edge hour bucket so we never truncate a prior full hour
            left_edge_h = int(rolling_24h_cutoff // 3600)
            persist_buckets = {hk: cnt for hk, cnt in account_24h_utc_buckets.items() if hk > left_edge_h and cnt > 0}
            degraded_hours: Optional[Dict[int, Optional[int]]] = None
            if is_deg_grade and deg_ts > 0.0:
                deg_h = int(deg_ts // 3600)
                target_deg_h: Optional[int] = None
                if grade == "T1":
                    if account_24h_utc_buckets.get(deg_h, 0) > 0:
                        target_deg_h = deg_h
                else:
                    if abs(now - deg_ts) <= 120.0 and rolling_24h_steps > 0:
                        if account_24h_utc_buckets.get(deg_h, 0) > 0:
                            target_deg_h = deg_h
                        elif today_steps > 0 and deg_ts >= today_start:
                            # Anchor top-of-hour 0-step T2/T3 bursts to the most recent active (>0 steps) bucket
                            # in the same rolling 24h window so we neither write 0-step ghost buckets nor drop real T2/T3!
                            cand_hs = [hk for hk, cnt in persist_buckets.items() if hk <= deg_h and cnt > 0]
                            if not cand_hs:
                                cand_hs = [hk for hk, cnt in account_24h_utc_buckets.items() if hk <= deg_h and cnt > 0]
                            if cand_hs:
                                target_deg_h = max(cand_hs)
                if target_deg_h is not None:
                    anchor_key = (resolved_account.lower(), int(round(deg_ts)))
                    if anchor_key in self._recorded_deg_anchors:
                        # Reuse frozen initial load for this exact degradation event (prevents write storms & load drift as old steps slide out!)
                        frozen_at = self._recorded_deg_anchors[anchor_key]
                        self._recorded_deg_anchors.move_to_end(anchor_key)
                    else:
                        # Only record live rolling_24h_steps when observed within 120s of the degradation event;
                        # for stale pre-restart degradations pass None so historical two-pointer scan restores exact load.
                        if abs(now - deg_ts) <= 120.0 and rolling_24h_steps > 0:
                            frozen_at = int(rolling_24h_steps)
                        else:
                            frozen_at = None
                        self._recorded_deg_anchors[anchor_key] = frozen_at
                        while len(self._recorded_deg_anchors) > 64:
                            self._recorded_deg_anchors.popitem(last=False)
                    degraded_hours = {target_deg_h: frozen_at}
                    if target_deg_h not in persist_buckets and account_24h_utc_buckets.get(target_deg_h, 0) > 0:
                        persist_buckets[target_deg_h] = account_24h_utc_buckets[target_deg_h]
            if persist_buckets or degraded_hours:
                self._ledger.record_hourly_step_buckets(
                    short_label=resolved_account,
                    hour_counts=persist_buckets,
                    degraded_hours=degraded_hours,
                    now_ts=now,
                )

        # Compute thread-safe current-24h degradation indicator inside engine lock for UI tooltip preview
        current_24h_has_degradation = grade in ("T1", "T2", "T3")
        if (not current_24h_has_degradation) and resolved_account:
            cur_min_h = int(rolling_24h_cutoff // 3600)
            hb = self._ledger.get_account_hourly_buckets(resolved_account)
            scan_hb = self._hist_hourly_buckets_cache.get(resolved_account.lower(), {})
            if any(
                hk >= cur_min_h and rec.get("degraded") and int(rec.get("steps", 0) or 0) > 0
                for hk, rec in hb.items()
            ) or any(
                hk >= cur_min_h and rec.get("degraded") and int(rec.get("steps", 0) or 0) > 0
                for hk, rec in scan_hb.items()
            ):
                current_24h_has_degradation = True
            else:
                today_prof = self._ledger.get_account_daily_profiles(resolved_account).get(today_str)
                if today_prof is not None and today_prof.get("degraded_at") is not None:
                    current_24h_has_degradation = True

        # Offline + custom/history lookup yielding no records -> offline_unknown (exit 2) rather than normal (exit 0)
        if grade == "WAIT" and not self._account_cache.online:
            pressure = "offline_unknown"
        else:
            pressure = compute_pressure(
                grade=grade,
                today_steps=today_steps,
                daily_steps_warn=eff_steps_warn,
                daily_steps_alert=eff_steps_alert,
                rolling_24h_steps=rolling_24h_steps,
            )

        alerts = self._collect_new_alerts(
            grade=grade,
            notable_events=annotated_notable,
            recent_active=recent_active,
            max_consec_miss=max_consec_miss,
            today_steps=today_steps,
            steps_scope_label=steps_scope_label,
            is_online=self._account_cache.online,
            severe_frequent_miss_conv=severe_frequent_miss_conv,
            slot_inventory=slot_inv,
            slots_only=bool(is_custom_account_view or hr_key != "30m" or is_historical_rating),
            daily_steps_alert_override=eff_steps_alert,
            rolling_24h_steps=rolling_24h_steps,
            now_ts=now,
        )

        primary_ttft_avg = eval_primary.ttft_avg_sec if eval_primary else None
        primary_ttft_max = eval_primary.ttft_max_sec if eval_primary else None
        primary_recent_ttfts = list(eval_primary.recent_ttfts) if eval_primary else []

        # Release temporary per-poll full step/gen lists before crossing thread boundary into UI snapshot
        for _c in convs:
            _c._scoped_steps_all = []
            _c._scoped_gens_all = []
        for _c in top_convs:
            _c._scoped_steps_all = []
            _c._scoped_gens_all = []
        for _c in carryover_convs:
            _c._scoped_steps_all = []
            _c._scoped_gens_all = []
        if target_conv is not None:
            target_conv._scoped_steps_all = []
            target_conv._scoped_gens_all = []

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
            daily_steps_warn=eff_steps_warn,
            daily_steps_alert=eff_steps_alert,
            active_conversations=recent_active,
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
            account=replace(self._account_cache, pools=dict(self._account_cache.pools)),
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
            is_host_idle_offline=bool(
                (not self._account_cache.online)
                and (not self._any_hub_pid_alive)
                and ((now - latest_host_transcript_ts) >= 15.0)
            ),
            primary_hit_reason=primary_hit_reason,
            carryover_conversations=carryover_convs,
            heavy_media_timeout_recent=heavy_media_timeout_count,
            proxy_outage_recent=proxy_outage_recent,
            proxy_outage_note=proxy_outage_note,
            daily_steps_limit_mode=steps_limit_mode,
            daily_steps_limit_note=steps_limit_note,
            daily_steps_base_alert=base_steps_alert,
            account_hist_safe_peak=acct_hist_safe_peak,
            rolling_24h_steps=rolling_24h_steps,
            account_24h_steps=account_24h_steps,
            total_24h_steps=total_24h_steps,
            main_24h_steps=main_24h_steps,
            subagent_24h_steps=subagent_24h_steps,
            total_main_24h_steps=total_main_24h_steps,
            total_subagent_24h_steps=total_subagent_24h_steps,
            peak_hour_steps_24h=peak_hour_steps_24h,
            peak_hour_label_24h=peak_hour_label_24h,
            current_24h_has_degradation=current_24h_has_degradation,
            hub_pid_alive=hub_pid_alive_val,
            primary_hit_is_compact=primary_hit_is_compact,
            primary_hit_is_revert=primary_hit_is_revert,
            cache_culprit_summary=cache_culprit_summary,
            log_session_start_ts=self._log_tailer.get_session_start_ts(),
        )

    @staticmethod
    def _extract_head_before_blobs(line: str, max_len: int = 512) -> str:
        """Return only the leading metadata prefix strictly before content/thinking/tool_calls/media blobs."""
        limit = min(len(line), max_len)
        head = line[:limit]
        cut = len(head)
        for marker in ('"content"', '"thinking"', '"tool_calls"', '"media"'):
            pos = head.find(marker)
            if pos != -1 and pos < cut:
                cut = pos
        return head[:cut]

    def _get_cached_history_daily_profiles(
        self,
        now: float,
        today_start: float,
        today_str: str,
        timeline: List[Tuple[float, str]],
        fallback_label: str = "",
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Low-frequency (10m TTL) read-only scan of historical per-account hourly & daily step profiles."""
        hist_cutoff = min(today_start, now - 86400.0) - 15 * 86400.0
        settled_end_h = int(math.floor((now - 86400.0) / 3600.0))
        upper_hist_ts = max(today_start, float(settled_end_h) * 3600.0)
        hist_rules = self._observe_and_get_rule_timestamps(now, since_ts=hist_cutoff)
        hist_rule_sig = tuple(round(t, 1) for t in hist_rules[-64:])
        idle_sec = int(self.config.get("idle_rewarm_sec", 180))
        min_cache_tok = int(self.config.get("min_cacheable_tokens", 32000))
        last_tl = timeline[-1] if timeline else None
        cache_key = (
            today_str,
            settled_end_h,
            len(timeline),
            last_tl,
            (fallback_label or "").lower(),
            hist_rule_sig,
            idle_sec,
            min_cache_tok,
        )
        if (
            self._hist_daily_profiles_cache_key == cache_key
            and 0.0 <= (now - self._hist_daily_profiles_cache_ts) < 600.0
        ):
            return self._hist_daily_profiles_cache

        profiles: Dict[str, Dict[str, Dict[str, Any]]] = {}
        hist_hourly: Dict[str, Dict[int, Dict[str, Any]]] = {}
        if not self.brain_dir.is_dir():
            self._hist_daily_profiles_cache_key = cache_key
            self._hist_daily_profiles_cache_ts = now
            self._hist_daily_profiles_cache = profiles
            self._hist_hourly_buckets_cache = hist_hourly
            return profiles

        hist_timeline = self._ledger.get_timeline(hist_cutoff)
        if not hist_timeline:
            hist_timeline = timeline
        tl_times = [t for t, _ in hist_timeline]
        hist_switches = self._ledger.get_switch_timestamps(hist_cutoff, timeline=hist_timeline)

        # Gather bounded set of transcripts modified within 15d (skip pure-today cached subagents; 10m TTL)
        cands: List[Tuple[float, str, Path]] = []
        try:
            with os.scandir(self.brain_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    tp_str = os.path.join(entry.path, ".system_generated", "logs", "transcript.jsonl")
                    try:
                        st = os.stat(tp_str)
                    except OSError:
                        continue
                    if st.st_mtime >= hist_cutoff:
                        cands.append((st.st_mtime, entry.name, Path(tp_str)))
        except OSError:
            cands = []
        cands.sort(key=lambda x: x[0], reverse=True)

        acct_all_events: Dict[str, List[Tuple[float, bool]]] = {}
        processed_hist_convs = 0
        for _mtime, cid, tp in cands[:600]:
            self._maybe_yield_for_ui()
            cached = self._transcript_cache.get(cid)
            raw_steps: List[Dict[str, Any]]
            if cached is not None:
                raw_steps = cached[1]
                if not raw_steps or float(raw_steps[0].get("timestamp", 0.0) or 0.0) >= upper_hist_ts:
                    continue
            else:
                # Binary pre-filter + header-only scalar extraction (verifying all present fields captured before skipping json.loads)
                raw_steps = []
                pending_cp = False
                try:
                    with tp.open("rb") as fh:
                        for raw_line in fh:
                            has_pr = b'"PLANNER_RESPONSE"' in raw_line
                            if not has_pr:
                                if b'"CHECKPOINT"' in raw_line:
                                    head_cp = self._extract_head_before_blobs(
                                        raw_line[:320].decode("utf-8", errors="replace"),
                                        256,
                                    )
                                    if '"CHECKPOINT"' in head_cp:
                                        pending_cp = True
                                continue
                            if b'"input_tokens"' not in raw_line:
                                continue
                            head_raw = raw_line[:640].decode("utf-8", errors="replace")
                            head = self._extract_head_before_blobs(head_raw, 640)
                            is_pr_head = ('"type":"PLANNER_RESPONSE"' in head) or ('"type": "PLANNER_RESPONSE"' in head)
                            m_in = _FAST_IN_TOK_RE.search(head) if is_pr_head else None
                            m_ts = _FAST_CREATED_AT_RE.search(head) if is_pr_head else None
                            m_cr = _FAST_CR_TOK_RE.search(head) if (is_pr_head and m_in and m_ts) else None
                            m_out = _FAST_OUT_TOK_RE.search(head) if (is_pr_head and m_in and m_ts) else None
                            m_si = _FAST_STEP_IDX_RE.search(head) if (is_pr_head and m_in and m_ts) else None
                            m_mod = _FAST_MODEL_RE.search(head) if (is_pr_head and m_in and m_ts) else None
                            fast_complete = (
                                is_pr_head
                                and (m_in is not None)
                                and (m_ts is not None)
                                and (m_cr is not None or b'"cache_read_tokens"' not in raw_line)
                                and (m_out is not None or b'"output_tokens"' not in raw_line)
                                and (m_si is not None or b'"step_index"' not in raw_line)
                                and (m_mod is not None or b'"model"' not in raw_line)
                            )
                            if fast_complete:
                                assert m_in is not None and m_ts is not None
                                ts_v = parse_iso_timestamp(m_ts.group(1))
                                if ts_v is None or ts_v < hist_cutoff:
                                    pending_cp = False
                                    continue
                                raw_steps.append(
                                    _RawStepSlot(
                                        step_index=int(m_si.group(1)) if m_si else len(raw_steps),
                                        timestamp=ts_v,
                                        model=m_mod.group(1) if m_mod else "",
                                        input_tokens=int(m_in.group(1)),
                                        cache_read_tokens=int(m_cr.group(1)) if m_cr else 0,
                                        output_tokens=int(m_out.group(1)) if m_out else 0,
                                        after_checkpoint=pending_cp,
                                        after_revert=False,
                                    )
                                )
                                pending_cp = False
                            else:
                                try:
                                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                                except (ValueError, TypeError):
                                    continue
                                if not isinstance(obj, dict) or obj.get("type") != "PLANNER_RESPONSE" or "input_tokens" not in obj:
                                    continue
                                ts_v = parse_iso_timestamp(str(obj.get("created_at", "") or ""))
                                if ts_v is None or ts_v < hist_cutoff:
                                    pending_cp = False
                                    continue
                                raw_steps.append(
                                    _RawStepSlot(
                                        step_index=obj.get("step_index", len(raw_steps)),
                                        timestamp=ts_v,
                                        model=str(obj.get("model", "") or ""),
                                        input_tokens=int(obj.get("input_tokens", 0) or 0),
                                        cache_read_tokens=int(obj.get("cache_read_tokens", 0) or 0),
                                        output_tokens=int(obj.get("output_tokens", 0) or 0),
                                        after_checkpoint=pending_cp,
                                        after_revert=False,
                                    )
                                )
                                pending_cp = False
                except OSError:
                    continue
            if not raw_steps:
                continue
            has_prior = any(hist_cutoff <= float(r.get("timestamp", 0.0) or 0.0) < upper_hist_ts for r in raw_steps)
            if not has_prior:
                continue
            processed_hist_convs += 1
            if processed_hist_convs > 240:
                break
            steps = classify_steps(
                raw_steps,
                idle_rewarm_sec=idle_sec,
                min_cacheable_tokens=min_cache_tok,
                account_switches=hist_switches,
                rule_change_timestamps=hist_rules,
            )
            consec = 0
            win10: Deque[str] = deque(maxlen=10)
            prev_acct_low = ""
            for st_rec in steps:
                acct = self._ledger.attribute_timestamp(
                    st_rec.timestamp,
                    hist_timeline,
                    fallback_label=fallback_label,
                    _times_cache=tl_times,
                )
                acct_low = acct.lower() if acct else ""
                if st_rec.is_account_switch or (acct_low and prev_acct_low and acct_low != prev_acct_low):
                    consec = 0
                    win10.clear()
                if acct_low:
                    prev_acct_low = acct_low

                win10.append(st_rec.classification)
                is_t1_step = False
                if st_rec.classification == "abnormal_miss":
                    consec += 1
                    if consec >= T1_CONSEC_MISS_THRESHOLD:
                        is_t1_step = True
                    else:
                        abn_10 = sum(1 for c_cls in win10 if c_cls == "abnormal_miss")
                        hit_10 = sum(1 for c_cls in win10 if c_cls == "hit")
                        cach_10 = abn_10 + hit_10
                        if abn_10 >= 4 and cach_10 >= 4 and (hit_10 / float(cach_10)) < 0.5:
                            is_t1_step = True
                else:
                    consec = 0

                if not (hist_cutoff <= st_rec.timestamp < upper_hist_ts):
                    continue
                if not acct_low:
                    continue
                acct_all_events.setdefault(acct_low, []).append((st_rec.timestamp, is_t1_step))

        for acct_low, ev_all in acct_all_events.items():
            ev_all.sort(key=lambda x: x[0])
            hmap: Dict[int, Dict[str, Any]] = {}
            day_events: Dict[str, List[bool]] = {}
            left = 0
            for idx_e, (ts_v, is_t1) in enumerate(ev_all):
                while left < idx_e and (ts_v - ev_all[left][0]) > 86400.0:
                    left += 1
                win_24h_load = idx_e - left + 1
                hk = int(ts_v // 3600)
                slot = hmap.get(hk)
                if slot is None:
                    slot = {"steps": 0, "degraded": False, "degraded_24h_at": None}
                    hmap[hk] = slot
                slot["steps"] += 1
                if is_t1:
                    slot["degraded"] = True
                    slot["degraded_24h_at"] = _pick_preferred_deg_at(slot["degraded_24h_at"], win_24h_load)

                if ts_v < today_start:
                    d_str = datetime.datetime.fromtimestamp(ts_v).strftime("%Y-%m-%d")
                    if d_str < today_str:
                        day_events.setdefault(d_str, []).append(is_t1)

            if hmap:
                hist_hourly[acct_low] = hmap
            for d_str, flags in day_events.items():
                total_s = len(flags)
                deg_at: Optional[int] = None
                for step_ord, is_t1 in enumerate(flags, 1):
                    if is_t1:
                        deg_at = _pick_preferred_deg_at(deg_at, step_ord)
                        if deg_at is not None and deg_at >= 400:
                            break
                profiles.setdefault(acct_low, {})[d_str] = {
                    "safe_steps": total_s if deg_at is None else max(0, deg_at - 1),
                    "max_steps": total_s,
                    "degraded_at": deg_at,
                }

        self._hist_daily_profiles_cache_key = cache_key
        self._hist_daily_profiles_cache_ts = now
        self._hist_daily_profiles_cache = profiles
        self._hist_hourly_buckets_cache = hist_hourly
        return profiles

    @staticmethod
    def _format_cache_culprit_summary(conv: Optional[ConversationSummary]) -> str:
        """Format culprit conversation short id + occurrence date, e.g. '1a5df4 (11-20)'."""
        if conv is None:
            return ""
        ts_c = conv.last_abnormal_ts if conv.last_abnormal_ts > 0.0 else conv.latest_timestamp
        if ts_c > 0.0:
            md = datetime.datetime.fromtimestamp(ts_c).strftime("%m-%d")
            return f"{conv.short_id} ({md})"
        return conv.short_id

    @staticmethod
    def _merge_account_hourly_buckets(
        ledger_hourly: Dict[int, Dict[str, Any]],
        scan_hourly: Dict[int, Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        """Merge persisted ledger & scanned hourly buckets with self-healing 0-step ghost drop and live-load priority."""
        merged_hourly: Dict[int, Dict[str, Any]] = {}
        for hk in set(ledger_hourly.keys()) | set(scan_hourly.keys()):
            l_rec = ledger_hourly.get(hk)
            s_rec = scan_hourly.get(hk)
            l_steps = int(l_rec.get("steps", 0) or 0) if l_rec else 0
            s_steps = int(s_rec.get("steps", 0) or 0) if s_rec else 0
            steps_v = max(l_steps, s_steps)
            # Self-heal legacy 0-step phantom degraded buckets written by older versions
            if steps_v <= 0:
                continue
            deg_v = bool((l_rec and l_rec.get("degraded")) or (s_rec and s_rec.get("degraded")))
            l_at_raw = l_rec.get("degraded_24h_at") if l_rec else None
            s_at_raw = s_rec.get("degraded_24h_at") if s_rec else None
            l_at = (
                int(l_at_raw)
                if (l_at_raw is not None and not isinstance(l_at_raw, bool) and int(l_at_raw) > 0)
                else None
            )
            s_at = (
                int(s_at_raw)
                if (s_at_raw is not None and not isinstance(s_at_raw, bool) and int(s_at_raw) > 0)
                else None
            )
            if (
                l_rec is not None
                and l_steps > 0
                and l_at is not None
                and s_at is not None
                and l_at >= 400
                and s_at >= 400
            ):
                deg_at: Optional[int] = max(l_at, s_at)
            else:
                deg_at = _pick_preferred_deg_at(l_at, s_at)
            merged_hourly[hk] = {"steps": steps_v, "degraded": deg_v, "degraded_24h_at": deg_at}
        return merged_hourly

    @staticmethod
    def _compute_recent_unsettled_recovery_steps(
        merged_hourly: Dict[int, Dict[str, Any]],
        now_ts: float,
    ) -> int:
        """Return max rolling-24h healthy steps strictly after the latest degradation hour and strictly before current hour.

        Requires >= 3 non-empty purely healthy hours strictly after ``last_deg_h`` and strictly before
        ``int(now_ts // 3600)``; otherwise returns 0. Bounded O(360) horizon.
        """
        if not merged_hourly:
            return 0
        cur_h = int(float(now_ts) // 3600)
        min_horizon_h = cur_h - 360
        last_deg_h: Optional[int] = None
        parsed: Dict[int, Tuple[int, bool]] = {}
        for raw_h, rec in merged_hourly.items():
            try:
                hk = int(raw_h)
            except (TypeError, ValueError):
                continue
            if hk < min_horizon_h or not isinstance(rec, dict):
                continue
            deg_v = bool(rec.get("degraded", False))
            steps_v = max(0, int(rec.get("steps", 0) or 0))
            if deg_v and (last_deg_h is None or hk > last_deg_h):
                last_deg_h = hk
            parsed[hk] = (steps_v, deg_v)

        clean_map: Dict[int, int] = {}
        for hk, (steps_v, deg_v) in parsed.items():
            if hk >= cur_h or deg_v or steps_v <= 0:
                continue
            if last_deg_h is not None and hk <= last_deg_h:
                continue
            clean_map[hk] = steps_v

        if len(clean_map) < 3:
            return 0

        min_c = min(clean_map.keys())
        max_c = max(clean_map.keys())
        best_steps = 0
        win_steps = 0
        win_hours = 0
        for end_h in range(min_c, max_c + 1):
            enter_s = clean_map.get(end_h, 0)
            if enter_s > 0:
                win_steps += enter_s
                win_hours += 1
            leave_h = end_h - 24
            if leave_h >= min_c:
                leave_s = clean_map.get(leave_h, 0)
                if leave_s > 0:
                    win_steps -= leave_s
                    win_hours -= 1
            if win_hours >= 3 and win_steps > best_steps:
                best_steps = win_steps
        return best_steps

    def _get_account_completed_daily_history(
        self,
        account_label: str,
        now: float,
        today_start: float,
        today_str: str,
        timeline: List[Tuple[float, str]],
        fallback_label: str = "",
    ) -> List[Dict[str, Any]]:
        """Merge persisted & scanned hourly buckets into strictly settled rolling-24h peaks + legacy completed daily ledger."""
        acct_low = (account_label or "").strip().lower()
        if not acct_low:
            return []
        self._get_cached_history_daily_profiles(
            now, today_start, today_str, timeline, fallback_label=fallback_label
        )
        # 1. Fuse hourly buckets by hour_epoch BEFORE sliding-window evaluation (preserves cross-midnight windows after cleanup)
        ledger_hourly = self._ledger.get_account_hourly_buckets(account_label)
        scan_hourly = self._hist_hourly_buckets_cache.get(acct_low, {})
        merged_hourly = self._merge_account_hourly_buckets(ledger_hourly, scan_hourly)

        settled_recs = compute_settled_rolling_24h_records(merged_hourly, now_ts=now)
        settled_by_day: Dict[str, Dict[str, Any]] = {r["date"]: r for r in settled_recs}

        # 2. Legacy daily_step_profiles fallback ONLY for pre-upgrade dates strictly earlier than
        #    both yesterday AND this account's earliest hourly bucket date (prevents calendar-day
        #    profiles written post-upgrade from cross-contaminating rolling-24h settled peaks!).
        yesterday_str = datetime.datetime.fromtimestamp(today_start - 43200.0).strftime("%Y-%m-%d")
        first_hourly_day = (
            datetime.datetime.fromtimestamp((min(merged_hourly.keys()) + 1) * 3600 - 1).strftime("%Y-%m-%d")
            if merged_hourly
            else ""
        )
        ledger_days = self._ledger.get_account_daily_profiles(account_label)
        legacy_days = {
            d: rec
            for d, rec in ledger_days.items()
            if d < yesterday_str
            and d not in settled_by_day
            and (not first_hourly_day or d < first_hourly_day)
        }

        all_dates = sorted(set(settled_by_day.keys()) | set(legacy_days.keys()))
        merged: List[Dict[str, Any]] = []
        for d_str in all_dates:
            r_set = settled_by_day.get(d_str)
            r_leg = legacy_days.get(d_str)
            deg_at = _pick_preferred_deg_at(
                r_set.get("degraded_at") if r_set else None,
                r_leg.get("degraded_at") if r_leg else None,
            )
            safe_vals = [int(r.get("safe_steps", 0) or 0) for r in (r_set, r_leg) if r is not None]
            safe_s = max(safe_vals) if safe_vals else 0
            if deg_at is not None:
                safe_s = min(safe_s, max(0, deg_at - 1))
            entry: Dict[str, Any] = {"date": d_str, "safe_steps": safe_s, "degraded_at": deg_at}
            if r_set is not None:
                post_safe = int(r_set.get("post_deg_safe_steps", 0) or 0)
                if post_safe > 0:
                    entry["post_deg_safe_steps"] = post_safe
            merged.append(entry)
        return merged

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

    @staticmethod
    def _format_timeout_dur_short(dur_sec: Optional[float]) -> str:
        if dur_sec is None or dur_sec <= 0:
            return ""
        total = int(round(dur_sec))
        if total >= 3600 and total % 3600 == 0:
            return f"{total // 3600}h"
        if total >= 60 and total % 60 == 0:
            return f"{total // 60}m"
        if total >= 60:
            return f"{total // 60}m{total % 60}s"
        return f"{dur_sec:.0f}s"

    def _annotate_log_events_with_conversations(
        self,
        events: List[LogEvent],
        known_convs: List[ConversationSummary],
    ) -> List[LogEvent]:
        """Match language_server.log error timestamps against point-in-time ERROR_MESSAGE (±6s) and USER_INPUT snapshots."""
        if not events:
            return events
        conv_map = {c.conversation_id: c for c in known_convs}
        out: List[LogEvent] = []
        for ev in events:
            if ev.category not in ("timeout", "503", "403"):
                out.append(ev)
                continue
            best_cid = ""
            best_snap_mc = 0
            best_snap_mb = 0
            best_snap_ctx = 0
            best_key: Optional[Tuple[int, int, float]] = None

            # Stage 1: Exact point-in-time ERROR_MESSAGE match (±6s)
            for cid, err_entries in self._transcript_error_ts.items():
                if not err_entries:
                    continue
                is_sub = 1 if self._is_cached_subagent(cid, conv_map) else 0
                for entry in err_entries:
                    ets = float(entry[0])
                    etxt = str(entry[1])
                    s_mc = int(entry[2]) if len(entry) >= 5 else 0
                    s_mb = int(entry[3]) if len(entry) >= 5 else 0
                    s_ctx = int(entry[4]) if len(entry) >= 5 else 0
                    diff = abs(ets - ev.timestamp)
                    if diff > 6.0:
                        continue
                    # Prefer explicit timeout text (0), then empty content (1, Antigravity timeout norm), then unrelated text (2)
                    if ev.category == "timeout":
                        if "timeout" in etxt or "deadline" in etxt or "timed out" in etxt:
                            rel_rank = 0
                        elif not etxt.strip():
                            rel_rank = 1
                        else:
                            rel_rank = 2
                    elif ev.category == "503":
                        rel_rank = 0 if ("503" in etxt or "capacity" in etxt or "unavailable" in etxt or "exhausted" in etxt) else 1
                    else:
                        rel_rank = 0 if ("403" in etxt or "permission" in etxt or "tos" in etxt) else 1
                    cand_key = (rel_rank, is_sub, diff)
                    if best_key is None or cand_key < best_key:
                        best_key = cand_key
                        best_cid = cid
                        best_snap_mc = s_mc
                        best_snap_mb = s_mb
                        best_snap_ctx = s_ctx

            # Stage 2: Fallback USER_INPUT / /compact snapshot match (ev.timestamp - dur_sec ±20s) for long >=60s timeouts.
            # Also allow Stage 2 to override an unrelated non-timeout Stage 1 error (rel_rank == 2).
            if (
                (not best_cid or (best_key is not None and best_key[0] >= 2))
                and ev.category == "timeout"
                and ev.duration_sec is not None
                and ev.duration_sec >= 60.0
                and self._transcript_tail_meta
            ):
                est_start = ev.timestamp - float(ev.duration_sec)
                best_u_key: Optional[Tuple[int, float]] = None
                for cid, tmeta in self._transcript_tail_meta.items():
                    u_snaps = tmeta[0]
                    if not u_snaps:
                        continue
                    is_sub = 1 if self._is_cached_subagent(cid, conv_map) else 0
                    for u_ts, u_mc, u_mb, u_ctx in u_snaps:
                        diff = abs(est_start - u_ts)
                        if diff > 20.0:
                            continue
                        cand_u = (is_sub, diff)
                        if best_u_key is None or cand_u < best_u_key:
                            best_u_key = cand_u
                            best_cid = cid
                            best_snap_mc = int(u_mc)
                            best_snap_mb = int(u_mb)
                            best_snap_ctx = int(u_ctx)

            if best_cid:
                conv_obj = conv_map.get(best_cid)
                title = conv_obj.title if conv_obj is not None else self._resolve_conv_title(best_cid)
                title_part = f" {truncate_display_width(title, 22)}" if title else ""
                tag = f"{best_cid[:6]}{title_part}"

                # Heavy-media upload timeout attribution strictly uses point-in-time snapshot (immune to later /compact!)
                is_heavy_media_to = False
                if ev.category == "timeout":
                    if (
                        best_snap_mb >= 5 * 1024 * 1024
                        or best_snap_mc >= 10
                        or (best_snap_ctx >= 400_000 and best_snap_mb >= 2 * 1024 * 1024)
                    ):
                        is_heavy_media_to = True

                if is_heavy_media_to:
                    hm_match = re.match(r"^(\[\d{2}:\d{2}:\d{2}\])\s*", ev.summary or "")
                    hm_prefix = f"{hm_match.group(1)} " if hm_match else ""
                    dur_short = self._format_timeout_dur_short(ev.duration_sec)
                    dur_part = f" ({dur_short})" if dur_short else ""
                    media_part = f" [图{best_snap_mc}({format_media_size(best_snap_mb)})]"
                    new_summary = f"{hm_prefix}多图上传超时{dur_part} · {tag}{media_part}"
                else:
                    new_summary = ev.summary if tag in ev.summary else f"{ev.summary} · {tag}"

                out.append(
                    LogEvent(
                        timestamp=ev.timestamp,
                        category=ev.category,
                        summary=new_summary,
                        event_key=ev.event_key,
                        model=ev.model,
                        duration_sec=ev.duration_sec,
                        is_heavy_media_timeout=is_heavy_media_to,
                        media_count=best_snap_mc,
                        media_bytes=best_snap_mb,
                        culprit_cid=best_cid,
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
        enum_map: Optional[Dict[str, str]] = None,
        slow_tps_th: float = 40.0,
        slow_ttft_th: float = 45.0,
    ) -> List[ModelPerfStats]:
        """Aggregate per-model TTFT, TPS, cache hits, and post-503 recovery evidence."""
        buckets: Dict[str, Dict[str, Any]] = {}
        full_speed_tps_floor = max(80.0, float(slow_tps_th) * 2.0)
        for c in recent_active:
            if use_full_scoped and c._scoped_steps_all:
                steps = c._scoped_steps_all
                gens = c._scoped_gens_all if len(c._scoped_gens_all) == len(steps) else []
                ttfts = [g.ttft_sec if g is not None else None for g in gens] if gens else c.recent_ttfts
                tpss = [g.tps if g is not None else None for g in gens] if gens else c.recent_tps_list
                strms = [g.streaming_sec if g is not None else None for g in gens] if gens else []
            else:
                steps = c.recent_steps[-10:]
                gens_r = c._scoped_gens_all[-len(steps):] if c._scoped_gens_all else []
                ttfts = c.recent_ttfts[-len(steps):] if c.recent_ttfts else []
                tpss = c.recent_tps_list[-len(steps):] if c.recent_tps_list else []
                strms = [g.streaming_sec if g is not None else None for g in gens_r] if len(gens_r) == len(steps) else []
            t_off = len(steps) - len(ttfts)
            p_off = len(steps) - len(tpss)
            s_off = len(steps) - len(strms)
            max_ctx_cap = int(c.max_context_tokens or 0) or 512_000
            for idx, st in enumerate(steps):
                if st.timestamp < cutoff_ts:
                    continue
                mname = format_model_name(st.model, enum_map=enum_map)
                b = buckets.setdefault(
                    mname,
                    {
                        "steps": 0,
                        "hits": 0,
                        "abn": 0,
                        "ttfts": [],
                        "tpss": [],
                        "w_out": 0,
                        "w_strm": 0.0,
                        "latest_ts": 0.0,
                        "latest_ttft": None,
                        "post_steps": 0,
                        "post_ttfts": [],
                        "post_latest_ts": 0.0,
                        "post_latest_ttft": None,
                        "main_post_steps": 0,
                        "main_post_pairs": [],       # (timestamp, raw_ttft_sec)
                        "main_adj_post_pairs": [],   # (timestamp, calibrated_ttft_sec)
                        "main_media_exempt": 0,
                        "main_large_ctx_cal": False,
                    },
                )
                b["steps"] += 1
                if st.classification == "hit":
                    b["hits"] += 1
                elif st.classification == "abnormal_miss":
                    b["abn"] += 1
                ti = idx - t_off
                pi = idx - p_off
                si = idx - s_off
                t_val = ttfts[ti] if 0 <= ti < len(ttfts) else None
                p_val = tpss[pi] if 0 <= pi < len(tpss) else None
                s_val = strms[si] if 0 <= si < len(strms) else None
                if t_val is not None:
                    b["ttfts"].append(float(t_val))
                if p_val is not None:
                    b["tpss"].append(float(p_val))
                if s_val is not None and s_val >= 0.5 and st.output_tokens > 0:
                    b["w_out"] += int(st.output_tokens)
                    b["w_strm"] += float(s_val)
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
                            raw_t = float(t_val)
                            # True low-TPS throttling is NEVER exempted by large-context or media allowance!
                            is_low_tps = bool(
                                st.output_tokens >= 500
                                and (s_val is None or s_val >= 1.5)
                                and p_val is not None
                                and float(p_val) < float(slow_tps_th)
                            )
                            ctx_allow = 0.0 if is_low_tps else compute_large_ctx_ttft_allowance(st.total_tokens, max_ctx_cap)
                            # Module 2: Strict heavy-media sustained full-speed upload exemption
                            is_heavy_media = bool(
                                st.active_media_count >= 3 or st.active_media_bytes >= 1 * 1024 * 1024
                            )
                            is_sustained_full_speed = bool(
                                st.output_tokens >= 500
                                and s_val is not None
                                and float(s_val) >= 1.5
                                and p_val is not None
                                and float(p_val) >= full_speed_tps_floor
                            )
                            media_mb = max(0.0, float(st.active_media_bytes)) / (1024.0 * 1024.0)
                            media_cap = min(
                                300.0,
                                float(slow_ttft_th) + float(st.active_media_count) * 5.0 + media_mb * 15.0 + ctx_allow,
                            )
                            if (
                                is_heavy_media
                                and is_sustained_full_speed
                                and raw_t >= float(slow_ttft_th)
                                and raw_t < media_cap
                            ):
                                b["main_media_exempt"] += 1
                            else:
                                adj_t = max(0.0, raw_t - ctx_allow)
                                if ctx_allow >= 5.0:
                                    b["main_large_ctx_cal"] = True
                                b["main_post_pairs"].append((st.timestamp, raw_t))
                                b["main_adj_post_pairs"].append((st.timestamp, adj_t))

        out: List[ModelPerfStats] = []
        for mname, b in buckets.items():
            tl = b["ttfts"]
            pl = b["tpss"]
            if b["w_strm"] >= 0.5 and b["w_out"] > 0:
                tps_avg_val: Optional[float] = float(b["w_out"]) / float(b["w_strm"])
            else:
                tps_avg_val = (sum(pl) / len(pl)) if pl else None
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

            apairs = sorted(b["main_adj_post_pairs"], key=lambda x: x[0])
            avals = [v for _, v in apairs]
            a_median: Optional[float] = None
            if avals:
                sa = sorted(avals)
                ma = len(sa) // 2
                a_median = sa[ma] if (len(sa) % 2 == 1) else ((sa[ma - 1] + sa[ma]) / 2.0)
            a_latest = avals[-1] if avals else None
            a_rec2_max = max(avals[-2:]) if len(avals) >= 2 else a_latest

            out.append(
                ModelPerfStats(
                    model=mname,
                    step_count=int(b["steps"]),
                    hit_count=int(b["hits"]),
                    abnormal_miss_count=int(b["abn"]),
                    ttft_avg_sec=(sum(tl) / len(tl)) if tl else None,
                    ttft_max_sec=max(tl) if tl else None,
                    latest_ttft_sec=b["latest_ttft"],
                    tps_avg=tps_avg_val,
                    latest_timestamp=float(b["latest_ts"]),
                    steps_after_last_t2=int(b["post_steps"]),
                    ttft_after_last_t2_avg=(sum(ptl) / len(ptl)) if ptl else None,
                    ttft_after_last_t2_latest=b["post_latest_ttft"],
                    ttft_after_last_t2_median=ptl_median,
                    main_steps_after_last_t2=int(b["main_post_steps"]),
                    main_ttft_after_last_t2_median=m_median,
                    main_ttft_after_last_t2_latest=m_latest,
                    main_ttft_after_last_t2_recent2_max=m_rec2_max,
                    main_adj_ttft_after_last_t2_median=a_median,
                    main_adj_ttft_after_last_t2_latest=a_latest,
                    main_adj_ttft_after_last_t2_recent2_max=a_rec2_max,
                    main_media_exempt_post_steps=int(b["main_media_exempt"]),
                    main_unexempt_ttft_steps_after_last_t2=len(mvals),
                    main_large_ctx_calibrated=bool(b["main_large_ctx_cal"]),
                )
            )
        out.sort(key=lambda m: m.latest_timestamp, reverse=True)
        return out

    def _classify_speed_ex(
        self,
        output_tokens: int,
        tps: Optional[float],
        ttft_sec: Optional[float],
        context_tokens: int,
        streaming_sec: Optional[float] = None,
        active_media_count: int = 0,
        active_media_bytes: int = 0,
    ) -> Tuple[str, bool]:
        """Classify step speed into ('ok'|'slow'|'unknown', media_ttft_exempted).

        Guards against:
        1) Ultra-short burst tool-calls (output_tokens < 500 or streaming_sec < 1.5s) false-alarming low TPS.
        2) High-TPS steps (>= 2x slow_tps_threshold, min 80 t/s) with moderate TTFT (< 120s) proving decoding is full-speed.
        3) Multi-media upload payload latency (~10s/MB + 3s/item, capped +120s) misattributed as backend throttling.
        """
        if tps is None and ttft_sec is None:
            return "unknown", False
        slow_tps_th = float(self.config["slow_tps_threshold"])
        slow_ttft_th = float(self.config["slow_ttft_threshold_sec"])
        if (
            output_tokens >= 500
            and (streaming_sec is None or streaming_sec >= 1.5)
            and tps is not None
            and tps < slow_tps_th
        ):
            return "slow", False
        if ttft_sec is not None and context_tokens < 220000:
            media_mb = max(0.0, float(active_media_bytes)) / (1024.0 * 1024.0)
            media_grace = min(120.0, max(0, int(active_media_count)) * 3.0 + media_mb * 10.0)
            ctx_grace = max(0.0, (context_tokens - 100000) / 10000.0) * 3.0
            eff_ttft_th = slow_ttft_th + media_grace + ctx_grace
            if ttft_sec >= slow_ttft_th:
                # Full-speed streaming output (<120s TTFT) disproves backend throttling
                if tps is not None and tps >= max(80.0, slow_tps_th * 2.0) and ttft_sec < 120.0:
                    return "ok", bool(media_grace > 0.0 and ttft_sec >= (slow_ttft_th + ctx_grace))
                if ttft_sec < eff_ttft_th:
                    return "ok", bool(media_grace > 0.0 and ttft_sec >= (slow_ttft_th + ctx_grace))
                return "slow", False
        return "ok", False

    def _classify_speed(
        self,
        output_tokens: int,
        tps: Optional[float],
        ttft_sec: Optional[float],
        context_tokens: int,
        streaming_sec: Optional[float] = None,
        active_media_count: int = 0,
        active_media_bytes: int = 0,
    ) -> str:
        status, _ = self._classify_speed_ex(
            output_tokens=output_tokens,
            tps=tps,
            ttft_sec=ttft_sec,
            context_tokens=context_tokens,
            streaming_sec=streaming_sec,
            active_media_count=active_media_count,
            active_media_bytes=active_media_bytes,
        )
        return status

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
        heavy_media_timeout_count: int = 0,
        latest_heavy_timeout_summary: str = "",
        proxy_outage_active: bool = False,
        proxy_outage_note: str = "",
        daily_steps_warn_override: Optional[int] = None,
        daily_steps_alert_override: Optional[int] = None,
        rolling_24h_steps: Optional[int] = None,
        hub_pid_alive: bool = False,
        cache_culprit_summary: str = "",
        downgraded_stale_t1: bool = False,
    ) -> Tuple[str, str, str, str, str]:
        win = self.config["log_sliding_window_min"]
        scope_win_txt = history_range_label if is_historical and history_range_label != "近 30 分钟" else f"近 {win} 分钟"
        hist_prefix = f"[{historical_tag}] " if (is_historical and historical_tag) else ""
        hist_head_tag = " (历史)" if is_historical else ""
        eff_steps_warn = int(daily_steps_warn_override) if daily_steps_warn_override is not None else int(self.config["daily_steps_warn"])
        eff_steps_alert = int(daily_steps_alert_override) if daily_steps_alert_override is not None else int(self.config["daily_steps_alert"])

        # When Antigravity desktop client is not running (no live hub) and no very recent (<2m) active steps,
        # stay quietly in standby instead of alerting on stale pre-shutdown log disconnects.
        if not is_online and not has_live_transcript:
            if hub_pid_alive:
                return (
                    "green",
                    "WAIT",
                    "已运行 · 等待账号响应",
                    "检测到 Antigravity 服务运行中，正在等待账号状态响应",
                    "",
                )
            return ("green", "IDLE", "离线待机 · Antigravity 未运行", "客户端未启动，已暂停实时异常告警", "")

        # Only evaluate live language_server.log T2/T3 alerts while online or inspecting history.
        eff_403 = recent_log["403"] if is_online else 0
        eff_timeout = recent_log["timeout"] if is_online else 0
        eff_heavy_timeout = min(eff_timeout, max(0, int(heavy_media_timeout_count))) if is_online else 0
        server_timeout_count = max(0, eff_timeout - eff_heavy_timeout)
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
        # - Heavy-media upload timeouts are excluded from server_timeout_count so large image uploads never false-alarm red T2.
        # - Single isolated server timeout (or single 503) warns in orange (WARN) instead of hard-alarming T2.
        # - (eff_503 + server_timeout_count) >= 2 enters T2 evaluation using calibrated MAIN-conversation post-error TTFT:
        #   1. Subagents & lightweight immune models (Hydrogen/Flash) NEVER whitewash flagship (Neon/Carbon) T2!
        #   2. True low-TPS steps NEVER receive large-context or media allowance.
        #   3. Heavy-media sustained full-speed steps are exempted from degrading recovery (can soften T2 -> WARN, never directly green T0).
        #   4. Full green T0 recovery requires >= 2 unexempted measured TTFT steps on the active model.
        t2_active = False
        t2_recovering_warn = ""
        t2_recovered_info = ""
        if (server_timeout_count + eff_503) >= 2:
            stats_list = model_stats or []
            active_is_light = _is_light_immune_model(active_model)

            def _eff_rep_ttft(ms: ModelPerfStats) -> Optional[float]:
                """Always display the true unadjusted raw median TTFT (never fabricate calibrated numbers)."""
                if ms.main_steps_after_last_t2 > 0:
                    return ms.main_ttft_after_last_t2_median
                return ms.ttft_after_last_t2_median if ms.ttft_after_last_t2_median is not None else ms.ttft_after_last_t2_avg

            def _eval_rep_ttft(ms: ModelPerfStats) -> Optional[float]:
                if ms.main_adj_ttft_after_last_t2_median is not None:
                    return ms.main_adj_ttft_after_last_t2_median
                return ms.main_ttft_after_last_t2_median

            def _eval_latest_ttft(ms: ModelPerfStats) -> Optional[float]:
                if ms.main_adj_ttft_after_last_t2_latest is not None:
                    return ms.main_adj_ttft_after_last_t2_latest
                return ms.main_ttft_after_last_t2_latest

            def _eval_rec2_ttft(ms: ModelPerfStats) -> Optional[float]:
                if ms.main_adj_ttft_after_last_t2_recent2_max is not None:
                    return ms.main_adj_ttft_after_last_t2_recent2_max
                return ms.main_ttft_after_last_t2_recent2_max

            def _unexempt_ttft_steps(ms: ModelPerfStats) -> int:
                if ms.main_unexempt_ttft_steps_after_last_t2 > 0:
                    return ms.main_unexempt_ttft_steps_after_last_t2
                # Backward-compatible fallback for manually constructed ModelPerfStats in legacy callers/tests
                if ms.main_media_exempt_post_steps == 0 and ms.main_ttft_after_last_t2_median is not None:
                    return ms.main_steps_after_last_t2
                return 0

            def _is_degraded(ms: ModelPerfStats) -> bool:
                steps_n = ms.main_steps_after_last_t2
                if steps_n <= 0:
                    return False
                rep = _eval_rep_ttft(ms)
                lat = _eval_latest_ttft(ms)
                rec2 = _eval_rec2_ttft(ms)
                if rep is not None and rep >= 45.0:
                    return True
                if _unexempt_ttft_steps(ms) >= 4 and rep is not None and rep < 30.0:
                    if lat is not None and lat >= 150.0:
                        return True
                    return False
                return bool(lat is not None and lat >= 60.0 and (rec2 is None or rec2 >= 60.0))

            def _is_healthy_main_recovery(ms: ModelPerfStats, allow_no_ttft: bool = False) -> bool:
                steps_n = ms.main_steps_after_last_t2
                if steps_n < 2:
                    return False
                rep = _eval_rep_ttft(ms)
                lat = _eval_latest_ttft(ms)
                if rep is None and not allow_no_ttft:
                    return False
                if rep is not None and rep >= 45.0:
                    return False
                if _unexempt_ttft_steps(ms) >= 4 and rep is not None and rep < 30.0:
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
            if server_timeout_count > 0:
                err_parts.append(f"{server_timeout_count} 次流超时")
            err_desc = " / ".join(err_parts) if err_parts else "限流事件"

            if primary_ms is not None and _is_healthy_main_recovery(primary_ms, allow_no_ttft=False):
                rep = _eff_rep_ttft(primary_ms)
                cal_tag = "(大上下文校准)" if primary_ms.main_large_ctx_calibrated else ""
                ttft_desc = f"，TTFT 中位 {rep:.1f}s{cal_tag}" if rep is not None else ""
                p_steps = primary_ms.main_steps_after_last_t2
                unexempt_n = _unexempt_ttft_steps(primary_ms)
                if p_steps >= 6 and elapsed_since_err >= 600.0 and unexempt_n >= 2:
                    t2_recovered_info = f"前序 {err_desc} 已平复（{primary_ms.model} 事后连跑 {p_steps} 步正常{ttft_desc}）"
                else:
                    t2_recovering_warn = f"前序 {err_desc}，{primary_ms.model} 事后已连跑 {p_steps} 步正常{ttft_desc}"
            elif primary_ms is not None and _is_healthy_main_recovery(primary_ms, allow_no_ttft=True):
                # Main steps succeeded via heavy-media full-speed steps or without SQLite TTFT yet -> WARN only, never green T0
                p_steps = primary_ms.main_steps_after_last_t2
                if primary_ms.main_media_exempt_post_steps >= 2:
                    t2_recovering_warn = f"前序 {err_desc}，{primary_ms.model} 事后已连跑 {p_steps} 步多图满速正常（待常规步验真）"
                else:
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
                    cal_tag = "(大上下文校准)" if best_peer.main_large_ctx_calibrated else ""
                    ttft_desc = f"，TTFT 中位 {rep:.1f}s{cal_tag}" if rep is not None else ""
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
                f"{hist_prefix}{scope_win_txt}触发 {eff_503} 次 503 / {server_timeout_count} 次流超时，事后未恢复稳定响应{extra}",
                "",
            )
        if max_consec_miss >= T1_CONSEC_MISS_THRESHOLD:
            culprit_s = cache_culprit_summary or ""
            if is_historical and culprit_s:
                sub_t1 = f"{hist_prefix}会话 {culprit_s} 曾连续 {max_consec_miss} 步异常掉缓存，建议检查账号或切回主力号"
            else:
                sub_t1 = f"{hist_prefix}活跃会话已连续 {max_consec_miss} 步异常掉缓存，建议检查账号或切回主力号"
            return (
                "red",
                "T1",
                f"T1 连续掉缓存{hist_head_tag} · {active_model}",
                sub_t1,
                t2_recovering_warn or t2_recovered_info,
            )
        if severe_frequent_miss_conv is not None:
            pct = int(round((severe_frequent_miss_conv.recent10_hit_ratio or 0.0) * 100))
            culprit_s = cache_culprit_summary or self._format_cache_culprit_summary(severe_frequent_miss_conv)
            if is_historical:
                sub_freq = (
                    f"{hist_prefix}{scope_win_txt}内 {culprit_s} 异常掉缓存 "
                    f"{severe_frequent_miss_conv.abnormal_in_recent10}/{severe_frequent_miss_conv.cacheable_in_recent10} 步"
                    f"（有效命中仅 {pct}%），尚未完全恢复健康"
                )
            else:
                sub_freq = (
                    f"{hist_prefix}近 10 步异常掉缓存 "
                    f"{severe_frequent_miss_conv.abnormal_in_recent10}/{severe_frequent_miss_conv.cacheable_in_recent10} 步"
                    f"（有效命中仅 {pct}%），单步命中未恢复健康"
                )
            return (
                "red",
                "T1",
                f"T1 频繁掉缓存{hist_head_tag} · {active_model}",
                sub_freq,
                t2_recovering_warn or t2_recovered_info,
            )
        # Step thresholds are independent volume reminders and MUST NOT override
        # the real health grade (T0/WARN/T1/T2/T3) derived from actual cache, TTFT & logs.
        eff_step_count = int(rolling_24h_steps) if rolling_24h_steps is not None else int(today_steps)
        step_window_lbl = "近24h步数" if rolling_24h_steps is not None else "今日步数"
        step_note = ""
        if eff_step_count >= eff_steps_alert:
            step_note = f" · 提醒：{step_window_lbl} {eff_step_count}/{eff_steps_alert} 已超保护红线"
        elif eff_step_count >= eff_steps_warn:
            step_note = f" · 提醒：{step_window_lbl} {eff_step_count}/{eff_steps_alert} 接近保护线"

        if (
            prior_hist_warning
            or t2_recovering_warn
            or max_consec_miss >= 2
            or any_single_miss
            or unstable_recovery_conv is not None
            or eff_503 == 1
            or server_timeout_count == 1
            or eff_heavy_timeout >= 1
            or (proxy_outage_active and bool(proxy_outage_note))
            or any_slow
        ):
            reasons = []
            if prior_hist_warning:
                reasons.append(prior_hist_warning)
            if t2_recovering_warn:
                reasons.append(t2_recovering_warn)
            if proxy_outage_active and proxy_outage_note:
                reasons.append(proxy_outage_note)
            if eff_heavy_timeout >= 1:
                if latest_heavy_timeout_summary:
                    short_h = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", latest_heavy_timeout_summary)
                    cnt_s = f" ({eff_heavy_timeout}次)" if eff_heavy_timeout > 1 else ""
                    reasons.append(f"{short_h}{cnt_s}，建议压图或/compact")
                else:
                    reasons.append(f"多图大包上传超时 ({eff_heavy_timeout}次)，建议压图或/compact")
            if max_consec_miss >= 2:
                if is_historical and cache_culprit_summary:
                    reasons.append(f"{cache_culprit_summary} 曾连续 {max_consec_miss} 步掉缓存(观察中)")
                else:
                    reasons.append(f"连续 {max_consec_miss} 步掉缓存(观察中)")
                if any_single_miss:
                    reasons.append("另含单步异常掉缓存")
            elif any_single_miss:
                if is_historical and cache_culprit_summary:
                    reasons.append(f"{cache_culprit_summary} 曾单步异常掉缓存")
                else:
                    reasons.append("单步异常掉缓存")
            elif unstable_recovery_conv is not None:
                if is_historical:
                    u_culprit = cache_culprit_summary or self._format_cache_culprit_summary(unstable_recovery_conv)
                    if downgraded_stale_t1:
                        reasons.append(
                            f"{u_culprit} 曾异常掉缓存 "
                            f"{unstable_recovery_conv.abnormal_in_recent10}/{unstable_recovery_conv.cacheable_in_recent10} 步（后续已恢复健康）"
                        )
                    else:
                        reasons.append(
                            f"{u_culprit} 曾异常掉缓存 {unstable_recovery_conv.abnormal_in_recent10} 次（观察恢复中）"
                        )
                else:
                    reasons.append(f"近10步 {unstable_recovery_conv.abnormal_in_recent10} 次掉缓存(观察恢复中)")
            if any_slow:
                reasons.append("疑似暗限速")
            if eff_503 == 1 and not t2_recovering_warn:
                reasons.append("偶发 1 次 503")
            if server_timeout_count == 1 and not t2_recovering_warn:
                reasons.append(f"偶发流超时: {latest_timeout_summary}" if latest_timeout_summary else "偶发 1 次流超时")
            return ("orange", "WARN", f"留意预警{hist_head_tag} · {active_model}", f"{hist_prefix}" + " / ".join(reasons) + step_note, t2_recovering_warn)

        if primary is None:
            if custom_empty_acct:
                return (
                    "cyan",
                    "WAIT",
                    f"历史视角 · {custom_empty_acct} 暂无记录",
                    f"{hist_prefix}{scope_win_txt}该账号无可用对话或日志记录{step_note}",
                    "",
                )
            if switched_acct_label:
                return (
                    "cyan",
                    "WAIT",
                    f"待采样 · 已切入 {switched_acct_label}",
                    f"{hist_prefix}该账号暂无历史与实时采样，前序账号异常已隔离，发出首步后自动定级{step_note}",
                    "",
                )
            return ("cyan", "WAIT", f"待采样{hist_head_tag} · 暂无会话记录", f"{hist_prefix}{scope_win_txt}暂无可用采样记录{step_note}", "")

        if t2_recovered_info:
            return ("green", "T0", f"T0 健康{hist_head_tag} · {active_model}", f"{hist_prefix}{t2_recovered_info}{step_note}", t2_recovered_info)

        hist_503 = today_log["503"]
        if (not is_historical) and proxy_outage_note and (not proxy_outage_active):
            sub = f"{hist_prefix}{proxy_outage_note}{step_note}"
        elif hist_503 > 0 and eff_503 == 0:
            sub = f"{hist_prefix}{scope_win_txt}无新异常（该号今日历史 {hist_503} 条 503 已衰减）{step_note}"
        else:
            sub = f"{hist_prefix}{scope_win_txt}缓存与底层日志均健康{step_note}"
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
        daily_steps_alert_override: Optional[int] = None,
        rolling_24h_steps: Optional[int] = None,
        now_ts: Optional[float] = None,
    ) -> List[Tuple[str, str, str]]:
        eff_steps_alert = int(daily_steps_alert_override) if daily_steps_alert_override is not None else int(self.config["daily_steps_alert"])
        eff_steps = int(rolling_24h_steps) if rolling_24h_steps is not None else int(today_steps)
        eff_now = float(now_ts) if now_ts is not None else time.time()
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
                    if ev.category == "503" or (ev.category == "timeout" and not ev.is_heavy_media_timeout):
                        candidates.append((ev.event_key, "Antigravity 限流/超时告警 (T2)", ev.summary))
            elif grade == "T1":
                # Deduplicate per contiguous cr=0 streak start or severe 10-step window start
                offending = next((c for c in recent_active if c.consec_miss >= T1_CONSEC_MISS_THRESHOLD), None)
                if offending is not None:
                    key = f"t1:{offending.conversation_id}:{offending.consec_miss_start_step}"
                    candidates.append(
                        (key, "Antigravity 疑似 T1 掉缓存", f"{offending.short_id} ({offending.model}) 已连续 {max_consec_miss} 步异常掉缓存")
                    )
                elif severe_frequent_miss_conv is not None:
                    c = severe_frequent_miss_conv
                    pct = int(round((c.recent10_hit_ratio or 0.0) * 100))
                    key = f"t1freq:{c.conversation_id}:{c.first_abnormal_step_in_recent10}"
                    candidates.append(
                        (key, "Antigravity 频繁掉缓存 (T1)", f"{c.short_id} 近 10 步异常掉缓存 {c.abnormal_in_recent10}/{c.cacheable_in_recent10} 步（有效命中仅 {pct}%）")
                    )
            else:
                # Re-arm rolling-24h step alert only after dropping below 90% of limit or after a 6h cooldown (prevents 00:00 duplicate popups)
                st_key = f"{steps_scope_label}:{eff_steps_alert}"
                prev_st = self._steps_alert_state.get(st_key)
                if eff_steps < int(eff_steps_alert * 0.9):
                    if prev_st is not None and prev_st[1]:
                        self._steps_alert_state[st_key] = (prev_st[0], False, prev_st[2])
                elif eff_steps >= eff_steps_alert:
                    can_emit = (
                        prev_st is None
                        or (not prev_st[1])
                        or (eff_now - prev_st[0]) >= 6.0 * 3600.0
                    )
                    if can_emit:
                        next_seq = (prev_st[2] + 1) if prev_st is not None else 1
                        self._steps_alert_state[st_key] = (eff_now, True, next_seq)
                        win_tag = "近24h" if rolling_24h_steps is not None else "今日"
                        candidates.append(
                            (
                                f"steps:{steps_scope_label}:{eff_steps_alert}:{next_seq}",
                                f"Antigravity {win_tag}步数红线 ({steps_scope_label})",
                                f"{steps_scope_label} {win_tag}已达 {eff_steps}/{eff_steps_alert} 步，注意控频护号",
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
        hot_cutoff: Optional[float] = None,
    ) -> List[Tuple[str, Path, Tuple[int, int]]]:
        if not self.brain_dir.is_dir():
            return []
        eff_hot_cutoff = min(today_start, float(hot_cutoff)) if hot_cutoff is not None else today_start
        today_items: List[Tuple[float, str, str, Tuple[int, int]]] = []
        older_items: List[Tuple[float, str, str, Tuple[int, int]]] = []
        join = os.path.join
        stat = os.stat
        try:
            with os.scandir(self.brain_dir) as it:
                for idx_dir, entry in enumerate(it):
                    if (idx_dir & 15) == 0:
                        self._maybe_yield_for_ui()
                    if not entry.is_dir():
                        continue
                    tp_str = join(entry.path, ".system_generated", "logs", "transcript.jsonl")
                    try:
                        st = stat(tp_str)
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_mtime < lookback_cutoff:
                        continue
                    stamp = (st.st_mtime_ns, st.st_size)
                    if st.st_mtime >= eff_hot_cutoff:
                        today_items.append((st.st_mtime, entry.name, tp_str, stamp))
                    else:
                        older_items.append((st.st_mtime, entry.name, tp_str, stamp))
        except OSError:
            return []

        today_items.sort(key=lambda item: item[0], reverse=True)
        older_items.sort(key=lambda item: item[0], reverse=True)
        merged = today_items + older_items[:max(16, int(max_older))]
        return [(cid, Path(tp_str), stamp) for _, cid, tp_str, stamp in merged]

    def _get_local_media_size_cached(self, uri: str) -> int:
        """Resolve local media URI/path to regular-file byte size with bounded LRU (1024); strictly blocks UNC/null/devices."""
        raw = (uri or "").strip()
        if not raw:
            return 0
        cached = self._media_size_cache.get(raw)
        if cached is not None:
            self._media_size_cache.move_to_end(raw)
            return cached

        size_bytes = 0
        local_path = ""
        if "\x00" not in raw and not raw.startswith("\\\\") and not raw.startswith("//"):
            cand = ""
            if raw.lower().startswith("file:"):
                try:
                    parsed_u = urllib.parse.urlparse(raw)
                    host = (parsed_u.netloc or "").strip().lower()
                    if host in ("", "localhost"):
                        p = urllib.parse.unquote(parsed_u.path or "")
                        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
                            p = p[1:]
                        cand = p
                except ValueError:
                    cand = ""
            else:
                cand = raw
            # Strict Windows drive-letter absolute path allowlist (blocks file:///%5c..., UNC, device paths, and null bytes)
            if (
                cand
                and "\x00" not in cand
                and len(cand) >= 3
                and cand[0].isascii()
                and cand[0].isalpha()
                and cand[1] == ":"
                and cand[2] in ("/", "\\")
            ):
                local_path = cand

        if local_path:
            try:
                st = os.stat(local_path)
                if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                    size_bytes = int(st.st_size)
            except (OSError, ValueError):
                size_bytes = 0

        self._media_size_cache[raw] = size_bytes
        self._media_size_cache.move_to_end(raw)
        while len(self._media_size_cache) > 1024:
            self._media_size_cache.popitem(last=False)
        return size_bytes

    def _read_transcript_cached(
        self,
        conv_id: str,
        tp: Path,
        prefetched_stamp: Optional[Tuple[int, int]] = None,
    ) -> Tuple[List[Any], bool, bool]:
        if prefetched_stamp is not None:
            stamp = prefetched_stamp
            fallback_mtime = prefetched_stamp[0] / 1_000_000_000.0
        else:
            try:
                st = tp.stat()
            except OSError:
                self._transcript_cache.pop(conv_id, None)
                self._transcript_inc_state.pop(conv_id, None)
                self._transcript_error_ts.pop(conv_id, None)
                self._transcript_tail_meta.pop(conv_id, None)
                return [], False, False
            stamp = (st.st_mtime_ns, st.st_size)
            fallback_mtime = st.st_mtime

        cached = self._transcript_cache.get(conv_id)
        if cached is not None and cached[0] == stamp:
            self._transcript_cache.move_to_end(conv_id)
            if conv_id in self._transcript_inc_state:
                self._transcript_inc_state.move_to_end(conv_id)
            if conv_id in self._transcript_error_ts:
                self._transcript_error_ts.move_to_end(conv_id)
            if conv_id in self._transcript_tail_meta:
                self._transcript_tail_meta.move_to_end(conv_id)
            return cached[1], cached[2], cached[3]

        def _parse_lines_into(
            raw_lines: List[bytes],
            state: Dict[str, Any],
        ) -> bool:
            parsed_l: List[Any] = state["parsed"]
            error_l: List[Tuple[float, str, int, int, int]] = state["error_ts_list"]
            u_snaps_l: List[Tuple[float, int, int, int]] = state["user_input_snaps"]
            has_u: bool = state["has_user_explicit"]
            has_sub: bool = state["has_subagent_marker"]
            pend_cp: bool = state["pending_checkpoint"]
            pend_cp_si: Optional[int] = state["pending_cp_si"]
            pend_rev: bool = state["pending_revert"]
            seen_pi: Optional[int] = state["seen_planner_idx"]
            min_ts: float = state["min_step_ts"]
            max_ts: float = state["max_step_ts"]
            last_ctx: int = state["last_known_ctx"]
            act_media: List[Tuple[int, str, int]] = state["active_media"]
            act_media_keys: set[Tuple[int, str]] = state["active_media_keys"]
            act_media_bytes: int = state["active_media_bytes_total"]
            last_err_ts: float = state["last_error_ts"]
            last_u_si: Optional[int] = state["last_user_si"]
            did_revert = False

            def _append_media_items(m_step_idx: int, media_list: Any) -> None:
                nonlocal act_media_bytes
                if not isinstance(media_list, list):
                    return
                for m_item in media_list:
                    if not isinstance(m_item, dict):
                        continue
                    uri = str(m_item.get("uri", "") or "").strip()
                    if not uri:
                        continue
                    mkey = (m_step_idx, uri)
                    if mkey in act_media_keys:
                        continue
                    act_media_keys.add(mkey)
                    sz = self._get_local_media_size_cached(uri)
                    act_media.append((m_step_idx, uri, sz))
                    act_media_bytes += sz

            for raw_line in raw_lines:
                if not raw_line:
                    continue
                if not has_u and b'"USER_EXPLICIT"' in raw_line:
                    has_u = True
                if not has_sub and (
                    b'"SUBAGENT"' in raw_line
                    or b'"SWARM"' in raw_line
                    or b"[swarm]" in raw_line
                    or b'"source":"AGENT"' in raw_line
                    or b'"source": "AGENT"' in raw_line
                ):
                    has_sub = True

                has_pr = b'"PLANNER_RESPONSE"' in raw_line
                if b'"ERROR_MESSAGE"' in raw_line:
                    try:
                        err_obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                        if isinstance(err_obj, dict) and err_obj.get("type") == "ERROR_MESSAGE":
                            ets = parse_iso_timestamp(str(err_obj.get("created_at", "") or ""))
                            if ets is not None:
                                ec = str(err_obj.get("content", "") or "")[:160].lower()
                                error_l.append((ets, ec, len(act_media), act_media_bytes, last_ctx))
                                if ets > last_err_ts:
                                    last_err_ts = ets
                            else:
                                last_err_ts = max(last_err_ts, max_ts if max_ts > 0.0 else fallback_mtime)
                            continue
                    except (ValueError, TypeError):
                        pass

                if not has_pr and b'"CHECKPOINT"' in raw_line:
                    try:
                        chk_obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                        if isinstance(chk_obj, dict) and chk_obj.get("type") == "CHECKPOINT":
                            pend_cp = True
                            chk_si = chk_obj.get("step_index")
                            pend_cp_si = chk_si if isinstance(chk_si, int) else None
                            pend_rev = False
                            last_err_ts = 0.0
                            act_media.clear()
                            act_media_keys.clear()
                            act_media_bytes = 0
                            last_ctx = 0
                            continue
                    except (ValueError, TypeError):
                        pass

                if not has_pr and b'"USER_INPUT"' in raw_line:
                    try:
                        u_obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(u_obj, dict) and u_obj.get("type") == "USER_INPUT":
                        u_si = u_obj.get("step_index")
                        u_ts = parse_iso_timestamp(str(u_obj.get("created_at", "") or ""))
                        is_error_resend_user = (
                            isinstance(u_si, int)
                            and last_u_si is not None
                            and u_si == last_u_si
                            and last_err_ts > 0.0
                            and (u_ts is None or (u_ts >= last_err_ts - 5.0 and (u_ts - last_err_ts) <= 30.0))
                        )
                        if (
                            not is_error_resend_user
                            and isinstance(u_si, int)
                            and u_ts is not None
                            and (max_ts <= 0.0 or u_ts >= max_ts - 5.0)
                            and (
                                (seen_pi is not None and u_si <= seen_pi)
                                or (pend_cp and pend_cp_si is not None and u_si <= pend_cp_si)
                            )
                        ):
                            pend_rev = True
                            did_revert = True
                            if pend_cp and (pend_cp_si is None or u_si <= pend_cp_si):
                                pend_cp = False
                                pend_cp_si = None
                            seen_pi = u_si
                            if u_ts > max_ts:
                                max_ts = u_ts
                            act_media = [m for m in act_media if m[0] < u_si]
                            act_media_keys = {(m[0], m[1]) for m in act_media}
                            act_media_bytes = sum(m[2] for m in act_media)
                            prev_ctx = 0
                            for prev_r in reversed(parsed_l):
                                p_si = prev_r.step_index if isinstance(prev_r, _RawStepSlot) else prev_r.get("step_index")
                                if isinstance(p_si, int) and p_si < u_si:
                                    p_in = prev_r.input_tokens if isinstance(prev_r, _RawStepSlot) else int(prev_r.get("input_tokens", 0) or 0)
                                    p_cr = prev_r.cache_read_tokens if isinstance(prev_r, _RawStepSlot) else int(prev_r.get("cache_read_tokens", 0) or 0)
                                    prev_ctx = int(p_in) + int(p_cr)
                                    break
                            last_ctx = prev_ctx
                        is_ooo_user_entry = (
                            isinstance(u_si, int)
                            and seen_pi is not None
                            and u_si < seen_pi
                            and u_ts is not None
                            and max_ts > 0.0
                            and u_ts < max_ts - 5.0
                        )
                        if not is_error_resend_user and not is_ooo_user_entry:
                            last_err_ts = 0.0
                        if isinstance(u_si, int) and not is_ooo_user_entry:
                            last_u_si = u_si
                        if u_obj.get("media") and not is_ooo_user_entry:
                            _append_media_items(int(u_si) if isinstance(u_si, int) else (seen_pi or 0), u_obj.get("media"))
                        if u_ts is not None and not is_ooo_user_entry:
                            u_snaps_l.append((u_ts, len(act_media), act_media_bytes, last_ctx))
                        continue

                if not has_pr:
                    if b'"media"' in raw_line:
                        try:
                            m_obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except (ValueError, TypeError):
                            continue
                        if isinstance(m_obj, dict) and m_obj.get("media"):
                            m_si = m_obj.get("step_index")
                            m_ts = parse_iso_timestamp(str(m_obj.get("created_at", "") or ""))
                            is_ooo_media = (
                                isinstance(m_si, int)
                                and seen_pi is not None
                                and m_si < seen_pi
                                and m_ts is not None
                                and max_ts > 0.0
                                and m_ts < max_ts - 5.0
                            )
                            if not is_ooo_media:
                                _append_media_items(int(m_si) if isinstance(m_si, int) else (seen_pi or 0), m_obj.get("media"))
                    continue

                if b'"input_tokens"' not in raw_line:
                    continue

                # 640B scalar fast-path (mirrors history daily profile parser; skips 98%+ large JSON blobs)
                fast_parsed = False
                ts = fallback_mtime
                raw_si: Any = None
                inp_tok = 0
                cr_tok = 0
                out_tok = 0
                model_str = ""
                if b'"media"' not in raw_line:
                    head_raw = raw_line[:640].decode("utf-8", errors="replace")
                    head = self._extract_head_before_blobs(head_raw, 640)
                    is_pr_head = ('"type":"PLANNER_RESPONSE"' in head) or ('"type": "PLANNER_RESPONSE"' in head)
                    m_in = _FAST_IN_TOK_RE.search(head) if is_pr_head else None
                    m_ts = _FAST_CREATED_AT_RE.search(head) if is_pr_head else None
                    m_cr = _FAST_CR_TOK_RE.search(head) if (is_pr_head and m_in) else None
                    m_out = _FAST_OUT_TOK_RE.search(head) if (is_pr_head and m_in) else None
                    m_si = _FAST_STEP_IDX_RE.search(head) if (is_pr_head and m_in) else None
                    m_mod = _FAST_MODEL_RE.search(head) if (is_pr_head and m_in) else None
                    if (
                        is_pr_head
                        and (m_in is not None)
                        and (m_ts is not None or b'"created_at"' not in raw_line)
                        and (m_cr is not None or b'"cache_read_tokens"' not in raw_line)
                        and (m_out is not None or b'"output_tokens"' not in raw_line)
                        and (m_si is not None or b'"step_index"' not in raw_line)
                        and (m_mod is not None or b'"model"' not in raw_line)
                    ):
                        ts = (parse_iso_timestamp(m_ts.group(1)) if m_ts is not None else None) or fallback_mtime
                        raw_si = int(m_si.group(1)) if m_si is not None else None
                        inp_tok = int(m_in.group(1))
                        cr_tok = int(m_cr.group(1)) if m_cr is not None else 0
                        out_tok = int(m_out.group(1)) if m_out is not None else 0
                        model_str = m_mod.group(1) if m_mod is not None else ""
                        fast_parsed = True

                if not fast_parsed:
                    try:
                        obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "PLANNER_RESPONSE" or "input_tokens" not in obj:
                        continue
                    ts = parse_iso_timestamp(str(obj.get("created_at", "") or "")) or fallback_mtime
                    raw_si = obj.get("step_index")
                    inp_tok = int(obj.get("input_tokens", 0) or 0)
                    cr_tok = int(obj.get("cache_read_tokens", 0) or 0)
                    out_tok = int(obj.get("output_tokens", 0) or 0)
                    model_str = str(obj.get("model", "") or "")

                tot_tok = inp_tok + cr_tok
                is_rev = pend_rev
                is_err_retry_flag = False
                if isinstance(raw_si, int):
                    if is_rev:
                        seen_pi = raw_si
                        if ts > max_ts:
                            max_ts = ts
                    elif (
                        seen_pi is not None
                        and raw_si <= seen_pi
                        and (max_ts <= 0.0 or ts >= max_ts - 5.0)
                    ):
                        is_ckpt_rebuild = pend_cp and (pend_cp_si is None or raw_si >= pend_cp_si)
                        is_error_retry_pr = (
                            raw_si == seen_pi
                            and last_err_ts > 0.0
                            and ts >= last_err_ts - 5.0
                            and (ts - last_err_ts) <= 30.0
                        )
                        is_same_si_eq_ctx_cr = (
                            raw_si == seen_pi
                            and cr_tok > 0
                            and last_ctx > 0
                            and abs(tot_tok - last_ctx) <= max(2048, int(last_ctx * 0.02))
                        )
                        if is_ckpt_rebuild:
                            seen_pi = raw_si
                            if ts > max_ts:
                                max_ts = ts
                        elif is_error_retry_pr or is_same_si_eq_ctx_cr:
                            is_err_retry_flag = bool(is_error_retry_pr)
                            if ts > max_ts:
                                max_ts = ts
                        else:
                            is_rev = True
                            did_revert = True
                            seen_pi = raw_si
                            if ts > max_ts:
                                max_ts = ts
                            act_media = [m for m in act_media if m[0] < raw_si]
                            act_media_keys = {(m[0], m[1]) for m in act_media}
                            act_media_bytes = sum(m[2] for m in act_media)
                    elif seen_pi is None or (raw_si > seen_pi and ts >= max_ts - 60.0):
                        seen_pi = raw_si
                        if ts > max_ts:
                            max_ts = ts
                elif ts > max_ts:
                    max_ts = ts

                if ts > 0.0:
                    if min_ts <= 0.0 or ts < min_ts:
                        min_ts = ts
                    if ts > max_ts:
                        max_ts = ts
                last_ctx = tot_tok
                parsed_l.append(
                    _RawStepSlot(
                        step_index=raw_si if raw_si is not None else len(parsed_l),
                        timestamp=ts,
                        model=model_str,
                        input_tokens=inp_tok,
                        cache_read_tokens=cr_tok,
                        output_tokens=out_tok,
                        after_checkpoint=pend_cp,
                        after_revert=is_rev,
                        active_media_count=len(act_media),
                        active_media_bytes=act_media_bytes,
                        is_error_retry=is_err_retry_flag,
                    )
                )
                pend_cp = False
                pend_cp_si = None
                pend_rev = False
                last_err_ts = 0.0

            state["has_user_explicit"] = has_u
            state["has_subagent_marker"] = has_sub
            state["pending_checkpoint"] = pend_cp
            state["pending_cp_si"] = pend_cp_si
            state["pending_revert"] = pend_rev
            state["seen_planner_idx"] = seen_pi
            state["min_step_ts"] = min_ts
            state["max_step_ts"] = max_ts
            state["last_known_ctx"] = last_ctx
            state["active_media"] = act_media
            state["active_media_keys"] = act_media_keys
            state["active_media_bytes_total"] = act_media_bytes
            state["last_error_ts"] = last_err_ts
            state["last_user_si"] = last_u_si
            return did_revert

        inc_state: Optional[Dict[str, Any]] = None
        inc_done = False
        new_offset = 0
        new_tail_sig = b""
        prev_inc = self._transcript_inc_state.get(conv_id)

        try:
            with tp.open("rb") as fh:
                old_offset = int(prev_inc.get("parsed_offset", 0)) if prev_inc is not None else 0
                prev_size = int(prev_inc.get("size", 0)) if prev_inc is not None else 0
                prev_tail_sig: bytes = prev_inc.get("tail_sig", b"") if prev_inc is not None else b""
                # Strictly require file growth past old_offset (or completion of a prior half-line) AND matching 64B tail_sig
                # so same-size or variable-length full rewrites NEVER reuse stale cached steps!
                if (
                    cached is not None
                    and prev_inc is not None
                    and old_offset > 0
                    and prev_tail_sig
                    and stamp[1] > old_offset
                    and (stamp[1] > prev_size or prev_size > old_offset)
                ):
                    sig_len = len(prev_tail_sig)
                    try:
                        fh.seek(max(0, old_offset - sig_len))
                        actual_sig = fh.read(sig_len)
                    except OSError:
                        actual_sig = b""
                    if actual_sig == prev_tail_sig and actual_sig.endswith(b"\n"):
                        chunk = fh.read()
                        last_nl = chunk.rfind(b"\n")
                        if last_nl == -1:
                            # Only a half-written line without trailing newline: NEVER consume half-line!
                            self._transcript_cache[conv_id] = (stamp, cached[1], cached[2], cached[3])
                            self._transcript_cache.move_to_end(conv_id)
                            prev_inc["size"] = stamp[1]
                            self._transcript_inc_state.move_to_end(conv_id)
                            return cached[1], cached[2], cached[3]
                        consumed = chunk[: last_nl + 1]
                        temp_state: Dict[str, Any] = {
                            "parsed": list(cached[1]),
                            "error_ts_list": list(prev_inc["error_ts_list"]),
                            "user_input_snaps": list(prev_inc["user_input_snaps"]),
                            "has_user_explicit": bool(cached[2]),
                            "has_subagent_marker": bool(cached[3]),
                            "pending_checkpoint": bool(prev_inc["pending_checkpoint"]),
                            "pending_cp_si": prev_inc["pending_cp_si"],
                            "pending_revert": bool(prev_inc["pending_revert"]),
                            "seen_planner_idx": prev_inc["seen_planner_idx"],
                            "min_step_ts": float(prev_inc.get("min_step_ts", 0.0)),
                            "max_step_ts": float(prev_inc["max_step_ts"]),
                            "last_known_ctx": int(prev_inc["last_known_ctx"]),
                            "active_media": list(prev_inc["active_media"]),
                            "active_media_keys": set(prev_inc["active_media_keys"]),
                            "active_media_bytes_total": int(prev_inc["active_media_bytes_total"]),
                            "last_error_ts": float(prev_inc["last_error_ts"]),
                            "last_user_si": prev_inc["last_user_si"],
                        }
                        try:
                            did_rev = _parse_lines_into(consumed.splitlines(), temp_state)
                        except Exception:
                            did_rev = True
                        if not did_rev:
                            inc_state = temp_state
                            new_offset = old_offset + len(consumed)
                            new_tail_sig = (prev_tail_sig + consumed)[-64:]
                            inc_done = True

                if not inc_done:
                    fh.seek(0)
                    raw_bytes = fh.read()
                    last_nl = raw_bytes.rfind(b"\n")
                    if last_nl != -1:
                        consumed = raw_bytes[: last_nl + 1]
                        trailing = raw_bytes[last_nl + 1 :]
                        new_offset = last_nl + 1
                        new_tail_sig = consumed[-64:]
                    else:
                        consumed = b""
                        trailing = raw_bytes
                        new_offset = 0
                        new_tail_sig = b""
                    lines_to_parse = consumed.splitlines() if consumed else []
                    if trailing:
                        tr_strip = trailing.strip()
                        # Only accept a complete trailing JSON object (legacy static files lacking final '\n');
                        # never advance incremental offset past an unterminated line.
                        if tr_strip.startswith(b"{") and tr_strip.endswith(b"}"):
                            lines_to_parse.append(tr_strip)
                            new_offset = 0
                            new_tail_sig = b""
                    full_state: Dict[str, Any] = {
                        "parsed": [],
                        "error_ts_list": [],
                        "user_input_snaps": [],
                        "has_user_explicit": False,
                        "has_subagent_marker": False,
                        "pending_checkpoint": False,
                        "pending_cp_si": None,
                        "pending_revert": False,
                        "seen_planner_idx": None,
                        "min_step_ts": 0.0,
                        "max_step_ts": 0.0,
                        "last_known_ctx": 0,
                        "active_media": [],
                        "active_media_keys": set(),
                        "active_media_bytes_total": 0,
                        "last_error_ts": 0.0,
                        "last_user_si": None,
                    }
                    _parse_lines_into(lines_to_parse, full_state)
                    inc_state = full_state
        except OSError:
            return (cached[1], cached[2], cached[3]) if cached is not None else ([], False, False)

        assert inc_state is not None
        parsed: List[Any] = inc_state["parsed"]
        has_user_explicit: bool = inc_state["has_user_explicit"]
        has_subagent_marker: bool = inc_state["has_subagent_marker"]
        error_ts_list: List[Tuple[float, str, int, int, int]] = inc_state["error_ts_list"]
        user_input_snaps: List[Tuple[float, int, int, int]] = inc_state["user_input_snaps"]
        active_media: List[Tuple[int, str, int]] = inc_state["active_media"]
        active_media_bytes_total: int = inc_state["active_media_bytes_total"]
        last_known_ctx: int = inc_state["last_known_ctx"]

        self._transcript_cache[conv_id] = (stamp, parsed, has_user_explicit, has_subagent_marker)
        self._transcript_cache.move_to_end(conv_id)
        self._transcript_inc_state[conv_id] = {
            "size": stamp[1],
            "parsed_offset": new_offset,
            "tail_sig": new_tail_sig,
            "error_ts_list": error_ts_list[-16:],
            "user_input_snaps": user_input_snaps[-16:],
            "pending_checkpoint": inc_state["pending_checkpoint"],
            "pending_cp_si": inc_state["pending_cp_si"],
            "pending_revert": inc_state["pending_revert"],
            "seen_planner_idx": inc_state["seen_planner_idx"],
            "min_step_ts": inc_state["min_step_ts"],
            "max_step_ts": inc_state["max_step_ts"],
            "last_known_ctx": last_known_ctx,
            "active_media": active_media,
            "active_media_keys": inc_state["active_media_keys"],
            "active_media_bytes_total": active_media_bytes_total,
            "last_error_ts": inc_state["last_error_ts"],
            "last_user_si": inc_state["last_user_si"],
        }
        self._transcript_inc_state.move_to_end(conv_id)
        while len(self._transcript_cache) > 320:
            # Evict strictly by oldest file mtime_ns (prevents descending-order scan LRU inversion from dropping the hottest conv!)
            evicted_cid = min(
                self._transcript_cache.keys(),
                key=lambda k: self._transcript_cache[k][0][0],
            )
            self._transcript_cache.pop(evicted_cid, None)
            self._transcript_inc_state.pop(evicted_cid, None)
            self._transcript_error_ts.pop(evicted_cid, None)
            self._transcript_tail_meta.pop(evicted_cid, None)
        while len(self._transcript_inc_state) > 320:
            self._transcript_inc_state.popitem(last=False)
        if error_ts_list:
            self._transcript_error_ts[conv_id] = error_ts_list[-16:]
            self._transcript_error_ts.move_to_end(conv_id)
            while len(self._transcript_error_ts) > 320:
                # Evict by oldest latest error timestamp rather than insertion order (prevents descending-scan LRU inversion)
                oldest_ecid = min(
                    self._transcript_error_ts.keys(),
                    key=lambda k: self._transcript_error_ts[k][-1][0] if self._transcript_error_ts[k] else 0.0,
                )
                self._transcript_error_ts.pop(oldest_ecid, None)
        else:
            self._transcript_error_ts.pop(conv_id, None)
        if user_input_snaps or active_media:
            self._transcript_tail_meta[conv_id] = (
                user_input_snaps[-16:],
                len(active_media),
                active_media_bytes_total,
                last_known_ctx,
            )
            self._transcript_tail_meta.move_to_end(conv_id)
            while len(self._transcript_tail_meta) > 320:
                oldest_tcid = min(
                    self._transcript_tail_meta.keys(),
                    key=lambda k: self._transcript_tail_meta[k][0][-1][0] if self._transcript_tail_meta[k][0] else 0.0,
                )
                self._transcript_tail_meta.pop(oldest_tcid, None)
        else:
            self._transcript_tail_meta.pop(conv_id, None)
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
            rule_timestamps = self._observe_and_get_rule_timestamps(now, since_ts=0.0)

            all_steps = self._classify_steps_cached(
                conv_id,
                raw_steps,
                switch_timestamps,
                rule_change_timestamps=rule_timestamps,
            )
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
            cur_online_lbl = self._account_cache.short_label if self._account_cache.online else ""
            is_custom_detail_view = False
            if raw_view == "__all__":
                is_custom_detail_view = True
            elif use_acct_filter:
                norm_view_chk = _mask_short(raw_view) or raw_view
                is_custom_detail_view = (not cur_online_lbl) or (norm_view_chk.lower() != cur_online_lbl.lower())
            use_range_filter = (hr_key != "30m") or (hr_key == "30m" and is_custom_detail_view)

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
                elif use_range_filter:
                    win_sec = max(1, int(self.config.get("log_sliding_window_min", 30))) * 60.0
                    range_start_ts = now - win_sec
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

            fallback_detail_model = ""
            for gen in reversed(sel_gens):
                if gen is not None and getattr(gen, "model_label", ""):
                    fallback_detail_model = resolve_model_label(gen.model_label, enum_map=self._model_enum_map)
                    if fallback_detail_model:
                        break
            if not fallback_detail_model:
                for gen in reversed(gens):
                    if gen is not None and getattr(gen, "model_label", ""):
                        fallback_detail_model = resolve_model_label(gen.model_label, enum_map=self._model_enum_map)
                        if fallback_detail_model:
                            break
            points: List[ConversationDetailPoint] = []
            max_ctx_limit = 0
            latest_resolved_model_raw = ""
            last_known_detail_model = ""
            for st, gen in zip(steps, sel_gens):
                if gen is not None and gen.max_context_tokens > max_ctx_limit:
                    max_ctx_limit = gen.max_context_tokens
                raw_m = st.model
                if not raw_m:
                    g_lbl = getattr(gen, "model_label", "") if gen is not None else ""
                    raw_m = (
                        resolve_model_label(g_lbl, enum_map=self._model_enum_map)
                        if g_lbl
                        else (last_known_detail_model or fallback_detail_model)
                    )
                if raw_m:
                    last_known_detail_model = raw_m
                    latest_resolved_model_raw = raw_m
                points.append(
                    ConversationDetailPoint(
                        step_index=st.step_index,
                        timestamp=st.timestamp,
                        model=format_model_name(raw_m, enum_map=self._model_enum_map),
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
                        is_account_switch=st.is_account_switch,
                        active_media_count=st.active_media_count,
                        active_media_bytes=st.active_media_bytes,
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
            cacheable_cr_sum = sum(s.cache_read_tokens for s in steps if s.classification in ("hit", "abnormal_miss"))
            cacheable_tok_sum = sum(s.total_tokens for s in steps if s.classification in ("hit", "abnormal_miss"))
            cacheable_token_hit_ratio: Optional[float] = (
                (cacheable_cr_sum / cacheable_tok_sum) * 100.0 if cacheable_tok_sum > 0 else None
            )

            ttfts = sorted(p.ttft_sec for p in points if p.ttft_sec is not None)
            avg_ttft = (sum(ttfts) / len(ttfts)) if ttfts else None
            max_ttft = ttfts[-1] if ttfts else None
            p95_ttft = ttfts[min(len(ttfts) - 1, int(round(0.95 * (len(ttfts) - 1))))] if ttfts else None
            # Token-weighted avg_tps over meaningful streaming steps (>=0.5s) resists short tool-call jitter
            weighted_out = sum(p.output_tokens for p in points if p.output_tokens > 0 and p.streaming_sec is not None and p.streaming_sec >= 0.5)
            weighted_strm = sum(p.streaming_sec for p in points if p.output_tokens > 0 and p.streaming_sec is not None and p.streaming_sec >= 0.5)
            if weighted_strm >= 0.5 and weighted_out > 0:
                avg_tps: Optional[float] = weighted_out / weighted_strm
            else:
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
                        fresh_invalid = _format_freshness_invalid_reason(consec_miss, latest.reason)
                    else:
                        fresh_rem = int(self.config["freshness_window_sec"] - age)

            eff_latest_model_raw = latest.model or latest_resolved_model_raw
            return ConversationDetailReport(
                conversation_id=conv_id,
                short_id=conv_id[:6],
                is_subagent=is_subagent,
                model=format_model_name(eff_latest_model_raw, enum_map=self._model_enum_map),
                model_raw=eff_latest_model_raw,
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
                consec_miss=consec_miss,
                cacheable_token_hit_ratio=cacheable_token_hit_ratio,
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
            with _open_localhost_url(req, timeout=1.2) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            summaries = data.get("trajectorySummaries") or {}
            if isinstance(summaries, dict):
                for cid, entry in summaries.items():
                    if not isinstance(entry, dict):
                        continue
                    summary = str(entry.get("summary") or "").strip()
                    if summary:
                        self._conv_titles[str(cid)] = summary
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError):
            pass

    def _load_vscdb_titles_cached(self) -> Dict[str, str]:
        """Read-only offline fallback for conversation titles from Antigravity state.vscdb."""
        if self.vscdb_path is None:
            return self._vscdb_titles_cache
        if self._in_poll and self._vscdb_last_stat_poll_seq == self._poll_seq:
            return self._vscdb_titles_cache
        if self._in_poll:
            self._vscdb_last_stat_poll_seq = self._poll_seq
        if not self.vscdb_path.is_file():
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

    @staticmethod
    def _sanitize_rpc_error(exc: BaseException) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTPError({exc.code})"
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", None)
            rname = type(reason).__name__ if reason is not None else "URLError"
            return f"URLError({rname})"
        return type(exc).__name__

    def _poll_account_and_quota(self, now: float, active_family: str = "gemini") -> AccountQuotaInfo:
        info = AccountQuotaInfo(observed_at=now, quota_family=active_family)
        live_email = ""
        prev_active_pid = self._active_hub_pid
        prev_active_port = self._active_hub_port
        prev_active_token = self._active_hub_token
        prev_active_cft = self._active_hub_creation_ft

        any_hub_pid_alive = False
        max_http_probes = 8
        http_probes_used = 0
        probed_endpoints: set[Tuple[int, int, str]] = set()
        last_err_kind = ""
        last_err_port = 0
        hub_file_count = 0

        def _try_hub_endpoint(
            pid: int,
            port: int,
            token: str,
            *,
            allow_alive_fallback: bool,
            timeout: float = 1.5,
            record_error: bool = True,
        ) -> bool:
            nonlocal live_email, any_hub_pid_alive, http_probes_used, last_err_kind, last_err_port
            if not (1 <= port <= 65535) or not token or ("\r" in token) or ("\n" in token):
                return False
            ep = (pid, port, token)
            if ep in probed_endpoints:
                return False
            probed_endpoints.add(ep)
            http_probes_used += 1
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "x-codeium-csrf-token": token,
                        "Connect-Protocol-Version": "1",
                    },
                )
                with _open_localhost_url(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid GetUserStatus JSON payload")
                status = payload.get("userStatus") or {}
                if not isinstance(status, dict):
                    status = {}
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
                any_hub_pid_alive = True
                self._active_hub_pid = pid
                self._active_hub_port = port
                self._active_hub_token = token
                self._active_hub_creation_ft = _get_windows_pid_creation_ft(pid)

                configs = (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
                pools: Dict[str, QuotaPoolInfo] = {}
                for cfg in configs:
                    if not isinstance(cfg, dict):
                        continue
                    lbl = str(cfg.get("label", "") or "")
                    mid = str((cfg.get("modelOrAlias") or {}).get("model", "") or "")
                    if mid and lbl:
                        mkey = mid.strip().upper()
                        self._model_enum_map[mkey] = lbl.strip()
                        self._model_enum_map.move_to_end(mkey)
                        while len(self._model_enum_map) > 256:
                            self._model_enum_map.popitem(last=False)
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
                email_changed = live_email.lower() != self._last_weekly_email.lower()
                if email_changed or (now - self._last_titles_poll_ts >= 15.0):
                    self._poll_hub_titles(port, token)
                    self._last_titles_poll_ts = now
                return True
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError) as exc:
                if record_error:
                    last_err_kind = self._sanitize_rpc_error(exc)
                    last_err_port = port
                if (
                    allow_alive_fallback
                    and not any_hub_pid_alive
                    and _is_hub_language_server_pid(
                        pid,
                        expected_active_pid=prev_active_pid,
                        expected_creation_ft=prev_active_cft,
                    )
                ):
                    any_hub_pid_alive = True
                return False

        # 1. Fast path: probe cached active Hub first if its PID is still the exact same live process
        if (
            prev_active_pid > 0
            and prev_active_port > 0
            and prev_active_token
            and self._is_cached_hub_still_same_process()
        ):
            _try_hub_endpoint(
                prev_active_pid,
                prev_active_port,
                prev_active_token,
                allow_alive_fallback=True,
            )

        # 2. Race-safe full scan of daemon_dir/hub_*.json (no [:40] truncation, bounded by max_http_probes)
        if not info.online and self.daemon_dir.is_dir():
            try:
                raw_hub_files = list(self.daemon_dir.glob("hub_*.json"))
            except OSError:
                raw_hub_files = []
            hub_entries: List[Tuple[float, Path]] = []
            for hp in raw_hub_files:
                try:
                    hub_entries.append((hp.stat().st_mtime, hp))
                except OSError:
                    continue
            hub_entries.sort(key=lambda item: item[0], reverse=True)
            hub_file_count = len(hub_entries)

            fallback_alive_hubs: List[Tuple[int, int, str]] = []
            for _mtime, hp in hub_entries:
                try:
                    hub = json.loads(hp.read_text(encoding="utf-8"))
                    if not isinstance(hub, dict):
                        continue
                    pid = int(hub.get("pid", 0) or 0)
                    port = int(hub.get("httpPort", 0) or 0)
                    token = str(hub.get("csrfToken", "") or "")
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                if not (pid > 0 and 1 <= port <= 65535 and token):
                    continue
                if os.name == "nt":
                    pid_alive, exe_name, creation_ft = _inspect_windows_pid(pid)
                    if not pid_alive:
                        continue
                    is_ls_proc = bool(
                        (exe_name and ("language_server" in exe_name or exe_name in ("antigravity.exe", "antigravity")))
                        or (
                            prev_active_pid > 0
                            and pid == prev_active_pid
                            and (
                                (prev_active_cft > 0 and creation_ft > 0 and creation_ft == prev_active_cft)
                                or (prev_active_cft <= 0 and not exe_name)
                            )
                        )
                    )
                    allow_fallback_probe = bool((not is_ls_proc) and (pid == os.getpid() or not exe_name))
                else:
                    if not _is_windows_pid_alive(pid):
                        continue
                    is_ls_proc = bool(prev_active_pid > 0 and pid == prev_active_pid)
                    allow_fallback_probe = not is_ls_proc
                if is_ls_proc:
                    any_hub_pid_alive = True
                    if http_probes_used < max_http_probes:
                        if _try_hub_endpoint(pid, port, token, allow_alive_fallback=True):
                            break
                    else:
                        break
                elif allow_fallback_probe and len(fallback_alive_hubs) < max_http_probes:
                    fallback_alive_hubs.append((pid, port, token))

            # 3. Fallback HTTP probe ONLY for same-PID test servers or access-denied unnamed PIDs:
            #    Uses short timeout & never pollutes last_err_kind or sets any_hub_pid_alive=True on failure.
            if not info.online:
                for pid, port, token in fallback_alive_hubs:
                    if http_probes_used >= max_http_probes:
                        break
                    if _try_hub_endpoint(
                        pid,
                        port,
                        token,
                        allow_alive_fallback=False,
                        timeout=0.25,
                        record_error=False,
                    ):
                        break

        # Enrich weekly quota: prefer official live Hub RetrieveUserQuotaSummary (~7ms local RPC,
        # matches official View Usage popup in real time without depending on Cockpit); fall back to
        # read-only Cockpit SQLite ONLY when older Antigravity builds lack the RPC or return no weekly pool.
        if info.online and live_email:
            email_low = live_email.lower()
            if email_low != self._last_weekly_email.lower() or (now - self._last_weekly_poll_ts >= 30.0):
                got_live_weekly = False
                if self._active_hub_port > 0 and self._active_hub_token:
                    got_live_weekly = self._poll_live_quota_summary(
                        self._active_hub_port,
                        self._active_hub_token,
                        info,
                    )
                if not got_live_weekly and self.cockpit_quota_db.is_file():
                    self._enrich_weekly_from_cockpit(info, live_email, now)
                self._last_weekly_email = email_low
                self._last_weekly_poll_ts = now
                self._last_weekly_pools = {
                    pn: (pl.weekly_pct, pl.weekly_reset_at, pl.five_hour_pct, pl.five_hour_reset_at)
                    for pn, pl in info.pools.items()
                }
            else:
                for pn, (wk_pct, wk_rst, fh_pct, fh_rst) in self._last_weekly_pools.items():
                    pool = info.pools.get(pn)
                    if pool is None and (wk_pct is not None or fh_pct is not None):
                        pool = info.pools.setdefault(pn, QuotaPoolInfo(name=pn))
                    if pool is not None:
                        if wk_pct is not None:
                            pool.weekly_pct = wk_pct
                        if wk_rst:
                            pool.weekly_reset_at = wk_rst
                        if pool.five_hour_pct is None and fh_pct is not None:
                            pool.five_hour_pct = fh_pct
                        if not pool.five_hour_reset_at and fh_rst:
                            pool.five_hour_reset_at = fh_rst
            self._sync_account_active_pool(info, active_family)

        self._last_probe_any_hub_pid_alive = bool(info.online or any_hub_pid_alive)
        self._last_probe_seq += 1
        self._last_probe_diag = {
            "err": "" if info.online else last_err_kind,
            "port": self._active_hub_port if info.online else last_err_port,
            "probes": http_probes_used,
            "files": hub_file_count,
            "any_alive": self._last_probe_any_hub_pid_alive,
        }
        return info

    def _poll_live_quota_summary(
        self,
        port: int,
        token: str,
        info: AccountQuotaInfo,
        *,
        timeout: float = 1.5,
    ) -> bool:
        """Query Antigravity's native localhost RetrieveUserQuotaSummary RPC for live weekly/5h pools."""
        if not (1 <= port <= 65535) or not token or ("\r" in token) or ("\n" in token):
            return False
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": token,
                    "Connect-Protocol-Version": "1",
                },
            )
            with _open_localhost_url(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        root = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        groups = root.get("groups")
        if not isinstance(groups, list):
            return False
        got_weekly = False
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            g_name = str(grp.get("displayName", "") or "").lower()
            buckets = grp.get("buckets")
            if not isinstance(buckets, list):
                continue
            for bkt in buckets:
                if not isinstance(bkt, dict):
                    continue
                bid = str(bkt.get("bucketId", "") or "").lower()
                win = str(bkt.get("window", "") or "").lower()
                pool_name = ""
                if "gemini" in bid or "gemini" in g_name:
                    pool_name = "gemini"
                elif "3p" in bid or "claude" in g_name or "gpt" in g_name:
                    pool_name = "3p"
                if not pool_name:
                    continue
                is_weekly = (win == "weekly") or ("weekly" in bid)
                is_5h = (win == "5h") or ("5h" in bid)
                if not is_weekly and not is_5h:
                    continue
                frac = bkt.get("remainingFraction")
                pct_val: Optional[float] = None
                if isinstance(frac, (int, float)) and not isinstance(frac, bool):
                    fval = float(frac)
                    if math.isfinite(fval):
                        pct_val = round(max(0.0, min(100.0, fval * 100.0)), 2)
                reset_iso = _normalize_reset_time(bkt.get("resetTime"))
                if pct_val is None and not reset_iso:
                    continue
                pool = info.pools.setdefault(pool_name, QuotaPoolInfo(name=pool_name))
                if is_weekly:
                    if pct_val is not None:
                        pool.weekly_pct = pct_val
                        got_weekly = True
                    if reset_iso:
                        pool.weekly_reset_at = reset_iso
                elif is_5h:
                    if pool.five_hour_pct is None and pct_val is not None:
                        pool.five_hour_pct = pct_val
                    if reset_iso and not pool.five_hour_reset_at:
                        pool.five_hour_reset_at = reset_iso
        return got_weekly

    @staticmethod
    def _file_stamp_3(p: Path) -> Tuple[int, int, int]:
        try:
            st = p.stat()
            return (int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            return (0, 0, 0)

    def _enrich_weekly_from_cockpit(self, info: AccountQuotaInfo, raw_email: str, now: float) -> None:
        cockpit_dir = self.cockpit_quota_db.parent
        acc_db = cockpit_dir / "antigravity_accounts.sqlite"
        accounts_json = cockpit_dir / "accounts.json"
        cockpit_wal = Path(str(self.cockpit_quota_db) + "-wal")
        acc_wal = Path(str(acc_db) + "-wal")
        email_low = (raw_email or "").lower()

        cache_key = (
            email_low,
            self._file_stamp_3(self.cockpit_quota_db),
            self._file_stamp_3(cockpit_wal),
            self._file_stamp_3(acc_db),
            self._file_stamp_3(acc_wal),
            self._file_stamp_3(accounts_json),
        )

        if self._cockpit_weekly_cache_key == cache_key:
            first_rows = self._cockpit_weekly_cache_rows
        else:
            bound_acc_id = ""
            acc_db_err = False
            # 1. Exact email match in Cockpit's read-only antigravity_accounts.sqlite
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
                    acc_db_err = True

            # 2. Exact email match in Cockpit accounts.json
            if not bound_acc_id and accounts_json.is_file():
                try:
                    data = json.loads(accounts_json.read_text(encoding="utf-8"))
                    for acc in data.get("accounts") or []:
                        if isinstance(acc, dict) and str(acc.get("email", "")).lower() == email_low:
                            bound_acc_id = str(acc.get("id") or acc.get("account_id") or "")
                            if bound_acc_id:
                                break
                except (OSError, json.JSONDecodeError):
                    pass

            first_rows = {}
            db_ok = not (acc_db_err and not bound_acc_id)
            # Never fall back to unrelated bindAccountId when email doesn't match (prevents cross-account pollution)
            if bound_acc_id:
                try:
                    resolved = self.cockpit_quota_db.resolve().as_posix()
                    encoded = urllib.parse.quote(resolved, safe="/:")
                    uri = f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"
                    conn = sqlite3.connect(uri, uri=True, timeout=0.3)
                    try:
                        cur = conn.cursor()
                        cols = {str(r[1]).lower() for r in cur.execute("PRAGMA table_info(quota_observations)").fetchall()}
                        has_reset_col = "reset_at" in cols
                        has_pk_prefix = ("platform" in cols) and ("account_created_at" in cols)
                        metrics_4 = ("gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h")
                        reset_expr = "reset_at" if has_reset_col else "NULL"

                        def _safe_obs_float(v: Any) -> float:
                            try:
                                return float(v or 0.0)
                            except (ValueError, TypeError):
                                return 0.0

                        if has_pk_prefix:
                            # Fast PK full-prefix path (WITHOUT ROWID native Cockpit schema: ~15ms vs ~400ms scan)
                            created_rows = [
                                r[0]
                                for r in cur.execute(
                                    "SELECT DISTINCT account_created_at FROM quota_observations "
                                    "WHERE platform = 'antigravity' AND account_id = ?",
                                    (bound_acc_id,),
                                ).fetchall()
                            ]
                            pk_sql = (
                                f"SELECT remaining, observed_at, {reset_expr} FROM quota_observations "
                                "WHERE platform = 'antigravity' AND account_id = ? AND account_created_at = ? AND metric = ? "
                                "ORDER BY observed_at DESC LIMIT 1"
                            )
                            for m_name in metrics_4:
                                best_row: Optional[Tuple[Any, float, Any]] = None
                                for ca in created_rows:
                                    r = cur.execute(pk_sql, (bound_acc_id, ca, m_name)).fetchone()
                                    if r is not None:
                                        obs_f = _safe_obs_float(r[1])
                                        if best_row is None or obs_f > best_row[1]:
                                            best_row = (r[0], obs_f, r[2])
                                if best_row is not None:
                                    first_rows[m_name] = best_row
                        else:
                            # Legacy / simplified test schema fallback (WITHOUT ROWID safe: never reference rowid)
                            sel_sql = (
                                f"SELECT metric, remaining, observed_at, {reset_expr} FROM quota_observations "
                                "WHERE account_id = ? AND metric IN ('gemini-weekly','gemini-5h','3p-weekly','3p-5h') "
                                "ORDER BY observed_at DESC"
                            )
                            for row in cur.execute(sel_sql, (bound_acc_id,)):
                                m_name = str(row[0] or "")
                                if m_name and m_name not in first_rows:
                                    first_rows[m_name] = (row[1], _safe_obs_float(row[2]), row[3])
                                    if len(first_rows) >= 4:
                                        break
                    finally:
                        conn.close()
                except sqlite3.Error:
                    db_ok = False
            if db_ok:
                self._cockpit_weekly_cache_key = cache_key
                self._cockpit_weekly_cache_rows = first_rows

        stale_cutoff_sec = now - 7 * 86400
        for pool_name in ("gemini", "3p"):
            for kind in ("weekly", "5h"):
                row = first_rows.get(f"{pool_name}-{kind}")
                if row is None or row[0] is None:
                    continue
                obs_raw = float(row[1] or 0.0)
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
        "latest_cache_reason": c.latest_cache_reason,
        "latest_cached_in": c.latest_cached_in,
        "latest_uncached_in": c.latest_uncached_in,
        "latest_timestamp": round(c.latest_timestamp, 3),
        "latest_time_local": _epoch_to_iso_local(c.latest_timestamp) if c.latest_timestamp > 0 else None,
        "active_media_count": c.active_media_count,
        "active_media_bytes": c.active_media_bytes,
        "media_ttft_exempted": c.media_ttft_exempted,
        "is_carryover_preview": c.is_carryover_preview,
        "large_ctx_ttft_allowance_sec": round(c.large_ctx_ttft_allowance_sec, 1),
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
                "is_revert": s.is_revert,
                "is_compact": s.is_compact,
                "is_account_switch": s.is_account_switch,
                "active_media_count": s.active_media_count,
                "active_media_bytes": s.active_media_bytes,
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
            "hub_pid_alive": bool(getattr(snap, "hub_pid_alive", False)),
            "tier": snap.account.tier,
            "plan_status": snap.account.plan_status,
            "quota_family": snap.account.quota_family,
            "five_hour_pct": round(snap.account.five_hour_pct, 2) if snap.account.five_hour_pct is not None else None,
            "weekly_pct": round(snap.account.weekly_pct, 2) if snap.account.weekly_pct is not None else None,
            "five_hour_reset_at": snap.account.five_hour_reset_at,
            "weekly_reset_at": snap.account.weekly_reset_at,
            "pools": pools_out,
        },
        "steps": (
            lambda _has24, _eff, _acct24, _tot24, _m24, _s24, _tm24, _ts24, _ph24, _pl24: {
                "scope_label": snap.steps_scope_label,
                "window_mode": "rolling_24h" if _has24 else "today",
                "effective_steps": _eff,
                "rolling_24h_steps": _eff if _has24 else snap.today_steps,
                "account_24h_steps": _acct24,
                "total_24h_steps": _tot24,
                "main_24h_steps": _m24,
                "subagent_24h_steps": _s24,
                "total_main_24h_steps": _tm24,
                "total_subagent_24h_steps": _ts24,
                "peak_hour_steps_24h": _ph24,
                "peak_hour_label_24h": _pl24,
                "today_steps": snap.today_steps,
                "account_today_steps": snap.account_today_steps,
                "total_today_steps": snap.total_today_steps,
                "main_today_steps": snap.main_today_steps,
                "subagent_today_steps": snap.subagent_today_steps,
                "total_main_today_steps": snap.total_main_today_steps,
                "total_subagent_today_steps": snap.total_subagent_today_steps,
                "daily_steps_warn": snap.daily_steps_warn,
                "daily_steps_alert": snap.daily_steps_alert,
                "daily_steps_base_alert": snap.daily_steps_base_alert,
                "daily_steps_limit_mode": snap.daily_steps_limit_mode,
                "daily_steps_limit_note": snap.daily_steps_limit_note,
                "account_hist_safe_peak": snap.account_hist_safe_peak,
                "remaining_to_warn": max(0, snap.daily_steps_warn - _eff),
                "remaining_to_alert": max(0, snap.daily_steps_alert - _eff),
                "usage_pct": round((_eff / max(1, snap.daily_steps_alert)) * 100.0, 1),
                "last_1h_steps": snap.last_1h_steps,
                "peak_hour_steps_today": snap.peak_hour_steps_today,
                "peak_hour_label": snap.peak_hour_label,
            }
        )(
            snap.rolling_24h_steps is not None,
            snap.rolling_24h_steps if snap.rolling_24h_steps is not None else snap.today_steps,
            snap.account_24h_steps if snap.account_24h_steps is not None else snap.account_today_steps,
            snap.total_24h_steps if snap.total_24h_steps is not None else snap.total_today_steps,
            snap.main_24h_steps if snap.rolling_24h_steps is not None else snap.main_today_steps,
            snap.subagent_24h_steps if snap.rolling_24h_steps is not None else snap.subagent_today_steps,
            snap.total_main_24h_steps if snap.rolling_24h_steps is not None else snap.total_main_today_steps,
            snap.total_subagent_24h_steps if snap.rolling_24h_steps is not None else snap.total_subagent_today_steps,
            snap.peak_hour_steps_24h if snap.rolling_24h_steps is not None else snap.peak_hour_steps_today,
            snap.peak_hour_label_24h if snap.rolling_24h_steps is not None else snap.peak_hour_label,
        ),
        "cache": {
            "primary_hit_rate": round(snap.primary_hit_rate, 2) if snap.primary_hit_rate is not None else None,
            "primary_hit_classification": snap.primary_hit_classification,
            "primary_hit_reason": snap.primary_hit_reason,
            "primary_hit_is_compact": getattr(snap, "primary_hit_is_compact", False),
            "primary_hit_is_revert": getattr(snap, "primary_hit_is_revert", False),
            "consec_miss": snap.max_consec_miss,
            "t1_consec_threshold": T1_CONSEC_MISS_THRESHOLD,
            "max_abnormal_recent10": snap.max_abnormal_recent10,
            "worst_recent10_hit_ratio": round(snap.worst_recent10_hit_ratio, 4) if snap.worst_recent10_hit_ratio is not None else None,
            "wasted_cold_tokens_total": snap.wasted_cold_tokens_total,
            "min_freshness_sec": snap.min_freshness_sec,
            "min_freshness_conv": snap.min_freshness_conv,
            "min_freshness_ctx": snap.min_freshness_ctx,
            "min_freshness_invalid_reason": snap.min_freshness_invalid_reason,
            "freshness_window_sec": snap.freshness_window_sec,
            "culprit_summary": getattr(snap, "cache_culprit_summary", ""),
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
            "heavy_media_timeout": snap.heavy_media_timeout_recent,
            "403": snap.recent_403,
            "retry": snap.recent_retry,
            "proxy_outage": snap.proxy_outage_recent,
            "proxy_outage_note": snap.proxy_outage_note,
            "t2_recovery_note": snap.t2_recovery_note,
            "log_session_start_ts": round(float(getattr(snap, "log_session_start_ts", 0.0) or 0.0), 3),
            "log_session_start_local": (
                _epoch_to_iso_local(float(getattr(snap, "log_session_start_ts", 0.0) or 0.0))
                if float(getattr(snap, "log_session_start_ts", 0.0) or 0.0) > 0.0
                else None
            ),
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
        "carryover_conversations": [
            _serialize_conversation_summary(c, include_steps=False)
            for c in snap.carryover_conversations
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
    tok_hit_part = (
        f" | Token命中 {report.cacheable_token_hit_ratio:.1f}%"
        if getattr(report, "cacheable_token_hit_ratio", None) is not None
        else ""
    )
    lines = [
        f"Antigravity 单对话长线体检 ({title_part} · {role} · {model_overview}{scope_part})",
        f"步数概况: 总计 {report.total_steps} 步 (今日 {report.today_steps} 步) | 压缩 {report.compact_count} 次{rev_s}",
        f"请求体规模: 当前 {format_tokens(report.latest_context_tokens)} ({format_delta_tokens(report.latest_context_delta)}) | 峰值 {format_tokens(report.peak_context_tokens)}",
        f"缓存统计: 有效命中率 {hit_ratio}{tok_hit_part} (命中 {report.cache_hit_steps} / 异常掉缓存 {report.abnormal_miss_steps} / 正常冷启 {report.expected_cold_steps})",
        f"速度统计: TTFT 均 {avg_t} / P95 {p95_t} / 峰 {max_t} | 平均吞吐 {avg_p}",
    ]
    cur_mc = report.points[-1].active_media_count if report.points else 0
    cur_mb = report.points[-1].active_media_bytes if report.points else 0
    peak_media_pt = max(report.points, key=lambda p: (p.active_media_count, p.active_media_bytes)) if report.points else None
    if peak_media_pt is not None and peak_media_pt.active_media_count > 0:
        cur_m_s = f"图{cur_mc}({format_media_size(cur_mb)})" if cur_mc > 0 else "0项"
        peak_m_s = f"图{peak_media_pt.active_media_count}({format_media_size(peak_media_pt.active_media_bytes)})"
        lines.append(
            f"多模态载荷: 当前 {cur_m_s} / 峰值 {peak_m_s}（多图每轮随请求重传会拉高 TTFT 上传耗时，引擎已自动豁免暗限速误判）"
        )
    large_ctx_allow = compute_large_ctx_ttft_allowance(report.latest_context_tokens, report.max_context_tokens)
    if large_ctx_allow >= 10.0:
        lines.append(
            f"大上下文校准: 当前 {format_tokens(report.latest_context_tokens)} 逼近压缩窗（判定自动校准 TTFT 基线 +{large_ctx_allow:.0f}s，真实数值如实保留）"
        )
    lines.append("最近 12 步明细（新 → 旧）:")
    for pt in reversed(report.points[-12:]):
        hm = datetime.datetime.fromtimestamp(pt.timestamp).strftime("%m-%d %H:%M:%S") if pt.timestamp > 0 else "-- --:--:--"
        t_s = f"{pt.ttft_sec:.1f}s" if pt.ttft_sec is not None else "—"
        p_s = f"{pt.tps:.0f}t/s" if pt.tps is not None else "—"
        base_tag = CACHE_REASON_CN_MAP.get(pt.reason, pt.reason)
        if pt.reason in ("cached", "cached_diluted"):
            if pt.is_revert:
                r_tag = f"回溯·{base_tag}"
            elif getattr(pt, "is_compact", False):
                r_tag = f"压缩·{base_tag}"
            elif getattr(pt, "is_account_switch", False):
                r_tag = f"换号·{base_tag}"
            else:
                r_tag = base_tag
        else:
            r_tag = base_tag
        m_part = f" │ 图{pt.active_media_count}({format_media_size(pt.active_media_bytes)})" if pt.active_media_count > 0 else ""
        lines.append(
            f"  #{pt.step_index:<4} {hm} │ [{pt.model}] │ {format_tokens(pt.total_tokens):>6} ({format_delta_tokens(pt.context_delta):>7}){m_part} "
            f"│ 缓存 {pt.hit_rate:5.1f}% │ 首字 {t_s:>6} │ {p_s:>6} [{r_tag}]"
        )
    return mask_email("\n".join(lines))


def _format_hit_with_cold_badge(
    hit_rate: Optional[float],
    classification: str,
    reason: str,
    *,
    is_compact: bool = False,
) -> str:
    if hit_rate is None:
        return "—"
    if classification == "expected_cold":
        badge = COLD_SHORT_BADGE_MAP.get(reason, "冷启")
        return f"{hit_rate:.1f}%({badge})"
    if classification == "abnormal_miss" and reason == "partial_miss":
        badge = COLD_SHORT_BADGE_MAP.get(reason, "部分失")
        return f"{hit_rate:.1f}%({badge})"
    if is_compact and classification == "hit":
        return f"{hit_rate:.1f}%(压缩)"
    return f"{hit_rate:.1f}%"


def format_text_summary(snap: MonitorSnapshot) -> str:
    """Build a clean, email-masked plain-text health summary for sharing or CLI output."""
    ts_str = datetime.datetime.fromtimestamp(snap.generated_at).strftime("%Y-%m-%d %H:%M:%S")
    hit_str = _format_hit_with_cold_badge(
        snap.primary_hit_rate,
        snap.primary_hit_classification,
        getattr(snap, "primary_hit_reason", ""),
        is_compact=bool(getattr(snap, "primary_hit_is_compact", False)),
    )
    tps_str = f"{snap.primary_tps:.1f} tok/s" if snap.primary_tps is not None else "—"
    ttft_str = f"{snap.primary_ttft:.1f}s" if snap.primary_ttft is not None else "—"
    if snap.primary_ttft_avg is not None and snap.primary_ttft_max is not None:
        ttft_str += f" (近10步均 {snap.primary_ttft_avg:.1f}s / 峰 {snap.primary_ttft_max:.1f}s)"
    acc_5h = f"{snap.account.five_hour_pct:.1f}%" if snap.account.five_hour_pct is not None else "—"
    acc_wk = f"{snap.account.weekly_pct:.0f}%" if snap.account.weekly_pct is not None else "—"
    limit_note = getattr(snap, "daily_steps_limit_note", "")
    limit_badge = f" [自适应·{limit_note}]" if (getattr(snap, "daily_steps_limit_mode", "default") != "default" and limit_note) else (" [自适应]" if getattr(snap, "daily_steps_limit_mode", "default") != "default" else "")
    has_24h = getattr(snap, "rolling_24h_steps", None) is not None
    if has_24h:
        eff_steps = int(snap.rolling_24h_steps or 0)
        eff_main = int(getattr(snap, "main_24h_steps", 0))
        eff_sub = int(getattr(snap, "subagent_24h_steps", 0))
        eff_tot = int(snap.total_24h_steps) if snap.total_24h_steps is not None else int(snap.total_today_steps)
        eff_pk_steps = int(getattr(snap, "peak_hour_steps_24h", 0))
        eff_pk_lbl = getattr(snap, "peak_hour_label_24h", "")
        steps_str = f"近24h {snap.steps_scope_label} {eff_steps}/{snap.daily_steps_alert}{limit_badge}"
        steps_str += f" (主 {eff_main} / 子 {eff_sub} | 近1h {snap.last_1h_steps}"
        if eff_pk_steps > 0 and eff_pk_lbl:
            steps_str += f" | 峰值 {eff_pk_steps}@{eff_pk_lbl}"
        steps_str += f" | 今日自然日 {snap.today_steps})"
        if eff_tot > eff_steps:
            steps_str += f" · 全机 {eff_tot}"
    else:
        steps_str = f"{snap.steps_scope_label} {snap.today_steps}/{snap.daily_steps_alert}{limit_badge}"
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
    if getattr(snap, "heavy_media_timeout_recent", 0) > 0:
        lines.append(f"多图超时: 近窗 {snap.heavy_media_timeout_recent} 次流超时归因为多图大包上传超时（已免误判红灯 T2）")
    if getattr(snap, "proxy_outage_note", ""):
        lines.append(f"代理状态: {snap.proxy_outage_note}")
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
    if getattr(snap, "carryover_conversations", None):
        carry_parts = []
        for cc in snap.carryover_conversations[:2]:
            ctitle = f" {truncate_display_width(cc.title, 16)}" if cc.title else ""
            carry_parts.append(f"↪{cc.short_id}{ctitle} ({cc.model} {format_tokens(cc.context_tokens)}, 待新号首步建缓)")
        lines.append("换号过渡: " + " | ".join(carry_parts))
    if snap.active_conversations:
        main_list = [c for c in snap.active_conversations if not c.is_subagent]
        sub_list = [c for c in snap.active_conversations if c.is_subagent]
        lines.append(f"活跃对话 (主对话 {len(main_list)} / 子代理 {len(sub_list)}，展示近 10 步):")
        ordered = main_list[:6] + sub_list[:4]
        for c in ordered:
            tag = "[子]" if c.is_subagent else "[主]"
            bar = "".join(
                "!" if (s.classification == "abnormal_miss" and s.reason == "partial_miss")
                else (
                    "×" if s.classification == "abnormal_miss"
                    else (
                        "◇" if ((s.is_compact or s.reason == "compact_reset") and not s.is_revert)
                        else ("▒" if (s.is_revert or s.classification == "expected_cold") else "█")
                    )
                )
                for s in c.recent_steps
            )
            delta_s = f"({format_delta_tokens(c.latest_context_delta)})" if c.latest_context_delta != 0 else ""
            sp = f" {c.tps:.0f}t/s" if c.tps is not None else ""
            tf = f" TTFT {c.ttft_sec:.1f}s" if c.ttft_sec is not None else ""
            if c.ttft_avg_sec is not None and c.ttft_max_sec is not None and len([x for x in c.recent_ttfts if x is not None]) >= 2:
                tf += f"(均{c.ttft_avg_sec:.0f}/峰{c.ttft_max_sec:.0f}s)"
            media_s = f" 图{c.active_media_count}({format_media_size(c.active_media_bytes)})" if c.active_media_count > 0 else ""
            last_st = c.recent_steps[-1] if c.recent_steps else None
            c_hit_s = _format_hit_with_cold_badge(
                c.latest_hit_rate,
                last_st.classification if last_st is not None else "",
                c.latest_cache_reason or (last_st.reason if last_st is not None else ""),
                is_compact=bool(last_st.is_compact) if last_st is not None else False,
            )
            title_s = f" {truncate_display_width(c.title, 20)}" if c.title else ""
            lines.append(
                f"  - {tag} {c.short_id}{title_s} {c.model} {format_tokens(c.context_tokens)}{delta_s}{media_s} [{bar}] {c_hit_s}{tf}{sp}"
            )
    if snap.target_conversation_query:
        tc = snap.target_conversation
        if tc is not None:
            fresh_s = tc.freshness_invalid_reason or format_countdown(tc.freshness_remaining_sec)
            tc_last = tc.recent_steps[-1] if tc.recent_steps else None
            tc_hit_s = _format_hit_with_cold_badge(
                tc.latest_hit_rate,
                tc_last.classification if tc_last is not None else "",
                tc.latest_cache_reason or (tc_last.reason if tc_last is not None else ""),
                is_compact=bool(tc_last.is_compact) if tc_last is not None else False,
            )
            tc_media_s = f" | 多模态 图{tc.active_media_count}({format_media_size(tc.active_media_bytes)})" if tc.active_media_count > 0 else ""
            lines.append(
                f"定向对话 ({tc.short_id}): {tc.model} | 上下文 {format_tokens(tc.context_tokens)} ({format_delta_tokens(tc.latest_context_delta)}){tc_media_s} "
                f"| 缓存 {tc_hit_s} | 保鲜 {fresh_s}"
            )
        else:
            lines.append(f"定向对话 ({snap.target_conversation_query}): 未找到匹配记录")
    if snap.wasted_cold_tokens_total > 0 and (snap.max_consec_miss >= 1 or snap.max_abnormal_recent10 >= 2):
        culprit_s = getattr(snap, "cache_culprit_summary", "")
        if snap.is_historical_rating or snap.history_range != "30m":
            who = f" {culprit_s} " if culprit_s else ""
            lines.append(
                f"冷算提醒: {snap.history_range_label}内{who}异常掉缓存累计多耗约 {format_tokens(snap.wasted_cold_tokens_total)} Token"
            )
        else:
            lines.append(f"冷算提醒: 近 10 步异常掉缓存累计多耗约 {format_tokens(snap.wasted_cold_tokens_total)} Token")
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
