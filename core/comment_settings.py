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


def _parse_choice(value: Any, default: str, choices: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else default


@dataclass(frozen=True, slots=True)
class CommentFilterSettings:
    enabled: bool
    mention_mode: str
    qrcode: bool
    ads: bool
    duplicates: bool
    low_information: bool
    ad_threshold: int

    @classmethod
    def from_comments(cls, comments: dict[str, Any]) -> CommentFilterSettings:
        raw = comments.get("filter", {})
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            enabled=parse_bool(raw.get("enabled", True), True),
            mention_mode=_parse_choice(
                raw.get("mention_mode", "balanced"),
                "balanced",
                {"off", "balanced", "strict"},
            ),
            qrcode=parse_bool(raw.get("qrcode", True), True),
            ads=parse_bool(raw.get("ads", True), True),
            duplicates=parse_bool(raw.get("duplicates", True), True),
            low_information=parse_bool(raw.get("low_information", False), False),
            ad_threshold=_clamp_int(raw.get("ad_threshold", 4), 4, 3, 8),
        )

    @classmethod
    def from_config(cls, config: Any) -> CommentFilterSettings:
        comments = config.get("comments", {}) if hasattr(config, "get") else {}
        return cls.from_comments(comments if isinstance(comments, dict) else {})


@dataclass(frozen=True, slots=True)
class CommentSettings:
    enabled: bool
    display_count: int
    timeout: int
    filter: CommentFilterSettings

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
            timeout=_clamp_int(
                comments.get("timeout", 90),
                90,
                15,
                120,
            ),
            filter=CommentFilterSettings.from_comments(comments),
        )


__all__ = ["CommentFilterSettings", "CommentSettings", "parse_bool"]
