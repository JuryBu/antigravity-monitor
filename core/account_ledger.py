"""Privacy-safe local account switch timeline for per-account daily step tracking.

Stores only masked short labels (e.g. ``de***o``) and timestamps in a local
ledger JSON file — never raw email addresses or tokens. Optionally augments
today's historical switches from read-only Cockpit logs when present.
"""

from __future__ import annotations

import bisect
import datetime
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

COCKPIT_SWITCH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+INFO\s+\[Antigravity[^\]]*\]\s+写入系统凭据:\s*(\S+)",
)


def _parse_ts(raw: str) -> Optional[float]:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Truncate sub-microsecond nanoseconds (e.g. .721645200 -> .721645) for Python fromisoformat
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{6})\d*([+-]\d{2}:?\d{2}|Z)?$", text)
    if m:
        tz = m.group(3) or ""
        text = f"{m.group(1)}.{m.group(2)}{tz}"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return dt.timestamp()


_INVALID_ACCOUNT_LABELS = frozenset({"", "离线", "离***", "offline", "unknown"})


def _is_invalid_label(raw: str) -> bool:
    clean = (raw or "").strip().lower()
    return clean in _INVALID_ACCOUNT_LABELS or clean.startswith("离*")


def _mask_short(raw: str) -> str:
    clean = raw.strip()
    if _is_invalid_label(clean):
        return ""
    if "@" in clean:
        clean = clean.split("@", 1)[0]
    if _is_invalid_label(clean):
        return ""
    if "***" in clean:
        return clean
    if len(clean) == 1:
        return "*"
    if len(clean) == 2:
        return f"{clean[0]}***"
    if len(clean) == 3:
        return f"{clean[:2]}***"
    return f"{clean[:2]}***{clean[-1]}"


