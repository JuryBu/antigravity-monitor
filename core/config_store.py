"""Atomic configuration load/save store for Antigravity Monitor."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "theme": "midnight_blue",
    "poll_interval_ms": 2500,
    "freshness_window_sec": 210,
    "idle_rewarm_sec": 180,
    "large_context_threshold": 100000,
    "daily_steps_warn": 800,
    "daily_steps_alert": 1000,
    "log_sliding_window_min": 30,
    "slow_tps_threshold": 40.0,
    "slow_ttft_threshold_sec": 45.0,
    "topmost": True,
    "notifications_enabled": True,
    "start_minimized": False,
    "window_alpha": 0.95,
    "window_x": None,
    "window_y": None,
}

VALID_THEMES = ("midnight_blue", "obsidian_dark")


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
    cfg["idle_rewarm_sec"] = _clamp_int(raw.get("idle_rewarm_sec"), 180, 60, 900)
    cfg["large_context_threshold"] = _clamp_int(raw.get("large_context_threshold"), 100000, 16000, 1000000)
    cfg["daily_steps_warn"] = _clamp_int(raw.get("daily_steps_warn"), 800, 50, 20000)
    cfg["daily_steps_alert"] = _clamp_int(raw.get("daily_steps_alert"), 1000, cfg["daily_steps_warn"] + 1, 50000)
    cfg["log_sliding_window_min"] = _clamp_int(raw.get("log_sliding_window_min"), 30, 5, 240)
    cfg["slow_tps_threshold"] = _clamp_float(raw.get("slow_tps_threshold"), 40.0, 5.0, 200.0)
    cfg["slow_ttft_threshold_sec"] = _clamp_float(raw.get("slow_ttft_threshold_sec"), 45.0, 10.0, 300.0)
    cfg["topmost"] = bool(raw.get("topmost", True))
    cfg["notifications_enabled"] = bool(raw.get("notifications_enabled", True))
    cfg["start_minimized"] = bool(raw.get("start_minimized", False))
    cfg["window_alpha"] = _clamp_float(raw.get("window_alpha"), 0.95, 0.72, 1.0)

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
    """Persistent JSON configuration backed by atomic temp-file replacement."""

    def __init__(self, config_path: Path | str) -> None:
        self.path = Path(config_path)

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return deepcopy(DEFAULT_CONFIG)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return deepcopy(DEFAULT_CONFIG)
        return sanitize_config(data)

    def save(self, config: Dict[str, Any]) -> Dict[str, Any]:
        clean = sanitize_config(config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        payload = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        return clean
