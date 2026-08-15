from __future__ import annotations

import colorsys
import re
from pathlib import Path

from PIL import Image

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalise_hex_color(value: object, fallback: str) -> str:
    color = str(value or "").strip()
    if _HEX_COLOR_RE.fullmatch(color):
        return color.lower()
    return fallback.lower()


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, channel)):02x}" for channel in rgb)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.removeprefix("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for channel in rgb:
        value = channel / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_with_white(rgb: tuple[int, int, int]) -> float:
    return 1.05 / (_relative_luminance(rgb) + 0.05)


def _darken_for_white_text(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    adjusted = rgb
    for _ in range(12):
        if _contrast_with_white(adjusted) >= 4.5:
            break
        adjusted = tuple(round(channel * 0.88) for channel in adjusted)
    return adjusted


def mix_hex_color(color: str, target: str, amount: float) -> str:
    source_rgb = _hex_to_rgb(color)
    target_rgb = _hex_to_rgb(target)
    ratio = max(0.0, min(1.0, float(amount)))
    return _rgb_to_hex(
        tuple(
            round(source + (destination - source) * ratio)
            for source, destination in zip(source_rgb, target_rgb, strict=True)
        )
    )


def extract_dominant_color(path: Path, fallback: str | None = None) -> str | None:
    fallback_color = (
        normalise_hex_color(fallback, "#536579") if fallback is not None else None
    )
    try:
        with Image.open(path) as source:
            source.seek(0)
            image = source.convert("RGBA")
            image.thumbnail((72, 72), Image.Resampling.LANCZOS)
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            quantized = background.convert("RGB").quantize(
                colors=10,
                method=Image.Quantize.MEDIANCUT,
            )
            palette = quantized.getpalette() or []
            colors = quantized.getcolors(maxcolors=256) or []
    except Exception:
        return fallback_color

    candidates: list[tuple[float, tuple[int, int, int]]] = []
    neutral_candidates: list[tuple[int, tuple[int, int, int]]] = []
    for count, palette_index in colors:
        offset = int(palette_index) * 3
        if offset + 2 >= len(palette):
            continue
        rgb = tuple(palette[offset : offset + 3])
        red, green, blue = (channel / 255 for channel in rgb)
        _, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
        if value >= 0.96 or value <= 0.05:
            continue
        neutral_candidates.append((count, rgb))
        if saturation < 0.10:
            continue
        brightness_weight = 1.0 - abs(value - 0.55) * 0.35
        score = count * (0.72 + saturation * 0.28) * brightness_weight
        candidates.append((score, rgb))

    if candidates:
        dominant = max(candidates, key=lambda item: item[0])[1]
    elif neutral_candidates:
        dominant = max(neutral_candidates, key=lambda item: item[0])[1]
    else:
        return fallback_color

    return _rgb_to_hex(_darken_for_white_text(dominant))


__all__ = [
    "extract_dominant_color",
    "mix_hex_color",
    "normalise_hex_color",
]
