from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config_policy import COMMENT_TIMEOUT_SECONDS


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
        # Filtering is an embedded policy, not a user-configurable switch.
        return cls(
            enabled=True, mention_mode="balanced", qrcode=True,
            ads=True, duplicates=True, low_information=False, ad_threshold=6,
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
                comments.get("enabled", comments.get(platform, legacy_default)),
                legacy_default,
            ),
            display_count=_clamp_int(
                comments.get("display_count", 10),
                10,
                1,
                20,
            ),
            timeout=COMMENT_TIMEOUT_SECONDS,
            filter=CommentFilterSettings.from_comments(comments),
        )


__all__ = ["CommentFilterSettings", "CommentSettings", "parse_bool"]
