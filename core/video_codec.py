"""Source-selection preferences only; never initiate encoding or probing."""
from collections.abc import Callable, Iterable, Mapping
from typing import TypeVar
import re

T = TypeVar("T")


def normalize_codec(value: object) -> str:
    raw = str(value or "").strip().lower()
    if re.search(r"(?:^|[^a-z0-9])(?:hevc|h[ ._-]?265|hev1|hvc1)(?:$|[^a-z0-9])", raw):
        return "hevc"
    if re.search(r"(?:^|[^a-z0-9])(?:avc|h[ ._-]?264|avc1)(?:$|[^a-z0-9])", raw):
        return "avc"
    return ""


def codec_preference(config: Mapping) -> str:
    perf = config.get("performance") or {}
    value = perf.get("video_codec", "auto") if isinstance(perf, Mapping) else "auto"
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("label") or "auto"
    return normalize_codec(value) or "auto"


def prefer_candidates(items: Iterable[T], preference: str, codec_getter: Callable[[T], object]) -> list[T]:
    candidates = list(items)
    target = normalize_codec(preference)
    if not target:
        return candidates
    preferred = [item for item in candidates if normalize_codec(codec_getter(item)) == target]
    return preferred or candidates


def ytdlp_video_format(preference: str) -> str:
    original = "best[height<=720]/bestvideo[height<=720]+bestaudio/best"
    target = normalize_codec(preference)
    if not target:
        return original
    codec = "^(avc|h264)" if target == "avc" else "^(hevc|h265|hev1|hvc1)"
    return (f"best[height<=720][vcodec~='{codec}']/"
            f"bestvideo[height<=720][vcodec~='{codec}']+bestaudio/" + original)