class AccountSwitchLedger:
    """Tracks masked account switches to partition daily steps and suppress switch false-positives."""

    def __init__(
        self,
        ledger_path: Optional[Path | str] = None,
        cockpit_logs_dir: Optional[Path | str] = None,
        read_only: bool = False,
    ) -> None:
        self.ledger_path: Optional[Path] = Path(ledger_path) if ledger_path is not None else None
        self.cockpit_logs_dir: Optional[Path] = Path(cockpit_logs_dir) if cockpit_logs_dir is not None else None
        self.read_only: bool = bool(read_only)
        self._own_events: List[Tuple[float, str]] = []
        self._daily_step_profiles: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._rule_change_timestamps: List[float] = []
        self._cockpit_cache: Dict[str, Tuple[Tuple[int, int], List[Tuple[float, str]]]] = {}
        self._load_own()

    @staticmethod
    def _parse_nonneg_int(raw: Any, default: int = 0) -> Optional[int]:
        if raw is None or isinstance(raw, bool):
            return None if raw is None else default
        try:
            val = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0, val)

    def _load_own(self) -> None:
        if self.ledger_path is None or not self.ledger_path.is_file():
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        events: List[Tuple[float, str]] = []
        raw_events = data.get("events")
        if isinstance(raw_events, list):
            for item in raw_events:
                if not isinstance(item, dict):
                    continue
                try:
                    raw_ts = item["ts"]
                    if isinstance(raw_ts, bool):
                        continue
                    ts = float(raw_ts)
                    lbl = _mask_short(str(item.get("label", "")))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if ts > 0 and lbl and not _is_invalid_label(lbl):
                    events.append((ts, lbl))
        events.sort(key=lambda x: x[0])
        self._own_events = events[-120:]

        profiles: Dict[str, Dict[str, Dict[str, Any]]] = {}
        canon_by_low: Dict[str, str] = {}
        raw_profiles = data.get("daily_step_profiles")
        if isinstance(raw_profiles, dict):
            for raw_acct, days_map in raw_profiles.items():
                lbl = _mask_short(str(raw_acct))
                if not lbl or _is_invalid_label(lbl) or not isinstance(days_map, dict):
                    continue
                low = lbl.lower()
                canon_lbl = canon_by_low.setdefault(low, lbl)
                target_days = profiles.setdefault(canon_lbl, {})
                for day_k, rec in days_map.items():
                    if not isinstance(day_k, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day_k):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    raw_safe = rec.get("safe_steps", 0)
                    raw_max = rec.get("max_steps", raw_safe)
                    raw_deg = rec.get("degraded_at")
                    if isinstance(raw_safe, bool) or isinstance(raw_max, bool) or isinstance(raw_deg, bool):
                        continue
                    safe_s = self._parse_nonneg_int(raw_safe, 0)
                    max_s = self._parse_nonneg_int(raw_max, 0)
                    if safe_s is None or max_s is None:
                        continue
                    max_s = max(safe_s, max_s)
                    deg_at: Optional[int] = None
                    if raw_deg is not None:
                        parsed_deg = self._parse_nonneg_int(raw_deg, 0)
                        if parsed_deg is None or parsed_deg <= 0:
                            continue
                        deg_at = parsed_deg
                        safe_s = min(safe_s, max(0, deg_at - 1))
                    prev = target_days.get(day_k)
                    if prev is None:
                        target_days[day_k] = {"safe_steps": safe_s, "max_steps": max_s, "degraded_at": deg_at}
                    else:
                        merged_deg = (
                            min(prev["degraded_at"], deg_at)
                            if (prev["degraded_at"] is not None and deg_at is not None)
                            else (prev["degraded_at"] if prev["degraded_at"] is not None else deg_at)
                        )
                        merged_safe = max(prev["safe_steps"], safe_s)
                        if merged_deg is not None:
                            merged_safe = min(merged_safe, max(0, merged_deg - 1))
                        target_days[day_k] = {
                            "safe_steps": merged_safe,
                            "max_steps": max(prev["max_steps"], max_s, merged_safe),
                            "degraded_at": merged_deg,
                        }
        # Trim each account to latest 30 dates and keep top 32 most recently active accounts
        trimmed: Dict[str, Dict[str, Dict[str, Any]]] = {}
        active_accts = [
            (max(dmap.keys()), lbl)
            for lbl, dmap in profiles.items()
            if dmap
        ]
        active_accts.sort(key=lambda x: x[0])
        for _last_day, lbl in active_accts[-32:]:
            dmap = profiles[lbl]
            trimmed[lbl] = {dk: dmap[dk] for dk in sorted(dmap.keys())[-30:]}
        self._daily_step_profiles = trimmed

        parsed_rules: List[float] = []
        raw_rule_ts = data.get("rule_change_timestamps")
        if isinstance(raw_rule_ts, list):
            for x in raw_rule_ts:
                if not isinstance(x, (int, float)) or isinstance(x, bool):
                    continue
                try:
                    val = float(x)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(val) and 946684800.0 <= val <= 4102444800.0:
                    parsed_rules.append(val)
        parsed_rules.sort()
        dedup_rules: List[float] = []
        for val in parsed_rules:
            if dedup_rules and (val - dedup_rules[-1]) <= 1.0:
                continue
            dedup_rules.append(val)
        self._rule_change_timestamps = dedup_rules[-64:]

    def _save_own(self) -> None:
        if self.read_only or self.ledger_path is None:
            return
        tmp: Optional[Path] = None
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            out_obj: Dict[str, Any] = {
                "version": 1,
                "events": [{"ts": round(ts, 3), "label": lbl} for ts, lbl in self._own_events[-120:]],
            }
            if self._daily_step_profiles:
                active_accts = [
                    (max(dmap.keys()), lbl)
                    for lbl, dmap in self._daily_step_profiles.items()
                    if dmap
                ]
                active_accts.sort(key=lambda x: x[0])
                trimmed_profiles: Dict[str, Dict[str, Dict[str, Any]]] = {}
                for _last_day, lbl in active_accts[-32:]:
                    dmap = self._daily_step_profiles[lbl]
                    keep_days = sorted(dmap.keys())[-30:]
                    trimmed_profiles[lbl] = {dk: dmap[dk] for dk in keep_days}
                out_obj["daily_step_profiles"] = trimmed_profiles
            if self._rule_change_timestamps:
                out_obj["rule_change_timestamps"] = [round(ts, 3) for ts in self._rule_change_timestamps[-64:]]
            payload = json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n"
            tmp = self.ledger_path.with_name(f"{self.ledger_path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.ledger_path)
            tmp = None
        except OSError:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def record_rule_change(self, mtime_ts: float, now_ts: Optional[float] = None) -> bool:
        """Record a global rule-file modification timestamp (updates memory even when read_only)."""
        if isinstance(mtime_ts, bool):
            return False
        try:
            val = float(mtime_ts)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(val):
            return False
        if now_ts is None:
            now = time.time()
        else:
            if isinstance(now_ts, bool):
                return False
            try:
                now = float(now_ts)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(now):
                return False
        cutoff = now - 14.0 * 86400.0
        if val <= 0.0 or val < cutoff or val > (now + 300.0) or val < 946684800.0 or val > 4102444800.0:
            return False
        if any(abs(t - val) <= 1.0 for t in self._rule_change_timestamps):
            return False
        updated = [t for t in self._rule_change_timestamps if t >= cutoff]
        updated.append(val)
        updated.sort()
        updated = updated[-64:]
        if val not in updated:
            return False
        self._rule_change_timestamps = updated
        if not self.read_only:
            self._save_own()
        return True

    def get_rule_change_timestamps(self, since_ts: float = 0.0) -> List[float]:
        """Return a sorted copy of recorded rule-change timestamps at or after ``since_ts``."""
        return sorted(t for t in self._rule_change_timestamps if t >= since_ts)

    def get_account_daily_profiles(self, short_label: str) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of stored per-day step stats for a masked account label."""
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return {}
        target_low = lbl.lower()
        for k, dmap in self._daily_step_profiles.items():
            if k.lower() == target_low:
                return {dk: dict(dv) for dk, dv in dmap.items()}
        return {}

    def record_daily_step_profile(
        self,
        short_label: str,
        date_str: str,
        today_steps: int,
        is_healthy: bool,
        is_degraded: bool,
    ) -> None:
        """Throttled privacy-safe persistence of per-account daily safe step capacity & degradation points."""
        if self.read_only or today_steps <= 0:
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
            return
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return
        # Reuse existing canonical casing if already present
        canon_key = next((k for k in self._daily_step_profiles if k.lower() == lbl.lower()), lbl)
        dmap = self._daily_step_profiles.setdefault(canon_key, {})
        prev = dmap.get(date_str)
        prev_safe = int(prev.get("safe_steps", 0)) if prev else 0
        prev_max = int(prev.get("max_steps", 0)) if prev else 0
        prev_deg = prev.get("degraded_at") if prev else None

        new_max = max(prev_max, int(today_steps))
        new_deg = prev_deg
        if is_degraded:
            new_deg = min(prev_deg, int(today_steps)) if prev_deg is not None else int(today_steps)
        # Only advance safe_steps while healthy AND strictly before any degradation on this day
        new_safe = prev_safe
        if new_deg is not None:
            new_safe = min(new_safe, max(0, new_deg - 1))
        elif is_healthy:
            new_safe = max(prev_safe, int(today_steps))

        dirty = False
        if prev is None:
            if new_safe >= 50 or new_deg is not None or new_max >= 50:
                dirty = True
        else:
            if new_deg != prev_deg:
                dirty = True
            elif (new_safe - prev_safe) >= 25 or (new_max - prev_max) >= 50:
                dirty = True
        if not dirty:
            return

        dmap[date_str] = {
            "safe_steps": new_safe,
            "max_steps": new_max,
            "degraded_at": new_deg,
        }
        if len(dmap) > 30:
            for old_k in sorted(dmap.keys())[:-30]:
                dmap.pop(old_k, None)
        # Evict least recently active accounts (never evicting active main accounts by FIFO insertion order)
        while len(self._daily_step_profiles) > 32:
            evict_candidates = [k for k in self._daily_step_profiles if k != canon_key]
            if not evict_candidates:
                break
            oldest_acct = min(
                evict_candidates,
                key=lambda k: max(self._daily_step_profiles[k].keys()) if self._daily_step_profiles[k] else "",
            )
            self._daily_step_profiles.pop(oldest_acct, None)
        self._save_own()

    def observe_online(
        self,
        short_label: str,
        now_ts: float,
        hint_switch_ts: Optional[float] = None,
    ) -> None:
        """Record an observed online masked account if it differs from the most recent known state."""
        if _is_invalid_label(short_label):
            return
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return
        # Prune entries older than 10 days (covers 7d history range + 48h lookback anchor)
        cutoff = now_ts - 10 * 86400
        self._own_events = [(ts, lab) for ts, lab in self._own_events if ts >= cutoff]
        if self._own_events and self._own_events[-1][1].lower() == lbl.lower():
            return
        # If Cockpit already recorded a switch to this exact account within the last 180s
        # AND strictly after our latest own event (never inserting backwards before a newer manual switch), anchor to it.
        prev_own_ts = self._own_events[-1][0] if self._own_events else 0.0
        cockpit_evs = self._read_cockpit_switches(now_ts - 2 * 86400)
        if cockpit_evs:
            last_ck_ts, last_ck_lbl = cockpit_evs[-1]
            if (
                last_ck_lbl.lower() == lbl.lower()
                and abs(now_ts - last_ck_ts) <= 180.0
                and last_ck_ts > prev_own_ts
            ):
                self._own_events.append((last_ck_ts, lbl))
                self._own_events.sort(key=lambda x: x[0])
                self._save_own()
                return
        # Without Cockpit switch log, honor an engine-validated cold-start hint timestamp (never crossing prior events)
        rec_ts = now_ts
        if hint_switch_ts is not None and (now_ts - 60.0) <= hint_switch_ts <= now_ts:
            prev_ts = max(prev_own_ts, cockpit_evs[-1][0] if cockpit_evs else 0.0)
            if hint_switch_ts > prev_ts:
                rec_ts = hint_switch_ts
        self._own_events.append((rec_ts, lbl))
        self._own_events.sort(key=lambda x: x[0])
        self._save_own()

    def _read_cockpit_switches(self, since_ts: float) -> List[Tuple[float, str]]:
        if self.cockpit_logs_dir is None or not self.cockpit_logs_dir.is_dir():
            return []
        try:
            raw_files = list(self.cockpit_logs_dir.glob("app.log.*"))
        except OSError:
            return []
        dated: List[Tuple[float, Path]] = []
        for p in raw_files:
            try:
                dated.append((p.stat().st_mtime, p))
            except OSError:
                continue
        dated.sort(key=lambda item: item[0], reverse=True)
        candidates = [p for _, p in dated[:12]]
        valid_keys = {str(p) for p in candidates}
        for stale_key in [k for k in self._cockpit_cache if k not in valid_keys]:
            self._cockpit_cache.pop(stale_key, None)

        merged: List[Tuple[float, str]] = []
        for lp in candidates:
            try:
                st = lp.stat()
            except OSError:
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            key = str(lp)
            cached = self._cockpit_cache.get(key)
            if cached is not None and cached[0] == stamp:
                rows = cached[1]
            else:
                rows = []
                try:
                    with lp.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if "写入系统凭据:" not in line:
                                continue
                            m = COCKPIT_SWITCH_RE.match(line)
                            if not m:
                                continue
                            ts = _parse_ts(m.group(1))
                            lbl = _mask_short(m.group(2))
                            if ts is not None and lbl:
                                rows.append((ts, lbl))
                except OSError:
                    rows = []
                self._cockpit_cache[key] = (stamp, rows)
            for ts, lbl in rows:
                if ts >= since_ts:
                    merged.append((ts, lbl))
        merged.sort(key=lambda x: x[0])
        deduped: List[Tuple[float, str]] = []
        seen: set[Tuple[float, str]] = set()
        for ts, lbl in merged:
            sig = (round(ts, 3), lbl.lower())
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append((ts, lbl))
        return deduped

    def get_timeline(self, since_ts: float) -> List[Tuple[float, str]]:
        """Return deduplicated, chronological (timestamp, masked_label) switch transitions."""
        lookback = since_ts - 48 * 3600
        raw = [ev for ev in self._own_events if ev[0] >= lookback]
        raw.extend(self._read_cockpit_switches(lookback))
        raw.sort(key=lambda x: x[0])

        dedup: List[Tuple[float, str]] = []
        for ts, lbl in raw:
            if dedup and dedup[-1][1].lower() == lbl.lower():
                continue
            dedup.append((ts, lbl))
        return dedup

    def get_switch_timestamps(self, since_ts: float) -> List[float]:
        """Return timestamps where the active account actually changed from a different prior account."""
        tl = self.get_timeline(since_ts)
        if len(tl) < 2:
            return []
        return [ts for ts, _ in tl[1:] if ts >= since_ts]

    def list_known_accounts(
        self,
        since_ts: float,
        current_label: str = "",
    ) -> List[str]:
        """Return known masked account labels ordered by most-recent appearance (current online first)."""
        tl = self.get_timeline(since_ts)
        ordered: List[str] = []
        seen_low: set[str] = set()
        cur_clean = _mask_short(current_label) if current_label else ""
        if cur_clean:
            ordered.append(cur_clean)
            seen_low.add(cur_clean.lower())
        for _ts, lbl in reversed(tl):
            low = lbl.lower()
            if low and low not in seen_low:
                ordered.append(lbl)
                seen_low.add(low)
        return ordered

    def attribute_timestamp(
        self,
        step_ts: float,
        timeline: List[Tuple[float, str]],
        fallback_label: str = "",
        _times_cache: Optional[List[float]] = None,
    ) -> str:
        """Determine which masked account owned ``step_ts`` based on the switch timeline."""
        if not timeline:
            return _mask_short(fallback_label)
        times = (
            _times_cache
            if (_times_cache is not None and len(_times_cache) == len(timeline))
            else [t for t, _ in timeline]
        )
        # Allow a tiny 2s grace margin for clock jitter between switch log and transcript write
        idx = bisect.bisect_right(times, step_ts + 2.0) - 1
        if idx >= 0:
            return timeline[idx][1]
        # Before the very first recorded entry: if only one account appears in the timeline, attribute to it
        first_low = timeline[0][1].lower()
        for _, lbl in timeline[1:]:
            if lbl.lower() != first_low:
                return ""
        return timeline[0][1]

    def build_account_matcher(
        self,
        target_account: str,
        timeline: List[Tuple[float, str]],
        fallback_label: str = "",
    ) -> Callable[[float], bool]:
        """Build a zero-allocation O(log N) predicate checking whether ``step_ts`` belongs to ``target_account``."""
        target_low = (_mask_short(target_account) or target_account).strip().lower()
        if not target_low:
            return lambda _ts: True
        if not timeline:
            fb_low = (_mask_short(fallback_label) or fallback_label).strip().lower()
            matched = fb_low == target_low
            return (lambda _ts: True) if matched else (lambda _ts: False)
        times = [t for t, _ in timeline]
        labels_low = [lbl.lower() for _, lbl in timeline]
        first_low = labels_low[0]
        single_before_match = all(l == first_low for l in labels_low[1:]) and (first_low == target_low)
        bisect_right = bisect.bisect_right

        def _matches(ev_ts: float) -> bool:
            idx = bisect_right(times, ev_ts + 2.0) - 1
            if idx >= 0:
                return labels_low[idx] == target_low
            return single_before_match

        return _matches
