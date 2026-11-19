"""Cross-user helper script to install or uninstall Desktop & Startup shortcuts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.startup_manager import (
    SHORTCUT_NAME,
    get_desktop_dir,
    install_desktop_shortcut,
    is_startup_enabled,
    set_startup_enabled,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/uninstall Antigravity Monitor Windows shortcuts.")
    parser.add_argument("--enable-startup", action="store_true", help="Also enable Startup folder auto-launch")
    parser.add_argument("--disable-startup", action="store_true", help="Remove Startup folder shortcut")
    parser.add_argument("--remove-desktop", action="store_true", help="Remove Desktop shortcut")
    args = parser.parse_args()

    if args.remove_desktop:
        p = get_desktop_dir() / SHORTCUT_NAME
        if p.exists():
            p.unlink()
            print(f"Removed desktop shortcut: {p}")
    else:
        p = install_desktop_shortcut(APP_ROOT)
        print(f"Installed desktop shortcut: {p}")

    if args.enable_startup:
        set_startup_enabled(True, APP_ROOT)
    elif args.disable_startup:
        set_startup_enabled(False, APP_ROOT)

    print(f"Startup enabled: {is_startup_enabled()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
