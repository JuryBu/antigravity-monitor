"""Atomic configuration load/save store for Antigravity Monitor."""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_CONFIG: Dict[str, Any] = {
    "theme": "midnight_blue",
    "poll_interval_ms": 2500,
    "freshness_window_sec": 210,
    "idle_rewarm_sec": 210,
    "large_context_threshold": 100000,
    "daily_steps_warn": 960,
    "daily_steps_alert": 1200,
    "log_sliding_window_min": 30,
    "slow_tps_threshold": 40.0,
    "slow_ttft_threshold_sec": 45.0,
    "topmost": True,
    "notifications_enabled": True,
    "start_minimized": False,
    "window_alpha": 0.95,
    "window_x": None,
    "window_y": None,
    "conv_filter": "main",
    "conv_list_rows": 5,
    "slot_cap": 500,
    "slot_warn_threshold": 380,
    "slot_alert_threshold": 450,
    "slot_auto_clean_enabled": True,
    "slot_protect_age_min": 120,
    "slot_auto_trigger_threshold": 420,
    "slot_clean_mode": "recycle",
    "slot_archive_dir": "",
    "update_check_enabled": True,
    "update_mode": "download",
    "auto_update_restart": False,
    "update_check_interval_min": 30,
    "update_ignored_version": "",
}

