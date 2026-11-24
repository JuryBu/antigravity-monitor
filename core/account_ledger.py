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

VALID_DEGRADED_KINDS = frozenset({"t1_single", "t1_multi", "t2", "t3"})
_MAJOR_DEGRADED_KINDS = frozenset({"t1_multi", "t2", "t3"})
_DEGRADED_KIND_SEVERITY: Dict[str, int] = {
    "": 0,
    "t1_single": 1,
    "t1_multi": 2,
    "t2": 3,
    "t3": 4,
}


def normalize_account_tier(raw_tier: str) -> str:
    """Normalize Hub ``userTier.id`` (never ``plan_status``) into ``ultra | pro | free | ''``."""
    if not isinstance(raw_tier, str):
        return ""
    low = raw_tier.strip().lower()
    if not low:
        return ""
    if "ultra" in low:
        return "ultra"
    if "pro" in low:
        return "pro"
    if "free" in low:
        return "free"
    return ""


def _tier_deg_min(norm_tier: str) -> int:
    if norm_tier == "pro":
        return 50
    if norm_tier == "free":
        return 30
    return 400


def _normalize_degraded_kind(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    low = raw.strip().lower()
    return low if low in VALID_DEGRADED_KINDS else ""


def _merge_degraded_kind(a: str, b: str, is_major_degraded: bool) -> str:
    ka = _normalize_degraded_kind(a)
    kb = _normalize_degraded_kind(b)
    if is_major_degraded:
        # Once an hour is marked as a real account-wide degradation (d=True), never let a stale
        # t1_single label mask it as a benign single-conversation drift.
        if ka == "t1_single":
            ka = ""
        if kb == "t1_single":
            kb = ""
    return ka if _DEGRADED_KIND_SEVERITY.get(ka, 0) >= _DEGRADED_KIND_SEVERITY.get(kb, 0) else kb


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
        self._hourly_step_buckets: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._account_tiers: Dict[str, Dict[str, Any]] = {}
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

    @staticmethod
    def _parse_strict_int(raw: Any) -> Optional[int]:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            s = raw.strip()
            if s and s.isascii() and s.isdigit():
                return int(s)
        return None

    @staticmethod
    def _parse_optional_bucket_int(val: Dict[str, Any], short_k: str, long_k: str, max_v: int = 500_000) -> int:
        raw = val.get(short_k)
        if raw is None:
            raw = val.get(long_k)
        if raw is None or isinstance(raw, bool):
            return 0
        parsed = AccountSwitchLedger._parse_strict_int(raw)
        if parsed is None or parsed < 0:
            return 0
        return min(max_v, parsed)

    @staticmethod
    def _pick_preferred_deg_at(
        a: Optional[int],
        b: Optional[int],
        deg_min: int = 400,
    ) -> Optional[int]:
        if a is None:
            return b
        if b is None:
            return a
        th = max(1, int(deg_min))
        a_high = a >= th
        b_high = b >= th
        if a_high and b_high:
            return min(a, b)
        if a_high:
            return a
        if b_high:
            return b
        return min(a, b)

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

        account_tiers: Dict[str, Dict[str, Any]] = {}
        t_canon_by_low: Dict[str, str] = {}
        raw_tiers = data.get("account_tiers")
        if isinstance(raw_tiers, dict):
            for raw_acct, rec in raw_tiers.items():
                lbl = _mask_short(str(raw_acct))
                if not lbl or _is_invalid_label(lbl) or not isinstance(rec, dict):
                    continue
                raw_tid = rec.get("tier")
                if not isinstance(raw_tid, str):
                    continue
                clean_tid = raw_tid.strip()[:64]
                if not clean_tid or not normalize_account_tier(clean_tid):
                    continue
                raw_upd = rec.get("updated_at", 0)
                if isinstance(raw_upd, bool):
                    continue
                try:
                    upd_ts = float(raw_upd)
                except (TypeError, ValueError, OverflowError):
                    upd_ts = 0.0
                if not math.isfinite(upd_ts) or upd_ts < 0.0:
                    upd_ts = 0.0
                low = lbl.lower()
                canon_lbl = t_canon_by_low.setdefault(low, lbl)
                prev_t = account_tiers.get(canon_lbl)
                if prev_t is None or upd_ts >= float(prev_t.get("updated_at", 0.0)):
                    account_tiers[canon_lbl] = {"tier": clean_tid, "updated_at": int(round(upd_ts))}
        if len(account_tiers) > 32:
            sorted_tiers = sorted(
                account_tiers.items(),
                key=lambda kv: float(kv[1].get("updated_at", 0.0)),
            )
            account_tiers = dict(sorted_tiers[-32:])
        self._account_tiers = account_tiers

        hourly_buckets: Dict[str, Dict[int, Dict[str, Any]]] = {}
        h_canon_by_low: Dict[str, str] = {}
        raw_hourly = data.get("hourly_step_buckets")
        if isinstance(raw_hourly, dict):
            for raw_acct, hmap_raw in raw_hourly.items():
                lbl = _mask_short(str(raw_acct))
                if not lbl or _is_invalid_label(lbl) or not isinstance(hmap_raw, dict):
                    continue
                low = lbl.lower()
                canon_lbl = h_canon_by_low.setdefault(low, lbl)
                acct_deg_min = _tier_deg_min(self.get_account_tier(canon_lbl))
                target_hours = hourly_buckets.setdefault(canon_lbl, {})
                for hk_raw, val in hmap_raw.items():
                    hk = self._parse_strict_int(hk_raw)
                    if hk is None or not (262968 <= hk <= 1139568):
                        continue
                    steps: Optional[int] = None
                    degraded = False
                    deg_at: Optional[int] = None
                    dk = ""
                    m_cnt = 0
                    sub_cnt = 0
                    lt_cnt = 0
                    p3_cnt = 0
                    uc_k = 0
                    if isinstance(val, int) and not isinstance(val, bool):
                        if not (0 <= val <= 500_000):
                            continue
                        steps = val
                    elif isinstance(val, dict):
                        if "steps" in val:
                            raw_s = val["steps"]
                        elif "s" in val:
                            raw_s = val["s"]
                        else:
                            raw_s = 0
                        steps = self._parse_strict_int(raw_s)
                        if steps is None or not (0 <= steps <= 500_000):
                            continue
                        if "degraded" in val:
                            raw_d = val["degraded"]
                        elif "d" in val:
                            raw_d = val["d"]
                        else:
                            raw_d = False
                        degraded = isinstance(raw_d, bool) and raw_d
                        # Parse dk unconditionally (NOT gated by `if degraded:`) so t1_single (d=False, dk="t1_single") loads cleanly
                        raw_dk = val.get("dk") if "dk" in val else val.get("degraded_kind")
                        dk = _normalize_degraded_kind(raw_dk)
                        if degraded:
                            if "degraded_24h_at" in val:
                                raw_at = val["degraded_24h_at"]
                            elif "at" in val:
                                raw_at = val["at"]
                            else:
                                raw_at = None
                            if raw_at is not None:
                                parsed_at = self._parse_strict_int(raw_at)
                                if parsed_at is not None and 1 <= parsed_at <= 500_000:
                                    deg_at = parsed_at
                        m_cnt = self._parse_optional_bucket_int(val, "m", "main_steps", 500_000)
                        sub_cnt = self._parse_optional_bucket_int(val, "sub", "sub_steps", 500_000)
                        lt_cnt = self._parse_optional_bucket_int(val, "lt", "light_steps", 500_000)
                        p3_cnt = self._parse_optional_bucket_int(val, "p3", "third_party_steps", 500_000)
                        uc_k = self._parse_optional_bucket_int(val, "uc", "uncached_k", 500_000_000)
                    else:
                        continue
                    if steps == 0 and not degraded and not dk:
                        continue
                    prev = target_hours.get(hk)
                    if prev is None:
                        target_hours[hk] = {
                            "steps": steps,
                            "degraded": degraded,
                            "degraded_24h_at": deg_at,
                            "dk": dk,
                            "m": m_cnt,
                            "sub": sub_cnt,
                            "lt": lt_cnt,
                            "p3": p3_cnt,
                            "uc": uc_k,
                        }
                    else:
                        merged_deg = bool(prev["degraded"]) or degraded
                        merged_at = (
                            self._pick_preferred_deg_at(
                                prev.get("degraded_24h_at"),
                                deg_at,
                                deg_min=acct_deg_min,
                            )
                            if merged_deg
                            else None
                        )
                        target_hours[hk] = {
                            "steps": max(int(prev["steps"]), steps),
                            "degraded": merged_deg,
                            "degraded_24h_at": merged_at,
                            "dk": _merge_degraded_kind(str(prev.get("dk", "")), dk, merged_deg),
                            "m": max(int(prev.get("m", 0)), m_cnt),
                            "sub": max(int(prev.get("sub", 0)), sub_cnt),
                            "lt": max(int(prev.get("lt", 0)), lt_cnt),
                            "p3": max(int(prev.get("p3", 0)), p3_cnt),
                            "uc": max(int(prev.get("uc", 0)), uc_k),
                        }
        trimmed_hourly: Dict[str, Dict[int, Dict[str, Any]]] = {}
        active_hourly_accts = [
            (max(hmap.keys()), lbl)
            for lbl, hmap in hourly_buckets.items()
            if hmap
        ]
        active_hourly_accts.sort(key=lambda x: x[0])
        for _last_h, lbl in active_hourly_accts[-32:]:
            hmap = hourly_buckets[lbl]
            trimmed_hourly[lbl] = {hk: hmap[hk] for hk in sorted(hmap.keys())[-336:]}
        self._hourly_step_buckets = trimmed_hourly

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
            if self._account_tiers:
                sorted_tiers = sorted(
                    self._account_tiers.items(),
                    key=lambda kv: float(kv[1].get("updated_at", 0.0)),
                )
                out_obj["account_tiers"] = {
                    lbl: {
                        "tier": str(rec["tier"]),
                        "updated_at": int(round(float(rec.get("updated_at", 0.0)))),
                    }
                    for lbl, rec in sorted_tiers[-32:]
                    if rec.get("tier")
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
            if self._hourly_step_buckets:
                active_hourly = [
                    (max(hmap.keys()), lbl)
                    for lbl, hmap in self._hourly_step_buckets.items()
                    if hmap
                ]
                active_hourly.sort(key=lambda x: x[0])
                trimmed_hourly_out: Dict[str, Dict[str, Any]] = {}
                for _last_h, lbl in active_hourly[-32:]:
                    hmap = self._hourly_step_buckets[lbl]
                    acct_out: Dict[str, Any] = {}
                    for hk in sorted(hmap.keys())[-336:]:
                        rec = hmap[hk]
                        steps = max(0, min(500_000, int(rec.get("steps", 0))))
                        degraded = bool(rec.get("degraded", False))
                        dk = _normalize_degraded_kind(rec.get("dk", ""))
                        m_cnt = max(0, min(500_000, int(rec.get("m", 0) or 0)))
                        sub_cnt = max(0, min(500_000, int(rec.get("sub", 0) or 0)))
                        lt_cnt = max(0, min(500_000, int(rec.get("lt", 0) or 0)))
                        p3_cnt = max(0, min(500_000, int(rec.get("p3", 0) or 0)))
                        uc_k = max(0, min(500_000_000, int(rec.get("uc", 0) or 0)))
                        has_extra = bool(dk) or (m_cnt > 0) or (sub_cnt > 0) or (lt_cnt > 0) or (p3_cnt > 0) or (uc_k > 0)
                        if degraded or has_extra:
                            item: Dict[str, Any] = {"s": steps}
                            if degraded:
                                item["d"] = True
                                deg_at = rec.get("degraded_24h_at")
                                if deg_at is not None and not isinstance(deg_at, bool):
                                    deg_at_i = int(deg_at)
                                    if 1 <= deg_at_i <= 500_000:
                                        item["at"] = deg_at_i
                            if dk:
                                item["dk"] = dk
                            if m_cnt > 0:
                                item["m"] = m_cnt
                            if sub_cnt > 0:
                                item["sub"] = sub_cnt
                            if lt_cnt > 0:
                                item["lt"] = lt_cnt
                            if p3_cnt > 0:
                                item["p3"] = p3_cnt
                            if uc_k > 0:
                                item["uc"] = uc_k
                            acct_out[str(hk)] = item
                        elif steps > 0:
                            # Compact legacy format: bare int for pure step counts without degradation/metadata
                            acct_out[str(hk)] = steps
                    if acct_out:
                        trimmed_hourly_out[lbl] = acct_out
                if trimmed_hourly_out:
                    out_obj["hourly_step_buckets"] = trimmed_hourly_out
            if self._rule_change_timestamps:
                out_obj["rule_change_timestamps"] = [round(ts, 3) for ts in self._rule_change_timestamps[-64:]]
            payload = json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n"
            tmp = self.ledger_path.with_name(f"{self.ledger_path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(payload, encoding="utf-8")
            last_exc: Optional[OSError] = None
            for attempt in range(4):
                try:
                    os.replace(tmp, self.ledger_path)
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    if attempt < 3:
                        time.sleep(0.015)
                except OSError as exc:
                    last_exc = exc
                    if attempt < 3 and getattr(exc, "winerror", None) in (5, 32, 33):
                        time.sleep(0.015)
                    else:
                        break
            if last_exc is not None:
                raise last_exc
            tmp = None
        except OSError:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def observe_account_tier(self, short_label: str, tier_id: str, now_ts: float) -> bool:
        """Persist observed Hub ``userTier.id`` (throttled: write only on change or >=24h, strictly read_only-safe)."""
        if self.read_only:
            return False
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return False
        if not isinstance(tier_id, str):
            return False
        clean_tid = tier_id.strip()[:64]
        if not clean_tid or not normalize_account_tier(clean_tid):
            return False
        if isinstance(now_ts, bool):
            return False
        try:
            now_f = float(now_ts)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(now_f) or now_f <= 0.0:
            return False

        canon_key = next((k for k in self._account_tiers if k.lower() == lbl.lower()), lbl)
        prev = self._account_tiers.get(canon_key)
        if prev is not None:
            prev_tid = str(prev.get("tier", ""))
            prev_upd = float(prev.get("updated_at", 0.0))
            if prev_tid == clean_tid and (now_f - prev_upd) < 86400.0:
                return False

        self._account_tiers[canon_key] = {
            "tier": clean_tid,
            "updated_at": int(round(now_f)),
        }
        while len(self._account_tiers) > 32:
            evict_candidates = [k for k in self._account_tiers if k != canon_key]
            if not evict_candidates:
                break
            oldest_acct = min(
                evict_candidates,
                key=lambda k: float(self._account_tiers[k].get("updated_at", 0.0)),
            )
            self._account_tiers.pop(oldest_acct, None)
        self._save_own()
        return True

    def get_account_tier(self, short_label: str) -> str:
        """Return normalized account tier (``ultra | pro | free | ''``) for a masked label."""
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return ""
        target_low = lbl.lower()
        for k, rec in self._account_tiers.items():
            if k.lower() == target_low:
                return normalize_account_tier(str(rec.get("tier", "")))
        return ""

    def get_all_account_tiers(self) -> Dict[str, str]:
        """Return a copy of all known masked account labels mapped to normalized tiers."""
        out: Dict[str, str] = {}
        for k, rec in self._account_tiers.items():
            norm = normalize_account_tier(str(rec.get("tier", "")))
            if norm:
                out[k] = norm
        return out

    def get_all_hourly_step_buckets(self) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """Return a read-only copy of all accounts' hourly step buckets for same-tier peer prior aggregation."""
        out: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for acct, hmap in self._hourly_step_buckets.items():
            if hmap:
                out[acct] = {hk: dict(rec) for hk, rec in hmap.items()}
        return out

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

    def get_account_hourly_buckets(self, short_label: str) -> Dict[int, Dict[str, Any]]:
        """Return a shallow copy of stored hourly step buckets for a masked account label (case-insensitive)."""
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
            return {}
        target_low = lbl.lower()
        acct_deg_min = _tier_deg_min(self.get_account_tier(lbl))
        merged: Dict[int, Dict[str, Any]] = {}
        for k, hmap in self._hourly_step_buckets.items():
            if k.lower() != target_low:
                continue
            for hk, rec in hmap.items():
                prev = merged.get(hk)
                if prev is None:
                    merged[hk] = {
                        "steps": int(rec.get("steps", 0)),
                        "degraded": bool(rec.get("degraded", False)),
                        "degraded_24h_at": rec.get("degraded_24h_at"),
                        "dk": _normalize_degraded_kind(rec.get("dk", "")),
                        "m": int(rec.get("m", 0) or 0),
                        "sub": int(rec.get("sub", 0) or 0),
                        "lt": int(rec.get("lt", 0) or 0),
                        "p3": int(rec.get("p3", 0) or 0),
                        "uc": int(rec.get("uc", 0) or 0),
                    }
                else:
                    m_deg = bool(prev.get("degraded", False)) or bool(rec.get("degraded", False))
                    m_at = (
                        self._pick_preferred_deg_at(
                            prev.get("degraded_24h_at"),
                            rec.get("degraded_24h_at"),
                            deg_min=acct_deg_min,
                        )
                        if m_deg
                        else None
                    )
                    merged[hk] = {
                        "steps": max(int(prev.get("steps", 0)), int(rec.get("steps", 0))),
                        "degraded": m_deg,
                        "degraded_24h_at": m_at,
                        "dk": _merge_degraded_kind(str(prev.get("dk", "")), str(rec.get("dk", "")), m_deg),
                        "m": max(int(prev.get("m", 0) or 0), int(rec.get("m", 0) or 0)),
                        "sub": max(int(prev.get("sub", 0) or 0), int(rec.get("sub", 0) or 0)),
                        "lt": max(int(prev.get("lt", 0) or 0), int(rec.get("lt", 0) or 0)),
                        "p3": max(int(prev.get("p3", 0) or 0), int(rec.get("p3", 0) or 0)),
                        "uc": max(int(prev.get("uc", 0) or 0), int(rec.get("uc", 0) or 0)),
                    }
        return merged

    def record_hourly_step_buckets(
        self,
        short_label: str,
        hour_counts: Dict[int, int],
        degraded_hours: Optional[Dict[int, Optional[int]]] = None,
        now_ts: Optional[float] = None,
        hour_meta: Optional[Dict[int, Dict[str, Any]]] = None,
        degraded_kinds: Optional[Dict[int, str]] = None,
        deg_min: Optional[int] = None,
    ) -> bool:
        """Throttled privacy-safe persistence of per-account UTC hourly step, degradation & pool metadata buckets."""
        if self.read_only:
            return False
        lbl = _mask_short(short_label)
        if not lbl or _is_invalid_label(lbl):
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

        cur_h = int(now // 3600)
        min_valid_h = cur_h - 336
        max_valid_h = cur_h + 1
        eff_deg_min = max(1, int(deg_min)) if deg_min is not None else _tier_deg_min(self.get_account_tier(lbl))

        clean_counts: Dict[int, int] = {}
        if isinstance(hour_counts, dict):
            for hk_raw, cnt_raw in hour_counts.items():
                hk = self._parse_strict_int(hk_raw)
                if hk is None or not (262968 <= hk <= 1139568) or hk < min_valid_h or hk > max_valid_h:
                    continue
                cnt = self._parse_strict_int(cnt_raw)
                if cnt is None or not (0 <= cnt <= 500_000):
                    continue
                prev_c = clean_counts.get(hk, 0)
                if cnt > prev_c:
                    clean_counts[hk] = cnt

        clean_deg: Dict[int, Optional[int]] = {}
        if isinstance(degraded_hours, dict):
            for hk_raw, at_raw in degraded_hours.items():
                hk = self._parse_strict_int(hk_raw)
                if hk is None or not (262968 <= hk <= 1139568) or hk < min_valid_h or hk > max_valid_h:
                    continue
                parsed_at: Optional[int] = None
                if at_raw is not None:
                    at_val = self._parse_strict_int(at_raw)
                    if at_val is not None and 1 <= at_val <= 500_000:
                        parsed_at = at_val
                if hk in clean_deg:
                    clean_deg[hk] = self._pick_preferred_deg_at(
                        clean_deg[hk],
                        parsed_at,
                        deg_min=eff_deg_min,
                    )
                else:
                    clean_deg[hk] = parsed_at

        clean_kinds: Dict[int, str] = {}
        if isinstance(degraded_kinds, dict):
            for hk_raw, dk_raw in degraded_kinds.items():
                hk = self._parse_strict_int(hk_raw)
                if hk is None or not (262968 <= hk <= 1139568) or hk < min_valid_h or hk > max_valid_h:
                    continue
                dk_norm = _normalize_degraded_kind(dk_raw)
                if not dk_norm:
                    continue
                prev_dk = clean_kinds.get(hk, "")
                clean_kinds[hk] = _merge_degraded_kind(
                    prev_dk,
                    dk_norm,
                    is_major_degraded=(hk in clean_deg or dk_norm in _MAJOR_DEGRADED_KINDS),
                )

        clean_meta: Dict[int, Dict[str, int]] = {}
        if isinstance(hour_meta, dict):
            for hk_raw, meta_raw in hour_meta.items():
                hk = self._parse_strict_int(hk_raw)
                if hk is None or not (262968 <= hk <= 1139568) or hk < min_valid_h or hk > max_valid_h:
                    continue
                if not isinstance(meta_raw, dict):
                    continue
                m_v = self._parse_optional_bucket_int(meta_raw, "m", "main_steps", 500_000)
                sub_v = self._parse_optional_bucket_int(meta_raw, "sub", "sub_steps", 500_000)
                lt_v = self._parse_optional_bucket_int(meta_raw, "lt", "light_steps", 500_000)
                p3_v = self._parse_optional_bucket_int(meta_raw, "p3", "third_party_steps", 500_000)
                uc_v = self._parse_optional_bucket_int(meta_raw, "uc", "uncached_k", 500_000_000)
                if m_v or sub_v or lt_v or p3_v or uc_v:
                    prev_m = clean_meta.get(hk)
                    if prev_m is None:
                        clean_meta[hk] = {"m": m_v, "sub": sub_v, "lt": lt_v, "p3": p3_v, "uc": uc_v}
                    else:
                        clean_meta[hk] = {
                            "m": max(prev_m["m"], m_v),
                            "sub": max(prev_m["sub"], sub_v),
                            "lt": max(prev_m["lt"], lt_v),
                            "p3": max(prev_m["p3"], p3_v),
                            "uc": max(prev_m["uc"], uc_v),
                        }

        all_hks = set(clean_counts.keys()) | set(clean_deg.keys()) | set(clean_kinds.keys()) | set(clean_meta.keys())
        if not all_hks:
            return False

        canon_key = next((k for k in self._hourly_step_buckets if k.lower() == lbl.lower()), lbl)
        existing_hmap = self._hourly_step_buckets.get(canon_key, {})

        pending_updates: Dict[int, Dict[str, Any]] = {}
        dirty = False
        for hk in all_hks:
            cur_steps = clean_counts.get(hk, 0)
            cur_dk = clean_kinds.get(hk, "")
            is_deg = (hk in clean_deg) or (cur_dk in _MAJOR_DEGRADED_KINDS)
            cur_at = clean_deg.get(hk) if (hk in clean_deg) else None
            cmeta = clean_meta.get(hk)
            cur_m = cmeta["m"] if cmeta else 0
            cur_sub = cmeta["sub"] if cmeta else 0
            cur_lt = cmeta["lt"] if cmeta else 0
            cur_p3 = cmeta["p3"] if cmeta else 0
            cur_uc = cmeta["uc"] if cmeta else 0

            prev = existing_hmap.get(hk)
            if prev is None:
                new_steps = cur_steps
                new_deg = is_deg
                new_at = cur_at if new_deg else None
                new_dk = _merge_degraded_kind("", cur_dk, new_deg)
                if new_steps == 0 and not new_deg and not new_dk:
                    continue
                pending_updates[hk] = {
                    "steps": new_steps,
                    "degraded": new_deg,
                    "degraded_24h_at": new_at,
                    "dk": new_dk,
                    "m": cur_m,
                    "sub": cur_sub,
                    "lt": cur_lt,
                    "p3": cur_p3,
                    "uc": cur_uc,
                }
                dirty = True
            else:
                prev_steps = int(prev.get("steps", 0))
                prev_deg = bool(prev.get("degraded", False))
                prev_at = prev.get("degraded_24h_at") if prev_deg else None
                prev_dk = _normalize_degraded_kind(prev.get("dk", ""))
                prev_m = int(prev.get("m", 0) or 0)
                prev_sub = int(prev.get("sub", 0) or 0)
                prev_lt = int(prev.get("lt", 0) or 0)
                prev_p3 = int(prev.get("p3", 0) or 0)
                prev_uc = int(prev.get("uc", 0) or 0)

                new_steps = max(prev_steps, cur_steps)
                new_deg = prev_deg or is_deg
                new_at = (
                    self._pick_preferred_deg_at(prev_at, cur_at, deg_min=eff_deg_min)
                    if new_deg
                    else None
                )
                new_dk = _merge_degraded_kind(prev_dk, cur_dk, new_deg)
                new_m = max(prev_m, cur_m)
                new_sub = max(prev_sub, cur_sub)
                new_lt = max(prev_lt, cur_lt)
                new_p3 = max(prev_p3, cur_p3)
                new_uc = max(prev_uc, cur_uc)
                if new_steps == 0 and not new_deg and not new_dk:
                    continue
                pending_updates[hk] = {
                    "steps": new_steps,
                    "degraded": new_deg,
                    "degraded_24h_at": new_at,
                    "dk": new_dk,
                    "m": new_m,
                    "sub": new_sub,
                    "lt": new_lt,
                    "p3": new_p3,
                    "uc": new_uc,
                }
                prev_has_meta = (prev_m > 0) or (prev_sub > 0) or (prev_lt > 0) or (prev_p3 > 0) or (prev_uc > 0)
                new_has_meta = (new_m > 0) or (new_sub > 0) or (new_lt > 0) or (new_p3 > 0) or (new_uc > 0)
                dk_upgraded = _DEGRADED_KIND_SEVERITY.get(new_dk, 0) > _DEGRADED_KIND_SEVERITY.get(prev_dk, 0)
                if (
                    (new_steps - prev_steps) >= 25
                    or (new_deg and not prev_deg)
                    or (new_at != prev_at)
                    or dk_upgraded
                    or (new_has_meta and not prev_has_meta)
                    or (new_p3 - prev_p3) >= 25
                ):
                    dirty = True

        if not dirty:
            return False

        hmap = self._hourly_step_buckets.setdefault(canon_key, {})
        hmap.update(pending_updates)
        for old_hk in [k for k in hmap if k < min_valid_h]:
            hmap.pop(old_hk, None)
        if len(hmap) > 336:
            for old_hk in sorted(hmap.keys())[:-336]:
                hmap.pop(old_hk, None)
        while len(self._hourly_step_buckets) > 32:
            evict_candidates = [k for k in self._hourly_step_buckets if k != canon_key]
            if not evict_candidates:
                break
            oldest_acct = min(
                evict_candidates,
                key=lambda k: max(self._hourly_step_buckets[k].keys()) if self._hourly_step_buckets[k] else -1,
            )
            self._hourly_step_buckets.pop(oldest_acct, None)
        self._save_own()
        return True

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

    def get_switch_timestamps(
        self,
        since_ts: float,
        *,
        timeline: Optional[List[Tuple[float, str]]] = None,
    ) -> List[float]:
        """Return timestamps where the active account actually changed from a different prior account."""
        tl = timeline if timeline is not None else self.get_timeline(since_ts)
        if len(tl) < 2:
            return []
        return [ts for ts, _ in tl[1:] if ts >= since_ts]

    def list_known_accounts(
        self,
        since_ts: float,
        current_label: str = "",
        *,
        timeline: Optional[List[Tuple[float, str]]] = None,
    ) -> List[str]:
        """Return known masked account labels ordered by most-recent appearance (current online first)."""
        tl = timeline if timeline is not None else self.get_timeline(since_ts)
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
