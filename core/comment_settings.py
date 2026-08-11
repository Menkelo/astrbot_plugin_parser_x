from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True, slots=True)
class CommentSettings:
    enabled: bool
    display_count: int
    chunk_size: int
    timeout: int

    @classmethod
    def from_config(
        cls,
        config,
        platform: str,
        *,
        legacy_enabled: Any = True,
    ) -> CommentSettings:
        comments = config.get("comments", {})
        if not isinstance(comments, dict):
            comments = {}
        legacy_default = parse_bool(legacy_enabled, True)
        return cls(
            enabled=parse_bool(
                comments.get(platform, legacy_default),
                legacy_default,
            ),
            display_count=_clamp_int(
                comments.get("display_count", 10),
                10,
                1,
                20,
            ),
            chunk_size=_clamp_int(
                comments.get("chunk_size", 5),
                5,
                1,
                10,
            ),
            timeout=_clamp_int(
                comments.get("timeout", 90),
                90,
                15,
                120,
            ),
        )


__all__ = ["CommentSettings", "parse_bool"]
