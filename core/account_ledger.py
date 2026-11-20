"""Privacy-safe local account switch timeline for per-account daily step tracking.

Stores only masked short labels (e.g. ``de***o``) and timestamps in a local
ledger JSON file — never raw email addresses or tokens. Optionally augments
today's historical switches from read-only Cockpit logs when present.
"""

from __future__ import annotations

import bisect
import datetime
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
        self._cockpit_cache: Dict[str, Tuple[Tuple[int, int], List[Tuple[float, str]]]] = {}
        self._load_own()

    def _load_own(self) -> None:
        if self.ledger_path is None or not self.ledger_path.is_file():
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        events: List[Tuple[float, str]] = []
        for item in data.get("events") or []:
            if not isinstance(item, dict):
                continue
            try:
                ts = float(item["ts"])
                lbl = _mask_short(str(item.get("label", "")))
            except (KeyError, TypeError, ValueError):
                continue
            if ts > 0 and lbl and not _is_invalid_label(lbl):
                events.append((ts, lbl))
        events.sort(key=lambda x: x[0])
        self._own_events = events[-120:]

    def _save_own(self) -> None:
        if self.read_only or self.ledger_path is None:
            return
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": 1, "events": [{"ts": round(ts, 3), "label": lbl} for ts, lbl in self._own_events[-120:]]},
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            tmp = self.ledger_path.with_name(f"{self.ledger_path.name}.tmp.{os.getpid()}")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.ledger_path)
        except OSError:
            pass

    def observe_online(self, short_label: str, now_ts: float) -> None:
        """Record an observed online masked account if it differs from the most recent known state."""
        if _is_invalid_label(short_label):
            return
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return
        # Prune entries older than 7 days
        cutoff = now_ts - 7 * 86400
        self._own_events = [(ts, lab) for ts, lab in self._own_events if ts >= cutoff]
        if self._own_events and self._own_events[-1][1].lower() == lbl.lower():
            return
        # If Cockpit already recorded a switch to this exact account within the last 120s, avoid duplicating
        cockpit_evs = self._read_cockpit_switches(now_ts - 2 * 86400)
        if cockpit_evs:
            last_ck_ts, last_ck_lbl = cockpit_evs[-1]
            if last_ck_lbl.lower() == lbl.lower() and abs(now_ts - last_ck_ts) <= 180.0:
                if not self._own_events or self._own_events[-1][1].lower() != lbl.lower():
                    self._own_events.append((last_ck_ts, lbl))
                    self._own_events.sort(key=lambda x: x[0])
                    self._save_own()
                return
        self._own_events.append((now_ts, lbl))
        self._own_events.sort(key=lambda x: x[0])
        self._save_own()

    def _read_cockpit_switches(self, since_ts: float) -> List[Tuple[float, str]]:
        if self.cockpit_logs_dir is None or not self.cockpit_logs_dir.is_dir():
            return []
        try:
            candidates = sorted(
                self.cockpit_logs_dir.glob("app.log.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:3]
        except OSError:
            return []

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
        return merged

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

    def attribute_timestamp(
        self,
        step_ts: float,
        timeline: List[Tuple[float, str]],
        fallback_label: str = "",
    ) -> str:
        """Determine which masked account owned ``step_ts`` based on the switch timeline."""
        if not timeline:
            return _mask_short(fallback_label)
        times = [t for t, _ in timeline]
        # Allow a tiny 2s grace margin for clock jitter between switch log and transcript write
        idx = bisect.bisect_right(times, step_ts + 2.0) - 1
        if idx >= 0:
            return timeline[idx][1]
        # Before the very first recorded entry: if only one account appears in the timeline, attribute to it
        unique_accounts = {lbl.lower() for _, lbl in timeline}
        if len(unique_accounts) == 1:
            return timeline[0][1]
        return ""
