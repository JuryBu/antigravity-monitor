"""Version metadata helpers for Antigravity Monitor."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Tuple

APP_VERSION = "7.1.0"
DEFAULT_UPDATE_REPO = "JuryBu/antigravity-monitor"
DEFAULT_UPDATE_BRANCH = "main"

_SEMVER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?")


def parse_version_tuple(version_str: str) -> Tuple[int, int, int]:
    """Parse a semantic version string such as '7.1.0' or 'v7.2' into a 3-tuple."""
    m = _SEMVER_RE.match(str(version_str or ""))
    if not m:
        return (0, 0, 0)
    return (
        int(m.group(1) or 0),
        int(m.group(2) or 0),
        int(m.group(3) or 0),
    )


def _version_sort_key(version_str: str) -> Tuple[int, int, int, int, Tuple[Tuple[int, Any], ...]]:
    """Return a SemVer comparison key where stable releases sort strictly above prereleases."""
    m = _SEMVER_RE.match(str(version_str or ""))
    if not m:
        return (0, 0, 0, 0, ())
    major = int(m.group(1) or 0)
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    pre = (m.group(4) or "").strip()
    if not pre:
        return (major, minor, patch, 1, ())
    pre_parts = []
    for part in pre.split("."):
        if part.isdigit():
            pre_parts.append((0, int(part)))
        else:
            pre_parts.append((1, part.lower()))
    return (major, minor, patch, 0, tuple(pre_parts))


def is_newer_version(remote_version: str, local_version: str) -> bool:
    """Return True strictly when remote_version > local_version (SemVer including prereleases)."""
    rem_core = parse_version_tuple(remote_version)
    if rem_core == (0, 0, 0):
        return False
    return _version_sort_key(remote_version) > _version_sort_key(local_version)


def read_local_version_info(app_root: Path | str) -> Dict[str, Any]:
    """Read local version.json with safe fallback to embedded APP_VERSION."""
    root = Path(app_root)
    info: Dict[str, Any] = {
        "version": APP_VERSION,
        "channel": DEFAULT_UPDATE_BRANCH,
        "released_at": "",
        "repo": DEFAULT_UPDATE_REPO,
        "branch": DEFAULT_UPDATE_BRANCH,
        "summary": "",
    }
    vf = root / "version.json"
    if not vf.is_file():
        return info
    try:
        raw = json.loads(vf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return info
    if isinstance(raw, dict):
        for key in ("version", "channel", "released_at", "repo", "branch", "summary"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                info[key] = val.strip()
    return info
