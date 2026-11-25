"""Next-generation time-decayed sliding-window adaptive step-limit model (v7.3.0).

Pure, side-effect-free module with ZERO dependency on ``core.log_monitor`` to prevent
circular imports. Implements six-layer adaptive protection thresholds with plain-Chinese
explanations for UI display and JSON snapshots.
"""

from __future__ import annotations

import datetime
import math
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.account_ledger import normalize_account_tier

_DATACLASS_SLOTS: Dict[str, Any] = {"slots": True} if sys.version_info >= (3, 10) else {}

HALF_LIFE_DAYS: float = 3.5
_MAJOR_DEGRADED_KINDS = frozenset({"t1_multi", "t2", "t3"})
_TIER_DISPLAY_LABELS: Dict[str, str] = {
    "ultra": "Ultra",
    "pro": "Pro",
    "free": "Free",
}


@dataclass(frozen=True, **_DATACLASS_SLOTS)
class AdaptiveStepsResult:
    warn_limit: int
    alert_limit: int
    mode: str                                 # "default" | "adaptive_up" | "adaptive_down"
    note: str                                 # Plain-Chinese summary (zero math jargon)
    detail_lines: Tuple[str, ...] = ()        # Plain-Chinese tooltip breakdown (1..4 lines)
    hist_safe_peak: int = 0                   # Time-decayed account safe capacity estimate
    peer_prior_peak: int = 0                  # Same-tier peer reference peak (0 when unused)
    peer_weight_pct: int = 0                  # Peer prior blending weight 0..25
    effective_weighted_steps: int = 0         # Current 24h effective cold-input/lightweight weighted steps
    pool_24h_steps: int = 0                   # Active pool (gemini or 3p) 24h raw steps
    quota_headroom_steps: Optional[int] = None
    soft_over_limit: bool = False             # True when over limit yet quota ample & live T0
    low_quota_tightened: bool = False         # True only when low 5h/weekly actually lowered alert_limit
    gemini_24h_steps: int = 0                 # Independent 24h Gemini pool steps
    third_party_24h_steps: int = 0            # Independent 24h 3P pool steps
    high_deg_capped: bool = False             # True when actively held by high-load degradation fallback cap
    high_deg_ref_steps: int = 0               # Reference step count where the active high-load degradation occurred


def _resolve_tier_params(account_tier: str, base_alert: int) -> Tuple[str, int, int, int, int]:
    """Return ``(norm_tier, deg_min, floor, cap, low_load_min)``."""
    norm = normalize_account_tier(account_tier)
    if norm == "pro":
        return norm, 50, 100, max(2 * base_alert, 3000), 40
    if norm == "free":
        return norm, 30, 80, min(base_alert, 600), 20
    # "ultra" or "" (unknown tier keeps legacy baseline bounds)
    return norm, 400, 600, max(3 * base_alert, 5000), 200


def _pick_preferred_deg_at(
    a: Optional[int],
    b: Optional[int],
    deg_min: int = 400,
) -> Optional[int]:
    """Pick the most informative degradation load: smallest >= deg_min if any, else smallest positive."""
    vals = [int(v) for v in (a, b) if v is not None and not isinstance(v, bool) and int(v) > 0]
    if not vals:
        return None
    th = max(1, int(deg_min))
    high = [v for v in vals if v >= th]
    return min(high) if high else min(vals)


def _is_major_degradation_rec(rec: Dict[str, Any]) -> Tuple[bool, bool]:
    """Return ``(is_major_deg, is_t1_single)`` for an hourly bucket record."""
    raw_deg = bool(rec.get("degraded", False))
    dk = str(rec.get("dk", "") or "").strip().lower()
    if raw_deg:
        # An explicit account-wide degraded flag (including legacy d=True, dk="") MUST NEVER
        # be whitewashed by a co-occurring t1_single label in the same hour.
        return True, False
    if dk in _MAJOR_DEGRADED_KINDS:
        return True, False
    if dk == "t1_single":
        return False, True
    return False, False


