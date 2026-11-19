"""Windows shortcut & Startup folder management (pure .lnk, zero registry edits)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

SHORTCUT_NAME = "反重力监控.lnk"
VBS_LAUNCHER_NAME = "启动反重力监控.vbs"
ICON_REL_PATH = Path("assets") / "app-icon.ico"


def get_desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def get_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def create_windows_shortcut(
    shortcut_path: Path,
    target_path: str,
    arguments: str,
    working_dir: Path,
    icon_path: Optional[Path] = None,
    description: str = "Antigravity 实时缓存与健康度桌面监控器",
) -> Path:
    """Create or update a .lnk shortcut via Windows Script Host COM object."""
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut({_ps_quote(str(shortcut_path))})",
        f"$sc.TargetPath = {_ps_quote(target_path)}",
        f"$sc.Arguments = {_ps_quote(arguments)}",
        f"$sc.WorkingDirectory = {_ps_quote(str(working_dir))}",
        f"$sc.Description = {_ps_quote(description)}",
    ]
    if icon_path and icon_path.exists():
        lines.append(f"$sc.IconLocation = {_ps_quote(str(icon_path) + ',0')}")
    lines.append("$sc.Save()")

    script = "; ".join(lines)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=creation_flags,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create shortcut {shortcut_path}: {res.stderr.strip()}")
    return shortcut_path


def install_desktop_shortcut(
    app_root: Path,
    desktop_dir: Optional[Path] = None,
    shortcut_name: str = SHORTCUT_NAME,
) -> Path:
    target_dir = desktop_dir or get_desktop_dir()
    vbs_path = app_root.resolve() / VBS_LAUNCHER_NAME
    icon_path = app_root.resolve() / ICON_REL_PATH
    wscript = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe")
    return create_windows_shortcut(
        shortcut_path=target_dir / shortcut_name,
        target_path=wscript,
        arguments=f'"{vbs_path}"',
        working_dir=app_root.resolve(),
        icon_path=icon_path,
    )


def is_startup_enabled(
    startup_dir: Optional[Path] = None,
    shortcut_name: str = SHORTCUT_NAME,
) -> bool:
    target_dir = startup_dir or get_startup_dir()
    return (target_dir / shortcut_name).is_file()


def set_startup_enabled(
    enabled: bool,
    app_root: Path,
    startup_dir: Optional[Path] = None,
    shortcut_name: str = SHORTCUT_NAME,
) -> bool:
    target_dir = startup_dir or get_startup_dir()
    lnk_path = target_dir / shortcut_name
    if enabled:
        vbs_path = app_root.resolve() / VBS_LAUNCHER_NAME
        icon_path = app_root.resolve() / ICON_REL_PATH
        wscript = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe")
        create_windows_shortcut(
            shortcut_path=lnk_path,
            target_path=wscript,
            arguments=f'"{vbs_path}"',
            working_dir=app_root.resolve(),
            icon_path=icon_path,
        )
        return True
    if lnk_path.exists():
        lnk_path.unlink()
    return False
