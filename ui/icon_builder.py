"""High-DPI app icon processing and dynamic 3-state system tray icon synthesis."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import threading
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw

STATUS_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    "green": (52, 211, 153, 255),
    "orange": (251, 191, 36, 255),
    "red": (251, 113, 133, 255),
}

_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
_BASE_CANVAS_CACHE_MAX = 8
_BASE_CANVAS_CACHE: "OrderedDict[Tuple[str, int, int, int], Image.Image]" = OrderedDict()
_BASE_CANVAS_LOCK = threading.Lock()


def _get_cached_base_canvas(base_png_path: Optional[Path], hi_size: int) -> Optional[Image.Image]:
    """Return a copy of the cached RGBA base canvas (keyed by path, mtime_ns, file size, target size)."""
    if not base_png_path:
        return None
    try:
        st = base_png_path.stat()
    except OSError:
        return None
    if not base_png_path.is_file():
        return None
    cache_key = (str(base_png_path), int(st.st_mtime_ns), int(st.st_size), int(hi_size))
    with _BASE_CANVAS_LOCK:
        cached = _BASE_CANVAS_CACHE.get(cache_key)
        if cached is not None:
            _BASE_CANVAS_CACHE.move_to_end(cache_key)
            return cached.copy()
    try:
        with Image.open(base_png_path) as raw_img:
            base = raw_img.convert("RGBA")
        inset = int(hi_size * 0.06)
        inner = hi_size - inset * 2
        base_resized = base.resize((inner, inner), _LANCZOS)
        base_canvas = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
        base_canvas.alpha_composite(base_resized, (inset, inset))
    except OSError:
        return None
    with _BASE_CANVAS_LOCK:
        _BASE_CANVAS_CACHE[cache_key] = base_canvas
        _BASE_CANVAS_CACHE.move_to_end(cache_key)
        while len(_BASE_CANVAS_CACHE) > _BASE_CANVAS_CACHE_MAX:
            _BASE_CANVAS_CACHE.popitem(last=False)
        return base_canvas.copy()


def build_tray_icon(base_png_path: Optional[Path], level: str = "green", size: int = 64) -> Image.Image:
    """Create a crisp 64x64 RGBA tray icon with a live colored status ring and badge dot."""
    color = STATUS_COLORS.get(level, STATUS_COLORS["green"])
    hi_size = size * 4
    canvas = _get_cached_base_canvas(base_png_path, hi_size)
    if canvas is None:
        canvas = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
        _draw_fallback_emblem(canvas, hi_size)

    draw = ImageDraw.Draw(canvas)
    # Refined outer colored status rim around squircle (keeps Yoko art unobstructed)
    pad = int(hi_size * 0.025)
    radius = int(hi_size * 0.22)
    draw.rounded_rectangle(
        (pad, pad, hi_size - pad, hi_size - pad),
        radius=radius,
        outline=color,
        width=max(5, hi_size // 34),
    )

    # Compact bottom-right high-contrast status dot (inset 0.055 so outer dark ring never clips)
    dot_r = int(hi_size * 0.14)
    cx = hi_size - dot_r - int(hi_size * 0.055)
    cy = hi_size - dot_r - int(hi_size * 0.055)
    ring_r = dot_r + int(hi_size * 0.04)
    draw.ellipse((cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r), fill=(15, 22, 36, 245))
    draw.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=color)

    return canvas.resize((size, size), _LANCZOS)


def _draw_fallback_emblem(canvas: Image.Image, hi_size: int) -> None:
    draw = ImageDraw.Draw(canvas)
    pad = int(hi_size * 0.08)
    draw.rounded_rectangle(
        (pad, pad, hi_size - pad, hi_size - pad),
        radius=int(hi_size * 0.22),
        fill=(21, 30, 48, 255),
    )
    cx, cy = hi_size // 2, hi_size // 2
    r = int(hi_size * 0.28)
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=30, end=330, fill=(96, 213, 250, 255), width=max(10, hi_size // 20))