def _extract_bucket_counts(rec: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Return ``(total_steps, gemini_steps, p3_steps, light_steps_clamped_to_gemini, uc_k)``."""
    total_s = max(0, int(rec.get("steps", 0) or 0))
    p3_s = min(total_s, max(0, int(rec.get("p3", 0) or 0)))
    gem_s = max(0, total_s - p3_s)
    # Lightweight immune models (Hydrogen/Flash) strictly belong to Gemini pool — never subtracted from 3P!
    lt_s = min(gem_s, max(0, int(rec.get("lt", 0) or 0)))
    uc_k = max(0, int(rec.get("uc", 0) or 0))
    return total_s, gem_s, p3_s, lt_s, uc_k


def _sum_24h_pools(
    hourly_buckets: Dict[int, Dict[str, Any]],
    now_ts: float,
) -> Tuple[int, int, int]:
    """Return ``(total_24h, gemini_24h, third_party_24h)`` over the rolling 24h hourly window."""
    if not hourly_buckets:
        return 0, 0, 0
    cur_h = int(math.floor(float(now_ts) / 3600.0))
    start_h = cur_h - 23
    tot = 0
    gem = 0
    p3 = 0
    for raw_h, rec in hourly_buckets.items():
        if not isinstance(rec, dict):
            continue
        try:
            hk = int(raw_h)
        except (TypeError, ValueError):
            continue
        if hk < start_h or hk > cur_h:
            continue
        ts, gs, ps, _, _ = _extract_bucket_counts(rec)
        tot += ts
        gem += gs
        p3 += ps
    return tot, gem, p3


def compute_effective_weighted_steps(
    hourly_buckets: Dict[int, Dict[str, Any]],
    *,
    now_ts: float,
    pool_family: str = "gemini",
) -> Tuple[int, int]:
    """Return ``(pool_raw_steps, effective_weighted_steps)`` over the latest 24h window.

    - ``pool_family`` accepts ``"gemini"``, ``"3p"``, or ``"all"``.
    - Lightweight immune models (``lt``) receive a ``0.75x`` discount ONLY inside Gemini / all-account counts
      and NEVER deduct from the 3P pool.
    - Cold-compute input tokens (``uc``, in K tokens) are proportionally allocated to the requested pool per hour
      so Gemini cold input never inflates 3P (and vice versa). When average cold input per step exceeds ``25K``,
      a gentle multiplier ``1.0 .. 2.5x`` is applied.
    """
    if not hourly_buckets:
        return 0, 0
    cur_h = int(math.floor(float(now_ts) / 3600.0))
    start_h = cur_h - 23
    pf = str(pool_family or "gemini").strip().lower()
    if pf not in ("gemini", "3p", "all"):
        pf = "gemini"

    pool_raw = 0
    weighted_base = 0.0
    uc_k_total = 0.0
    for raw_h, rec in hourly_buckets.items():
        if not isinstance(rec, dict):
            continue
        try:
            hk = int(raw_h)
        except (TypeError, ValueError):
            continue
        if hk < start_h or hk > cur_h:
            continue
        total_s, gem_s, p3_s, lt_s, uc_k = _extract_bucket_counts(rec)
        if total_s <= 0:
            continue
        if pf == "3p":
            if p3_s <= 0:
                continue
            pool_raw += p3_s
            weighted_base += float(p3_s)
            uc_k_total += float(uc_k) * (float(p3_s) / float(total_s))
        elif pf == "gemini":
            if gem_s <= 0:
                continue
            pool_raw += gem_s
            weighted_base += float(gem_s) - 0.25 * float(lt_s)
            uc_k_total += float(uc_k) * (float(gem_s) / float(total_s))
        else:
            pool_raw += total_s
            weighted_base += float(total_s) - 0.25 * float(lt_s)
            uc_k_total += float(uc_k)

    if pool_raw <= 0:
        return 0, 0
    avg_uc_k = uc_k_total / float(max(1, pool_raw))
    cold_mult = 1.0 + min(1.5, (avg_uc_k - 25.0) / 50.0) if avg_uc_k > 25.0 else 1.0
    eff_weighted = max(0, int(round(weighted_base * cold_mult)))
    return pool_raw, eff_weighted


def compute_account_time_decayed_records(
    hourly_buckets: Dict[int, Dict[str, Any]],
    now_ts: float,
    *,
    pool_family: str = "gemini",
    half_life_days: float = HALF_LIFE_DAYS,
    deg_min: int = 400,
) -> Dict[str, Any]:
    """Bounded O(360) sliding-window aggregation with time decay and cross-midnight recovery tracking.

    Guarantees:
    - Distinguishes ``t1_single`` (single-conversation isolated drift: does NOT contaminate the day,
      does NOT clear safe_steps, does NOT trigger tightening) from account-wide ``t1_multi/t2/t3``
      (and legacy ``degraded=True`` records without ``dk``).
    - Computes cross-midnight continuous healthy recovery ``(recov_steps, recov_hours, last_major_deg_age_h)``
      allowing 0-step idle gaps (e.g. lunch/sleep) while counting only non-empty healthy hours strictly
      after ``last_major_deg_h`` and strictly before ``cur_h``.
    - Returns settled daily peaks with ``age_days`` & ``weight`` plus unresolved major degradations.
    """
    now_f = float(now_ts)
    hl = max(0.5, float(half_life_days))
    eff_deg_min = max(1, int(deg_min))
    empty_result: Dict[str, Any] = {
        "daily_peaks": [],
        "settled_day_count": 0,
        "has_unsettled_major_deg": False,
        "unsettled_major_degs": [],
        "last_major_deg_h": None,
        "last_major_deg_age_h": None,
        "recov_steps": 0,
        "recov_hours": 0,
        "bridge_steps": 0,
        "bridge_hours": 0,
        "bridge_has_unsettled": False,
        "t1_single_ignored_count": 0,
    }
    if not hourly_buckets:
        return empty_result

    pf = str(pool_family or "gemini").strip().lower()
    if pf not in ("gemini", "3p", "all"):
        pf = "gemini"

    cur_h = int(math.floor(now_f / 3600.0))
    max_settled_h = int(math.floor((now_f - 86400.0) / 3600.0)) - 1
    min_horizon_h = max_settled_h - 360

    def _day_of_hour(h_epoch: int) -> str:
        return datetime.datetime.fromtimestamp((h_epoch + 1) * 3600 - 1).strftime("%Y-%m-%d")

    settled_map: Dict[int, Dict[str, Any]] = {}
    all_parsed: Dict[int, Tuple[int, bool, Optional[int]]] = {}
    unsettled_degraded_days: set[str] = set()
    unsettled_deg_hours: List[int] = []
    unsettled_major_degs: List[Tuple[int, Optional[int]]] = []
    last_major_deg_h: Optional[int] = None
    t1_single_ignored = 0

    for raw_h, rec in hourly_buckets.items():
        if not isinstance(rec, dict):
            continue
        try:
            hk = int(raw_h)
        except (TypeError, ValueError):
            continue
        if hk < min_horizon_h or hk > cur_h + 1:
            continue

        total_s, gem_s, p3_s, _, _ = _extract_bucket_counts(rec)
        if pf == "3p":
            steps_v = p3_s
        elif pf == "gemini":
            steps_v = gem_s
        else:
            steps_v = total_s

        is_major_deg, is_t1_single = _is_major_degradation_rec(rec)
        if is_t1_single:
            t1_single_ignored += 1

        deg_at_raw = rec.get("degraded_24h_at")
        deg_at_v: Optional[int] = None
        if is_major_deg and deg_at_raw is not None and not isinstance(deg_at_raw, bool):
            try:
                parsed_at = int(deg_at_raw)
                if parsed_at > 0:
                    deg_at_v = parsed_at
            except (TypeError, ValueError):
                deg_at_v = None

        if is_major_deg and (last_major_deg_h is None or hk > last_major_deg_h):
            last_major_deg_h = hk

        if steps_v > 0 or is_major_deg:
            all_parsed[hk] = (steps_v, is_major_deg, deg_at_v)

        if hk > max_settled_h:
            if is_major_deg:
                unsettled_degraded_days.add(_day_of_hour(hk))
                unsettled_deg_hours.append(hk)
                unsettled_major_degs.append((hk, deg_at_v))
            continue

        if steps_v == 0 and not is_major_deg:
            continue
        settled_map[hk] = {
            "steps": steps_v,
            "degraded": is_major_deg,
            "degraded_24h_at": deg_at_v,
        }

    unsettled_major_degs.sort(key=lambda item: item[0])
    # Fallback 24h rolling window step load when an unsettled major degradation bucket lacks explicit degraded_24h_at
    resolved_unsettled_degs: List[Tuple[int, Optional[int]]] = []
    for uhk, u_at in unsettled_major_degs:
        if u_at is None:
            win_sum = sum(all_parsed[h][0] for h in range(uhk - 23, uhk + 1) if h in all_parsed)
            u_at = win_sum if win_sum > 0 else None
        resolved_unsettled_degs.append((uhk, u_at))
    unsettled_major_degs = resolved_unsettled_degs

    def _best_24h_clean_window(cmap: Dict[int, int]) -> Tuple[int, int]:
        if not cmap:
            return 0, 0
        min_c = min(cmap.keys())
        max_c = max(cmap.keys())
        win_steps = 0
        win_hours = 0
        best_3h_steps = 0
        best_3h_hours = 0
        max_any_hours = 0
        max_any_steps = 0
        for end_h in range(min_c, max_c + 1):
            enter_s = cmap.get(end_h, 0)
            if enter_s > 0:
                win_steps += enter_s
                win_hours += 1
            leave_h = end_h - 24
            if leave_h >= min_c:
                leave_s = cmap.get(leave_h, 0)
                if leave_s > 0:
                    win_steps -= leave_s
                    win_hours -= 1
            if (win_hours > max_any_hours) or (win_hours == max_any_hours and win_steps > max_any_steps):
                max_any_hours = win_hours
                max_any_steps = win_steps
            if win_hours >= 3:
                if (win_steps > best_3h_steps) or (win_steps == best_3h_steps and win_hours > best_3h_hours):
                    best_3h_steps = win_steps
                    best_3h_hours = win_hours
        if best_3h_hours >= 3:
            return best_3h_steps, best_3h_hours
        return max_any_steps, max_any_hours

    # 1) Refutation recovery window (for lifting stale historical tightening): spans strictly after last_major_deg_h
    clean_map: Dict[int, int] = {}
    recov_min_h = (last_major_deg_h + 1) if last_major_deg_h is not None else max(min_horizon_h, cur_h - 48)
    for hk, (steps_v, is_major_deg, _deg_at) in all_parsed.items():
        if hk >= cur_h or hk < recov_min_h or is_major_deg or steps_v <= 0:
            continue
        clean_map[hk] = steps_v
    recov_steps, recov_hours = _best_24h_clean_window(clean_map)

    # 2) Recent cross-midnight bridge window: strictly capped to last 48h and MUST contain unsettled hours
    #    so 10-day-old peaks can NEVER bypass the 3.5-day exponential decay!
    bridge_min_h = max(cur_h - 48, (last_major_deg_h + 1) if last_major_deg_h is not None else (cur_h - 48))
    bridge_map: Dict[int, int] = {
        hk: steps_v
        for hk, (steps_v, is_major_deg, _deg_at) in all_parsed.items()
        if bridge_min_h <= hk < cur_h and (not is_major_deg) and steps_v > 0
    }
    bridge_has_unsettled = bool(bridge_map and max(bridge_map.keys()) > max_settled_h)
    bridge_steps, bridge_hours = _best_24h_clean_window(bridge_map) if bridge_has_unsettled else (0, 0)

    last_major_deg_age_h: Optional[float] = None
    if last_major_deg_h is not None:
        # Age in hours since the closing boundary of last_major_deg_h (non-negative)
        last_major_deg_age_h = max(0.0, (now_f - float(last_major_deg_h + 1) * 3600.0) / 3600.0)

    if not settled_map:
        return {
            "daily_peaks": [],
            "settled_day_count": 0,
            "has_unsettled_major_deg": bool(unsettled_deg_hours),
            "unsettled_major_degs": unsettled_major_degs,
            "last_major_deg_h": last_major_deg_h,
            "last_major_deg_age_h": last_major_deg_age_h,
            "recov_steps": recov_steps,
            "recov_hours": recov_hours,
            "bridge_steps": bridge_steps,
            "bridge_hours": bridge_hours,
            "bridge_has_unsettled": bridge_has_unsettled,
            "t1_single_ignored_count": t1_single_ignored,
        }

    def _touches_unsettled_deg(h_val: int) -> bool:
        return any(u_hk - 23 <= h_val <= u_hk for u_hk in unsettled_deg_hours)

    day_own_steps: Dict[str, int] = {}
    day_first_deg_h: Dict[str, int] = {}
    day_last_deg_h: Dict[str, int] = {}
    day_last_active_h: Dict[str, int] = {}
    for hk, rec in settled_map.items():
        d_str = _day_of_hour(hk)
        day_own_steps[d_str] = day_own_steps.get(d_str, 0) + rec["steps"]
        prev_act = day_last_active_h.get(d_str)
        if prev_act is None or hk > prev_act:
            day_last_active_h[d_str] = hk
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
        if enter_rec is not None and enter_rec["degraded"]:
            deg_load = enter_rec["degraded_24h_at"] if enter_rec["degraded_24h_at"] is not None else win_steps
            if deg_load is not None and deg_load > 0:
                slot = by_day.setdefault(d_str, {"date": d_str, "safe_steps": 0, "degraded_at": None})
                slot["degraded_at"] = _pick_preferred_deg_at(
                    slot["degraded_at"],
                    deg_load,
                    deg_min=eff_deg_min,
                )
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

    post_deg_min_floor = min(1200, eff_deg_min * 3) if eff_deg_min < 400 else 1200
    daily_peaks: List[Dict[str, Any]] = []
    for d_str in sorted(by_day.keys()):
        rec = by_day[d_str]
        deg_at = rec["degraded_at"]
        safe_s = int(rec["safe_steps"])
        if deg_at is not None:
            safe_s = min(safe_s, max(0, deg_at - 1))
        last_act_h = day_last_active_h.get(d_str)
        if last_act_h is not None:
            age_days = max(0.0, (now_f - float(last_act_h + 1) * 3600.0) / 86400.0)
        else:
            age_days = 1.0
        weight = 0.5 ** (age_days / hl)
        entry: Dict[str, Any] = {
            "date": d_str,
            "safe_steps": safe_s,
            "degraded_at": deg_at,
            "age_days": round(age_days, 4),
            "weight": weight,
        }
        if (
            deg_at is not None
            and d_str not in unsettled_degraded_days
            and d_str not in day_post_deg_touched_unsettled
        ):
            post_h_cnt = day_post_deg_hours.get(d_str, 0)
            post_steps = day_post_deg_steps.get(d_str, 0)
            if post_h_cnt >= 3 and post_steps >= max(post_deg_min_floor, int((deg_at or 0) * 1.5)):
                entry["post_deg_safe_steps"] = post_steps
        daily_peaks.append(entry)

    return {
        "daily_peaks": daily_peaks,
        "settled_day_count": len(daily_peaks),
        "has_unsettled_major_deg": bool(unsettled_deg_hours),
        "unsettled_major_degs": unsettled_major_degs,
        "last_major_deg_h": last_major_deg_h,
        "last_major_deg_age_h": last_major_deg_age_h,
        "recov_steps": recov_steps,
        "recov_hours": recov_hours,
        "bridge_steps": bridge_steps,
        "bridge_hours": bridge_hours,
        "bridge_has_unsettled": bridge_has_unsettled,
        "t1_single_ignored_count": t1_single_ignored,
    }


def _evaluate_account_history(
    daily_peaks: List[Dict[str, Any]],
    *,
    b_alert: int,
    deg_min: int,
    tier_floor: int,
    low_load_min: int,
    norm_tier: str,
) -> Tuple[float, float, int, Optional[int], str]:
    """Evaluate settled daily peaks -> ``(own_est, w_own, hist_safe_peak, latest_high_deg_steps, latest_high_deg_date)``."""
    latest_high_deg_date = ""
    latest_high_deg_steps: Optional[int] = None
    valid_safe_days: List[Tuple[float, int, str]] = []
    post_ref = b_alert if norm_tier in ("", "ultra") else min(b_alert, max(tier_floor, deg_min * 3))

    for rec in daily_peaks:
        d_str = str(rec.get("date", ""))
        safe_s = max(0, int(rec.get("safe_steps", 0) or 0))
        post_safe = max(0, int(rec.get("post_deg_safe_steps", 0) or 0))
        deg_raw = rec.get("degraded_at")
        deg_at = max(1, int(deg_raw)) if deg_raw is not None else None
        weight = max(1e-6, float(rec.get("weight", 1.0)))

        if deg_at is None:
            if safe_s > 0:
                valid_safe_days.append((weight, safe_s, d_str))
            if (
                latest_high_deg_steps is not None
                and d_str > latest_high_deg_date
                and safe_s >= latest_high_deg_steps
            ):
                latest_high_deg_steps = None
                latest_high_deg_date = ""
        else:
            if deg_at >= deg_min:
                latest_high_deg_date = d_str
                latest_high_deg_steps = deg_at
            if post_safe >= max(post_ref, deg_at):
                valid_safe_days.append((weight, post_safe, d_str))
                if (
                    latest_high_deg_steps is not None
                    and d_str >= latest_high_deg_date
                    and post_safe >= latest_high_deg_steps
                ):
                    latest_high_deg_steps = None
                    latest_high_deg_date = ""

    if not valid_safe_days:
        return 0.0, 0.0, 0, latest_high_deg_steps, latest_high_deg_date

    # Low-load rest-day filtering using decayed peak weight max(w_i * p_i) so ancient peaks never erase newer data
    max_wp = max(w * float(p) for w, p, _ in valid_safe_days)
    min_keep = max(float(low_load_min), 0.25 * max_wp)
    filtered = [(w, p, d) for (w, p, d) in valid_safe_days if float(p) >= min_keep]
    if not filtered:
        filtered = valid_safe_days

    w_own = sum(w for w, _, _ in filtered)
    if w_own <= 0.0:
        return 0.0, 0.0, 0, latest_high_deg_steps, latest_high_deg_date
    own_est = sum(w * float(p) for w, p, _ in filtered) / w_own
    return own_est, w_own, int(round(own_est)), latest_high_deg_steps, latest_high_deg_date


def _compute_same_tier_peer_prior(
    *,
    norm_tier: str,
    account_label: str,
    peer_hourly_by_account: Optional[Dict[str, Dict[int, Dict[str, Any]]]],
    peer_tiers: Optional[Dict[str, str]],
    now_ts: float,
    b_alert: int,
    deg_min: int,
    tier_floor: int,
    low_load_min: int,
    half_life_days: float,
) -> float:
    """Compute average time-decayed safe estimate across healthy same-tier peer accounts."""
    if (
        norm_tier not in ("ultra", "pro", "free")
        or not isinstance(peer_hourly_by_account, dict)
        or not isinstance(peer_tiers, dict)
    ):
        return 0.0

    self_low = (account_label or "").strip().lower()
    tiers_by_low: Dict[str, str] = {}
    for k, raw_t in peer_tiers.items():
        kl = str(k or "").strip().lower()
        nt = normalize_account_tier(str(raw_t or ""))
        if kl and nt:
            tiers_by_low[kl] = nt

    peer_ests: List[float] = []
    for p_acct, p_buckets in peer_hourly_by_account.items():
        pl = str(p_acct or "").strip().lower()
        if not pl or (self_low and pl == self_low):
            continue
        if tiers_by_low.get(pl) != norm_tier:
            continue
        if not isinstance(p_buckets, dict) or not p_buckets:
            continue
        p_rec = compute_account_time_decayed_records(
            p_buckets,
            now_ts,
            pool_family="all",
            half_life_days=half_life_days,
            deg_min=deg_min,
        )
        if p_rec["has_unsettled_major_deg"]:
            continue
        p_est, p_w, _p_pk, p_deg_steps, p_deg_date = _evaluate_account_history(
            p_rec["daily_peaks"],
            b_alert=b_alert,
            deg_min=deg_min,
            tier_floor=tier_floor,
            low_load_min=low_load_min,
            norm_tier=norm_tier,
        )
        # Skip peers actively tightened within the last 7 days
        if p_deg_steps is not None and p_deg_steps < b_alert:
            recent_deg = any(
                r.get("date") == p_deg_date and float(r.get("age_days", 99.0)) <= 7.0
                for r in p_rec["daily_peaks"]
            )
            if recent_deg:
                continue
        if p_w > 0.0 and p_est > 0.0:
            peer_ests.append(p_est)

    if not peer_ests:
        return 0.0
    return sum(peer_ests) / float(len(peer_ests))


def compute_adaptive_steps_v2(
    base_warn: int,
    base_alert: int,
    own_hourly_buckets: Dict[int, Dict[str, Any]],
    *,
    now_ts: float,
    account_tier: str = "",                       # "ultra" | "pro" | "free" | ""
    pool_family: str = "gemini",                  # "gemini" | "3p"
    peer_hourly_by_account: Optional[Dict[str, Dict[int, Dict[str, Any]]]] = None,
    peer_tiers: Optional[Dict[str, str]] = None,  # Only same-tier peers contribute to weak prior
    account_label: str = "",
    live_grade: str = "T0",                       # Live T1/T2/T3 strictly forbids relaxation/unban!
    live_24h_raw_steps: Optional[int] = None,
    live_5h_pct: Optional[float] = None,
    live_weekly_pct: Optional[float] = None,
    burn_pct_per_hour: Optional[float] = None,
    weekly_burn_pct_per_hour: Optional[float] = None,
    will_exhaust_before_reset: bool = False,
    last_1h_steps: int = 0,
    half_life_days: float = HALF_LIFE_DAYS,
) -> AdaptiveStepsResult:
    """Compute next-generation six-layer adaptive step protection limits with plain-Chinese explanations."""
    b_alert = max(50, int(base_alert))
    b_warn = max(1, min(b_alert - 1, int(base_warn)))
    warn_ratio = min(0.95, max(0.50, float(b_warn) / float(b_alert)))
    hl = max(0.5, float(half_life_days))

    norm_tier, deg_min, tier_floor, tier_cap, low_load_min = _resolve_tier_params(account_tier, b_alert)
    tier_label = _TIER_DISPLAY_LABELS.get(norm_tier, "")
    pool_norm = "3p" if str(pool_family or "").strip().lower() == "3p" else "gemini"

    def _calc_warn(alert_val: int) -> int:
        return max(1, min(alert_val - 1, int(round((alert_val * warn_ratio) / 10.0) * 10)))

    # 1. Pool breakdowns (independent) & all-account effective weighted steps (numerator/denominator consistent!)
    total_24h_bucket, gemini_24h, third_party_24h = _sum_24h_pools(own_hourly_buckets, now_ts)
    pool_24h_steps = third_party_24h if pool_norm == "3p" else gemini_24h
    _, effective_weighted_steps = compute_effective_weighted_steps(
        own_hourly_buckets,
        now_ts=now_ts,
        pool_family="all",
    )
    eff_live_24h = max(0, int(live_24h_raw_steps)) if live_24h_raw_steps is not None else total_24h_bucket

    # 2. All-account time-decayed history & recovery window (prevents 2500 Gemini + 5 3P -> false red spike!)
    own_rec = compute_account_time_decayed_records(
        own_hourly_buckets,
        now_ts,
        pool_family="all",
        half_life_days=hl,
        deg_min=deg_min,
    )
    daily_peaks: List[Dict[str, Any]] = own_rec["daily_peaks"]
    settled_day_count: int = int(own_rec["settled_day_count"])
    has_unsettled_major_deg: bool = bool(own_rec["has_unsettled_major_deg"])
    unsettled_major_degs: List[Tuple[int, Optional[int]]] = own_rec["unsettled_major_degs"]
    last_major_deg_age_h: Optional[float] = own_rec["last_major_deg_age_h"]
    recov_steps: int = int(own_rec["recov_steps"])
    recov_hours: int = int(own_rec["recov_hours"])
    bridge_steps: int = int(own_rec.get("bridge_steps", 0) or 0)
    bridge_hours: int = int(own_rec.get("bridge_hours", 0) or 0)
    bridge_has_unsettled: bool = bool(own_rec.get("bridge_has_unsettled", False))

    # Safety iron rule: live T1/T2/T3 strictly locks out recovery refutation and bridge relaxation
    grade_u = str(live_grade or "").strip().upper()
    live_is_degraded = grade_u in ("T1", "T2", "T3")
    if live_is_degraded:
        recov_steps = 0
        recov_hours = 0
        bridge_steps = 0
        bridge_hours = 0
        bridge_has_unsettled = False

    own_est, w_own, hist_safe_peak, latest_high_deg_steps, latest_deg_date = _evaluate_account_history(
        daily_peaks,
        b_alert=b_alert,
        deg_min=deg_min,
        tier_floor=tier_floor,
        low_load_min=low_load_min,
        norm_tier=norm_tier,
    )
    latest_high_deg_age_days: Optional[float] = None
    if latest_high_deg_steps is not None and latest_deg_date:
        for _rec in daily_peaks:
            if str(_rec.get("date", "")) == latest_deg_date:
                latest_high_deg_age_days = float(_rec.get("age_days", 0.0))
                break

    # Clamp baseline to tier_cap (e.g. Free tier cap=600) so higher settled peaks stay monotonic and never dive!
    eff_base_alert = min(b_alert, tier_cap)
    # Single-day soft cap 4200 applies ONLY when W_own < 1.2 and tier is known (preserves 1300->1500 & 3048->3600)
    eff_cap = min(tier_cap, 4200) if (w_own < 1.2 and norm_tier != "") else tier_cap

    # Expire settled high-load degradation (>7 days old) BEFORE merging unsettled degradations so ancient blips never resurrect
    if (
        latest_high_deg_steps is not None
        and latest_high_deg_steps >= eff_base_alert
        and latest_high_deg_age_days is not None
        and latest_high_deg_age_days > 7.0
    ):
        latest_high_deg_steps = None

    # Also incorporate unsettled high-load account-wide degradation (>= deg_min) for tightening & refutation guard
    unsettled_high_deg: Optional[int] = None
    for _uhk, u_at in unsettled_major_degs:
        if u_at is not None and u_at >= deg_min:
            unsettled_high_deg = _pick_preferred_deg_at(unsettled_high_deg, u_at, deg_min=deg_min)

    active_deg_steps: Optional[int] = latest_high_deg_steps
    refute_deg_steps: Optional[int] = latest_high_deg_steps
    if unsettled_high_deg is not None:
        if active_deg_steps is None:
            active_deg_steps = unsettled_high_deg
            refute_deg_steps = unsettled_high_deg
        else:
            # Never let a higher unsettled degradation mask a stricter historical tightening
            active_deg_steps = min(active_deg_steps, unsettled_high_deg)
            refute_deg_steps = max(refute_deg_steps or active_deg_steps, unsettled_high_deg)

    # 3. Check active hard tightening (< eff_base_alert) or smooth high-load fallback cap (>= eff_base_alert)
    eff_alert = eff_base_alert
    mode = "default"
    tightening_lifted = False
    high_deg_capped = False
    high_deg_lifted = False
    high_deg_ref_steps = 0
    used_bridge = False
    used_peer_cold = False
    used_peer_blend = False
    peer_prior_peak = 0
    peer_weight_pct = 0

    has_active_tightening = False
    tight_alert = eff_base_alert
    if active_deg_steps is not None and active_deg_steps < eff_base_alert:
        tight_raw = int(math.floor((active_deg_steps * 0.85) / 50.0) * 50)
        tight_alert = max(tier_floor, min(eff_base_alert, tight_raw))
        has_active_tightening = tight_alert < eff_base_alert

    if has_active_tightening:
        refute_base = refute_deg_steps if refute_deg_steps is not None else active_deg_steps
        refute_th = max(eff_base_alert, int(refute_base or 0) * 2)
        if (not live_is_degraded) and recov_hours >= 3 and recov_steps >= refute_th:
            tightening_lifted = True
            has_active_tightening = False
            mode = "default"
            eff_alert = eff_base_alert
        else:
            mode = "adaptive_down"
            eff_alert = tight_alert
    elif active_deg_steps is not None:
        # High-load degradation (>= baseline) or sub-baseline degradation clamped at tier_floor:
        # 1) Sustained post-degradation healthy throughput (>=3h & >= refute threshold) lifts the cap.
        #    Require 1.15x when unsettled degradation is still inside 24h window to prevent 1199 vs 1200 cliff jump.
        # 2) Otherwise smoothly fall back to 85% of active_deg_steps capped by own earned capacity (strictly monotonic!).
        refute_high_base = refute_deg_steps if refute_deg_steps is not None else active_deg_steps
        if has_unsettled_major_deg:
            refute_high_th = max(eff_base_alert, int(math.ceil(float(refute_high_base or 0) * 1.15)))
        else:
            refute_high_th = max(eff_base_alert, int(refute_high_base or 0))
        if (not live_is_degraded) and recov_hours >= 3 and recov_steps >= refute_high_th:
            high_deg_lifted = True
            active_deg_steps = None
        else:
            high_deg_capped = True
            high_deg_ref_steps = int(active_deg_steps)
            high_deg_raw = int(math.floor((active_deg_steps * 0.85) / 50.0) * 50)
            high_deg_cap = max(eff_base_alert, min(eff_cap, high_deg_raw))
            if active_deg_steps >= eff_base_alert and w_own > 0.0 and own_est > eff_base_alert:
                own_ceil_hd = max(eff_base_alert, int(math.ceil((own_est * 1.15) / 100.0) * 100))
                target_hd = min(eff_cap, own_ceil_hd, high_deg_cap)
            else:
                # Without proven clean history above baseline (or when clamped at tier_floor), stay strictly at baseline
                target_hd = eff_base_alert
            eff_alert = max(eff_base_alert, target_hd)
            mode = "adaptive_up" if eff_alert > eff_base_alert else "default"

    # 4. Same-tier peer weak prior & own time-decayed capacity calibration (only when not degraded/tightened)
    can_use_peer = (
        norm_tier in ("ultra", "pro", "free")
        and (not has_active_tightening)
        and (not tightening_lifted)
        and (not high_deg_capped)
        and (active_deg_steps is None)
        and (not has_unsettled_major_deg)
        and (not live_is_degraded)
    )
    peer_est = 0.0
    w_peer = 0.0
    if can_use_peer:
        peer_est = _compute_same_tier_peer_prior(
            norm_tier=norm_tier,
            account_label=account_label,
            peer_hourly_by_account=peer_hourly_by_account,
            peer_tiers=peer_tiers,
            now_ts=now_ts,
            b_alert=eff_base_alert,
            deg_min=deg_min,
            tier_floor=tier_floor,
            low_load_min=low_load_min,
            half_life_days=hl,
        )
        if peer_est > 0.0:
            peer_prior_peak = int(round(peer_est))
            w_peer = 0.25 / (1.0 + (w_own / 0.8))
            peer_weight_pct = int(round(w_peer * 100.0))

    if (not has_active_tightening) and (not tightening_lifted) and (active_deg_steps is None) and (eff_cap > eff_base_alert):
        if w_own > 0.0:
            own_ceil = max(eff_base_alert, int(math.ceil((own_est * 1.15) / 100.0) * 100))
            if peer_est > 0.0 and w_peer > 0.0:
                blended = own_est * (1.0 - w_peer) + peer_est * w_peer
                raw_target = int(math.ceil((blended * 1.15) / 100.0) * 100)
                # Hard anti-inflation guard: peer prior NEVER pushes alert above own_ceil
                raw_target = min(raw_target, own_ceil)
                if raw_target > eff_base_alert:
                    eff_alert = min(eff_cap, max(eff_base_alert, raw_target))
                    if eff_alert > eff_base_alert:
                        mode = "adaptive_up"
                        used_peer_blend = True
            else:
                if own_est > eff_base_alert:
                    raw_target = int(math.ceil((own_est * 1.15) / 100.0) * 100)
                    eff_alert = min(eff_cap, max(eff_base_alert, raw_target))
                    if eff_alert > eff_base_alert:
                        mode = "adaptive_up"
        elif peer_est > 0.0 and w_peer > 0.0:
            # Cold start with same-tier peer prior: conservative jump without extra 1.15x boost
            prior_base = float(eff_base_alert) * (1.0 - w_peer) + peer_est * w_peer
            cold_target = int(round(prior_base / 100.0) * 100)
            if cold_target > eff_base_alert:
                eff_alert = min(eff_cap, cold_target)
                if eff_alert > eff_base_alert:
                    mode = "adaptive_up"
                    used_peer_cold = True

    # 5. Cross-midnight conservative smooth bridge:
    #    - Strictly capped to recent <=48h unsettled window (cannot resurrect 10-day-old peaks)
    #    - Strictly respects eff_cap (never pierces 4200 single-day soft cap)
    #    - Strictly forbids brand-new Day 1 chasing (requires >=1 settled day OR lifted active tightening)
    not_brand_new = (settled_day_count >= 1) or tightening_lifted
    if (
        (not live_is_degraded)
        and (mode != "adaptive_down")
        and (not has_active_tightening)
        and (not high_deg_capped)
        and not_brand_new
        and bridge_has_unsettled
        and bridge_hours >= 6
        and bridge_steps > eff_alert
    ):
        maturity = min(0.70, max(0.0, (float(bridge_hours) - 3.0) / 18.0))
        bridge_raw = int(math.ceil((float(bridge_steps) * maturity) / 100.0) * 100)
        bridge_val = min(eff_cap, bridge_raw)
        if bridge_val > eff_alert:
            eff_alert = bridge_val
            mode = "adaptive_up"
            used_bridge = True

    # 6. 5h / weekly quota headroom estimation, low-quota tightening & ample-quota soft reminder
    quota_headroom_steps: Optional[int] = None
    steps_per_pct_5h: Optional[float] = None
    if (
        burn_pct_per_hour is not None
        and float(burn_pct_per_hour) > 0.1
        and int(last_1h_steps or 0) >= 5
    ):
        steps_per_pct_5h = float(last_1h_steps) / float(burn_pct_per_hour)
        if live_5h_pct is not None:
            est_h5 = int(round(max(0.0, float(live_5h_pct)) * steps_per_pct_5h))
            quota_headroom_steps = max(0, min(50000, est_h5))

    steps_per_pct_wk: Optional[float] = None
    if (
        weekly_burn_pct_per_hour is not None
        and float(weekly_burn_pct_per_hour) > 0.05
        and int(last_1h_steps or 0) >= 5
    ):
        steps_per_pct_wk = float(last_1h_steps) / float(weekly_burn_pct_per_hour)

    low_5h = live_5h_pct is not None and float(live_5h_pct) <= 15.0
    low_wk = live_weekly_pct is not None and float(live_weekly_pct) <= 10.0
    low_quota_tightened = False
    if (low_5h or low_wk) and eff_live_24h > 0:
        tight_cands: List[int] = []
        if low_5h and live_5h_pct is not None:
            if steps_per_pct_5h is not None:
                tight_cands.append(max(0, min(50000, int(round(max(0.0, float(live_5h_pct)) * steps_per_pct_5h)))))
            else:
                tight_cands.append(50)
        if low_wk and live_weekly_pct is not None:
            if steps_per_pct_wk is not None:
                tight_cands.append(max(0, min(50000, int(round(max(0.0, float(live_weekly_pct)) * steps_per_pct_wk)))))
            elif not low_5h:
                # Never mix 5h burn rate onto weekly remaining %; use conservative weekly % cushion instead
                tight_cands.append(max(100, int(round(max(0.0, float(live_weekly_pct)) * 30.0))))
        eff_buf = max(50, min(tight_cands) if tight_cands else 50)
        pre_alert = eff_alert
        candidate_alert = max(tier_floor, eff_live_24h + eff_buf)
        new_alert = min(pre_alert, candidate_alert)
        if new_alert < pre_alert:
            eff_alert = new_alert
            low_quota_tightened = True
            if eff_alert < eff_base_alert:
                mode = "adaptive_down"
            elif eff_alert == eff_base_alert and mode == "adaptive_up":
                mode = "default"

    eff_warn = _calc_warn(eff_alert)

    soft_over_limit = bool(
        (not live_is_degraded)
        and grade_u == "T0"
        and (not low_quota_tightened)
        and (not has_active_tightening)
        and (not high_deg_capped)
        and (not has_unsettled_major_deg)
        and live_5h_pct is not None
        and float(live_5h_pct) >= 50.0
        and (live_weekly_pct is None or float(live_weekly_pct) >= 50.0)
        and (not will_exhaust_before_reset)
        and eff_live_24h >= eff_alert
    )

    # 7. Plain-Chinese note & detail_lines (zero math/algorithm jargon!)
    tier_text = tier_label or "同档位"
    relax_tail = "，连续稳跑后会自动放宽" if eff_cap > eff_base_alert else ""
    if low_quota_tightened:
        note = "当前 5 小时或本周余量偏低，已主动收紧提醒线保护额度"
    elif high_deg_capped:
        if eff_alert > eff_base_alert:
            note = f"近期在 {high_deg_ref_steps} 步遇异常，保护线已回落保护至 {eff_alert} 步{relax_tail}"
        else:
            note = f"近期在 {high_deg_ref_steps} 步遇异常，保护线暂守默认 {eff_base_alert} 步{relax_tail}"
    elif mode == "adaptive_down":
        note = f"近期曾出现限流/掉缓存，保护线已自动收紧至 {eff_alert} 步，连续稳跑后会自动放宽"
    elif tightening_lifted and mode == "default":
        note = f"近期已连续健康跑过 {recov_steps} 步，已解除历史旧收紧，恢复默认保护线"
    elif high_deg_lifted and mode == "default":
        note = f"近期已连续健康跑过 {recov_steps} 步，已解除高位异常保护，恢复默认保护线"
    elif mode == "adaptive_up":
        if used_bridge:
            note = f"近期已连续稳跑 {bridge_hours} 小时（共 {bridge_steps} 步），保护线已平滑放宽至 {eff_alert} 步"
        elif used_peer_cold:
            note = f"本号记录尚少，暂按同为 {tier_text} 账号近期表现（{peer_weight_pct}% 参考）温和起步"
        elif used_peer_blend:
            note = f"结合本号近期表现与同为 {tier_text} 账号的参考（占 {peer_weight_pct}%）自动调整"
        else:
            note = f"按近期稳跑记录（约 {hist_safe_peak} 步）自动放宽，越近的日子权重越高"
    else:
        note = "初始建议值，积累近期稳跑记录后会自动校准"

    detail_lines: List[str] = [note]
    if low_quota_tightened and high_deg_capped and high_deg_ref_steps > 0:
        detail_lines.append(
            f"近期在 {high_deg_ref_steps} 步遇异常，当前同步回落保护"
        )
    if peer_prior_peak > 0 and peer_weight_pct > 0 and not used_peer_cold:
        detail_lines.append(
            f"同为 {tier_text} 账号近期稳跑约 {peer_prior_peak} 步（当前参考占比 {peer_weight_pct}%）"
        )
    # Compare effective_weighted_steps against same-basis total_24h_bucket (require >=20 steps so fresh 2-step switches don't over-report)
    eff_cmp_denom = total_24h_bucket if total_24h_bucket > 0 else eff_live_24h
    if (
        effective_weighted_steps > 0
        and eff_cmp_denom >= 20
        and effective_weighted_steps > int(round(eff_cmp_denom * 1.25))
    ):
        detail_lines.append(
            f"近期冷启动/大输入较多，实际算力消耗约折合 {effective_weighted_steps} 步"
        )
    if quota_headroom_steps is not None:
        detail_lines.append(
            f"按当前消耗速度，5 小时余量预计还可跑约 {quota_headroom_steps} 步"
        )
    if soft_over_limit:
        detail_lines.append("当前 24 小时步数较多，但剩余额度充裕且状态健康，可放心继续")

    return AdaptiveStepsResult(
        warn_limit=eff_warn,
        alert_limit=eff_alert,
        mode=mode,
        note=note,
        detail_lines=tuple(detail_lines[:4]),
        hist_safe_peak=hist_safe_peak,
        peer_prior_peak=peer_prior_peak,
        peer_weight_pct=peer_weight_pct,
        effective_weighted_steps=effective_weighted_steps,
        pool_24h_steps=pool_24h_steps,
        quota_headroom_steps=quota_headroom_steps,
        soft_over_limit=soft_over_limit,
        low_quota_tightened=low_quota_tightened,
        gemini_24h_steps=gemini_24h,
        third_party_24h_steps=third_party_24h,
        high_deg_capped=high_deg_capped,
        high_deg_ref_steps=high_deg_ref_steps,
    )
