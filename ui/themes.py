"""Dual-theme palette definitions (Default: Scheme B Midnight Blue, Alt: Scheme A Obsidian Dark)."""

from __future__ import annotations

from typing import Dict

THEMES: Dict[str, Dict[str, str]] = {
    "midnight_blue": {
        "display_name": "午夜星蓝（方案 B）",
        "window_bg": "#111827",
        "panel_bg": "#151E30",
        "card_bg": "#1B263B",
        "card_alt_bg": "#212E46",
        "border": "#304364",
        "border_soft": "#243450",
        "text_primary": "#F1F5FB",
        "text_secondary": "#9FB2CC",
        "text_muted": "#6F83A0",
        "divider": "#2C3E5C",
        "btn_bg": "#1F2C44",
        "btn_hover": "#2A3C5C",
        "btn_active": "#32496E",
        "btn_text": "#DCE6F5",
        "green": "#34D399",
        "green_bg": "#12342C",
        "orange": "#FBBF24",
        "orange_bg": "#3A2C14",
        "red": "#FB7185",
        "red_bg": "#3E1B26",
        "cyan": "#60D5FA",
        "pink_accent": "#F48FB1",
        "bar_track": "#25334C",
        "bar_dim": "#33435E",
        "alert_banner_bg": "#382817",
        "alert_banner_border": "#B87A2A",
        "alert_banner_text": "#FCD38D",
    },
    "obsidian_dark": {
        "display_name": "深炭极简（方案 A）",
        "window_bg": "#121418",
        "panel_bg": "#16191F",
        "card_bg": "#1D2129",
        "card_alt_bg": "#232832",
        "border": "#2F3542",
        "border_soft": "#252A34",
        "text_primary": "#ECEFF4",
        "text_secondary": "#9AA4B2",
        "text_muted": "#6D7685",
        "divider": "#2A303B",
        "btn_bg": "#222731",
        "btn_hover": "#2D3442",
        "btn_active": "#384152",
        "btn_text": "#D8DEE9",
        "green": "#34D399",
        "green_bg": "#133127",
        "orange": "#F59E0B",
        "orange_bg": "#372711",
        "red": "#F87171",
        "red_bg": "#3B1C20",
        "cyan": "#38BDF8",
        "pink_accent": "#F472B6",
        "bar_track": "#282E3A",
        "bar_dim": "#363D4B",
        "alert_banner_bg": "#332514",
        "alert_banner_border": "#A16207",
        "alert_banner_text": "#FDE68A",
    },
}


def get_theme(name: str) -> Dict[str, str]:
    return THEMES.get(name, THEMES["midnight_blue"])