VALID_THEMES = ("midnight_blue", "obsidian_dark")
VALID_CONV_FILTERS = ("main", "subagent", "all")
VALID_SLOT_CLEAN_MODES = ("recycle", "archive", "delete")
VALID_UPDATE_MODES = ("download", "auto_restart", "notify")


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def sanitize_config(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    """Merge arbitrary config dict with defaults and validate value ranges."""
    cfg = deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    theme = raw.get("theme")
    if theme in VALID_THEMES:
        cfg["theme"] = theme

    cfg["poll_interval_ms"] = _clamp_int(raw.get("poll_interval_ms"), 2500, 1000, 30000)
    cfg["freshness_window_sec"] = _clamp_int(raw.get("freshness_window_sec"), 210, 60, 900)
    cfg["idle_rewarm_sec"] = _clamp_int(raw.get("idle_rewarm_sec"), 210, 60, 900)
    cfg["large_context_threshold"] = _clamp_int(raw.get("large_context_threshold"), 100000, 16000, 1000000)
    cfg["daily_steps_warn"] = _clamp_int(raw.get("daily_steps_warn"), 960, 50, 20000)
    cfg["daily_steps_alert"] = _clamp_int(raw.get("daily_steps_alert"), 1200, cfg["daily_steps_warn"] + 1, 50000)
    cfg["log_sliding_window_min"] = _clamp_int(raw.get("log_sliding_window_min"), 30, 5, 240)
    cfg["slow_tps_threshold"] = _clamp_float(raw.get("slow_tps_threshold"), 40.0, 5.0, 200.0)
    cfg["slow_ttft_threshold_sec"] = _clamp_float(raw.get("slow_ttft_threshold_sec"), 45.0, 10.0, 300.0)
    cfg["topmost"] = bool(raw.get("topmost", True))
    cfg["notifications_enabled"] = bool(raw.get("notifications_enabled", True))
    cfg["start_minimized"] = bool(raw.get("start_minimized", False))
    cfg["window_alpha"] = _clamp_float(raw.get("window_alpha"), 0.95, 0.72, 1.0)
    conv_filter = raw.get("conv_filter")
    if conv_filter in VALID_CONV_FILTERS:
        cfg["conv_filter"] = conv_filter
    cfg["conv_list_rows"] = _clamp_int(raw.get("conv_list_rows"), 5, 3, 12)

    cfg["slot_cap"] = _clamp_int(raw.get("slot_cap"), 500, 100, 5000)
    cfg["slot_warn_threshold"] = _clamp_int(raw.get("slot_warn_threshold"), 380, 50, cfg["slot_cap"] - 20)
    cfg["slot_alert_threshold"] = _clamp_int(raw.get("slot_alert_threshold"), 450, cfg["slot_warn_threshold"] + 1, cfg["slot_cap"])
    cfg["slot_auto_clean_enabled"] = bool(raw.get("slot_auto_clean_enabled", True))
    # Hard floor: NEVER allow automatic or manual cleanup age protection below 30 minutes
    cfg["slot_protect_age_min"] = _clamp_int(raw.get("slot_protect_age_min"), 120, 30, 10080)
    trig_high = max(50, cfg["slot_cap"] - 10)
    trig_low = min(trig_high, max(50, int(cfg["slot_cap"] * 0.5)))
    cfg["slot_auto_trigger_threshold"] = _clamp_int(raw.get("slot_auto_trigger_threshold"), min(420, trig_high), trig_low, trig_high)
    clean_mode = raw.get("slot_clean_mode")
    if clean_mode in VALID_SLOT_CLEAN_MODES:
        cfg["slot_clean_mode"] = clean_mode
    arch_dir = raw.get("slot_archive_dir")
    cfg["slot_archive_dir"] = str(arch_dir).strip() if isinstance(arch_dir, str) else ""

    cfg["update_check_enabled"] = bool(raw.get("update_check_enabled", True))
    raw_upd_mode = raw.get("update_mode")
    if isinstance(raw_upd_mode, str) and raw_upd_mode in VALID_UPDATE_MODES:
        cfg["update_mode"] = raw_upd_mode
        cfg["auto_update_restart"] = (raw_upd_mode == "auto_restart") and cfg["update_check_enabled"]
    else:
        legacy_auto = bool(raw.get("auto_update_restart", False))
        cfg["update_mode"] = "auto_restart" if legacy_auto else "download"
        cfg["auto_update_restart"] = legacy_auto and cfg["update_check_enabled"]
    if not cfg["update_check_enabled"] and cfg["update_mode"] == "auto_restart":
        cfg["auto_update_restart"] = False
    cfg["update_check_interval_min"] = _clamp_int(raw.get("update_check_interval_min"), 30, 10, 1440)
    ign_ver = raw.get("update_ignored_version")
    cfg["update_ignored_version"] = str(ign_ver).strip()[:32] if isinstance(ign_ver, str) else ""

    for coord in ("window_x", "window_y"):
        val = raw.get(coord)
        if val is None:
            cfg[coord] = None
        else:
            try:
                cfg[coord] = int(val)
            except (TypeError, ValueError):
                cfg[coord] = None
    return cfg


class ConfigStore:
    """Thread-safe persistent JSON configuration backed by atomic temp-file replacement."""

    def __init__(self, config_path: Path | str) -> None:
        self.path = Path(config_path)
        self._lock = threading.Lock()
        self._last_known_config: Optional[Dict[str, Any]] = None

    def _read_disk_locked(self) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Read raw JSON dict from disk under lock. Returns ``(data_or_none, read_failed)``."""
        if not self.path.is_file():
            return None, False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, True
        if not isinstance(data, dict):
            return None, True
        return data, False

    @staticmethod
    def _apply_patch_dict(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(base)
        for k, v in patch.items():
            merged[k] = deepcopy(v)
        # Keep update_mode and legacy auto_update_restart consistent when only one is patched
        if "auto_update_restart" in patch and "update_mode" not in patch:
            if bool(patch["auto_update_restart"]):
                merged["update_mode"] = "auto_restart"
            elif merged.get("update_mode") == "auto_restart":
                merged["update_mode"] = "download"
        elif "update_mode" in patch and "auto_update_restart" not in patch:
            upd_mode = patch.get("update_mode")
            if isinstance(upd_mode, str) and upd_mode in VALID_UPDATE_MODES:
                merged["auto_update_restart"] = (upd_mode == "auto_restart") and bool(
                    merged.get("update_check_enabled", True)
                )
        return merged

    def _write_atomic_locked(self, clean: Dict[str, Any]) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        payload = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            last_exc: Optional[OSError] = None
            for attempt in range(4):  # initial attempt + up to 3x15ms retries on transient Windows locks
                try:
                    os.replace(tmp_path, self.path)
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
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        self._last_known_config = deepcopy(clean)
        return deepcopy(clean)

    def load(self) -> Dict[str, Any]:
        with self._lock:
            data, failed = self._read_disk_locked()
            if data is not None:
                clean = sanitize_config(data)
                self._last_known_config = deepcopy(clean)
                return deepcopy(clean)
            if failed and self._last_known_config is not None:
                return deepcopy(self._last_known_config)
            return deepcopy(DEFAULT_CONFIG)

    def save_patch(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Re-read latest disk JSON under lock, overlay explicit patch keys, sanitize and replace atomically."""
        with self._lock:
            data, _failed = self._read_disk_locked()
            if data is not None:
                base = sanitize_config(data)
                self._last_known_config = deepcopy(base)
            elif self._last_known_config is not None:
                base = deepcopy(self._last_known_config)
            else:
                base = deepcopy(DEFAULT_CONFIG)
            merged = self._apply_patch_dict(base, patch) if isinstance(patch, dict) else base
            clean = sanitize_config(merged)
            return self._write_atomic_locked(clean)

    def save(self, config: Dict[str, Any], *, merge_disk: bool = False) -> Dict[str, Any]:
        """Save full config atomically; when ``merge_disk=True``, merge onto latest disk JSON first."""
        if merge_disk:
            return self.save_patch(config if isinstance(config, dict) else {})
        with self._lock:
            clean = sanitize_config(config)
            return self._write_atomic_locked(clean)
