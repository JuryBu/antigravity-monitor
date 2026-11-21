"""System tray icon and native context menu powered by pystray.

All menu callbacks marshal onto the Tkinter main thread via `dispatch_to_ui`.
Menu rebuilds (`refresh_menu`) are triggered only on explicit state changes—never on
the 2.5s polling tick—to avoid Win32 DestroyMenu races while a popup menu is open.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Dict, Optional

import pystray
from PIL import Image

from ui.icon_builder import build_tray_icon
from ui.themes import THEMES


class TrayManager:
    """Manages a dedicated pystray thread with dynamic 3-state status icons."""

    def __init__(
        self,
        base_icon_png: Path,
        dispatch_to_ui: Callable[[Callable[[], None]], None],
        callbacks: Dict[str, Callable[[], None]],
        state_getters: Dict[str, Callable[[], object]],
    ) -> None:
        self._base_icon_png = Path(base_icon_png)
        self._dispatch = dispatch_to_ui
        self._callbacks = callbacks
        self._getters = state_getters

        self._icon_cache: Dict[str, Image.Image] = {}
        self._current_level: str = "green"
        self._current_tooltip: str = "反重力监控 · 启动中"
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()

    def _get_level_icon(self, level: str) -> Image.Image:
        img = self._icon_cache.get(level)
        if img is None:
            img = build_tray_icon(self._base_icon_png, level=level, size=64)
            self._icon_cache[level] = img
        return img

    def _invoke(self, key: str) -> Callable[[pystray.Icon, pystray.MenuItem], None]:
        def _handler(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
            cb = self._callbacks.get(key)
            if cb is not None:
                self._dispatch(cb)

        return _handler

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "显示 / 隐藏悬浮窗",
                self._invoke("toggle_visible"),
                default=True,
            ),
            pystray.MenuItem(
                "展开 / 收起体检详情",
                self._invoke("toggle_expand"),
            ),
            pystray.MenuItem(
                "界面主题",
                pystray.Menu(
                    pystray.MenuItem(
                        THEMES["midnight_blue"]["display_name"],
                        self._invoke("theme_midnight"),
                        checked=lambda _item: self._getters["theme"]() == "midnight_blue",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        THEMES["obsidian_dark"]["display_name"],
                        self._invoke("theme_obsidian"),
                        checked=lambda _item: self._getters["theme"]() == "obsidian_dark",
                        radio=True,
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "窗口置顶",
                self._invoke("toggle_topmost"),
                checked=lambda _item: bool(self._getters["topmost"]()),
            ),
            pystray.MenuItem(
                "开机自启动",
                self._invoke("toggle_startup"),
                checked=lambda _item: bool(self._getters["startup"]()),
            ),
            pystray.MenuItem(
                "启动时缩进托盘",
                self._invoke("toggle_start_minimized"),
                checked=lambda _item: bool(self._getters["start_minimized"]()),
            ),
            pystray.MenuItem(
                "静默通知",
                self._invoke("toggle_mute"),
                checked=lambda _item: not bool(self._getters["notifications"]()),
            ),
            pystray.MenuItem(
                "自动清理老旧子代理",
                self._invoke("toggle_auto_clean"),
                checked=lambda _item: bool(self._getters.get("auto_clean", lambda: True)()),
            ),
            pystray.MenuItem(
                "后台检查更新提示",
                self._invoke("toggle_update_check"),
                checked=lambda _item: bool(self._getters.get("update_check", lambda: True)()),
            ),
            pystray.MenuItem(
                "更新方式",
                pystray.Menu(
                    pystray.MenuItem(
                        "后台下载（下次启动生效）",
                        self._invoke("update_mode_download"),
                        checked=lambda _item: str(self._getters.get("update_mode", lambda: "download")()) == "download",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "自动下载并立即重启",
                        self._invoke("update_mode_auto_restart"),
                        checked=lambda _item: str(self._getters.get("update_mode", lambda: "download")()) == "auto_restart",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "仅提示（手动点击更新）",
                        self._invoke("update_mode_notify"),
                        checked=lambda _item: str(self._getters.get("update_mode", lambda: "download")()) == "notify",
                        radio=True,
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看本机 500 槽位详情", self._invoke("open_slots")),
            pystray.MenuItem(
                lambda _item: str(self._getters.get("clean_label", lambda: "立即清理 2h 外子代理")()),
                self._invoke("clean_subagents"),
            ),
            pystray.MenuItem(
                lambda _item: str(self._getters.get("update_label", lambda: "立即检查更新")()),
                self._invoke("check_or_apply_update"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即刷新采样", self._invoke("refresh_now")),
            pystray.MenuItem("复制文字体检摘要", self._invoke("copy_summary")),
            pystray.MenuItem("打开日志目录", self._invoke("open_logs")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重启监控", self._invoke("restart")),
            pystray.MenuItem("退出", self._invoke("quit")),
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._stopped.clear()
        self._icon = pystray.Icon(
            name="antigravity_monitor",
            icon=self._get_level_icon(self._current_level),
            title=self._current_tooltip,
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._run, name="TrayIconThread", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if self._icon is None:
            return

        def _setup(ic: pystray.Icon) -> None:
            ic.visible = True
            self._ready.set()

        try:
            self._icon.run(setup=_setup)
        except Exception:
            pass
        finally:
            self._ready.set()
            self._stopped.set()

    def update_status(self, level: str, tooltip: str) -> None:
        """Update tray icon badge & hover tooltip only; never rebuild active Win32 menu."""
        if self._icon is None or self._stopped.is_set() or not self._ready.is_set():
            return
        safe_tip = tooltip[:120]
        if level != self._current_level:
            self._current_level = level
            try:
                self._icon.icon = self._get_level_icon(level)
            except Exception:
                pass
        if safe_tip != self._current_tooltip:
            self._current_tooltip = safe_tip
            try:
                self._icon.title = safe_tip
            except Exception:
                pass

    def refresh_menu(self) -> None:
        """Refresh menu checkmarks after explicit user state toggles."""
        if self._icon is None or self._stopped.is_set() or not self._ready.is_set():
            return
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def notify(self, title: str, message: str) -> None:
        if self._icon is None or self._stopped.is_set() or not self._ready.is_set():
            return
        try:
            self._icon.notify(message[:220], title[:60])
        except Exception:
            pass

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            self._ready.wait(timeout=1.0)
            try:
                icon.visible = False
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
