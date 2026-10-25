"""Fixed runtime policy and lossless migration to the compact settings UI."""

from __future__ import annotations

from collections.abc import MutableMapping
from copy import deepcopy
from typing import Any

MAX_CONCURRENT_DOWNLOADS = 9
SOURCE_MAX_SIZE_MB = 100
RENDER_TIMEOUT_SECONDS = 45
COMMENT_TIMEOUT_SECONDS = 90
RESULT_CACHE_ENABLED = True
RESULT_CACHE_TTL_SECONDS = 300
RESULT_CACHE_MAX_ENTRIES = 128
COMMENT_PLATFORMS = ("bilibili", "douyin", "weibo", "xiaohongshu", "xiaoheihe")


def migrate_config(config: MutableMapping[str, Any]) -> bool:
    """Migrate in place; preserve raw cookies and discard the retired upload key.

    Explicit compact settings win over legacy settings. Legacy disabled sessions
    are unioned with current sessions so an upgrade cannot re-enable a group.
    Unrelated internal configuration (including debug.enabled) is left intact.
    """
    # Imported lazily to keep policy constants usable by comment_settings.
    from .comment_settings import parse_bool

    before = deepcopy(dict(config))
    cookies = config.get("cookies")
    if isinstance(cookies, MutableMapping):
        cookies.pop("ytdlp_cookie_file", None)
    raw_comments = config.get("comments", {})
    comments = raw_comments if isinstance(raw_comments, dict) else {}
    if "enabled" in comments:
        enabled = parse_bool(comments["enabled"], True)
    else:
        legacy = [comments[key] for key in COMMENT_PLATFORMS if key in comments]
        if not legacy and "bili_comment" in config:
            legacy = [config["bili_comment"]]
        enabled = any(parse_bool(value, True) for value in legacy) if legacy else True
    config["comments"] = {
        "enabled": enabled,
        "display_count": comments.get("display_count", 10),
    }

    raw_performance = config.get("performance", {})
    performance = raw_performance if isinstance(raw_performance, dict) else {}
    config["performance"] = {"video_codec": performance.get("video_codec", "hevc")}

    raw_behavior = config.get("behavior", {})
    behavior = dict(raw_behavior) if isinstance(raw_behavior, dict) else {}
    sessions = behavior.get("disabled_sessions", [])
    sessions = list(sessions) if isinstance(sessions, list) else []
    legacy_sessions = config.get("disabled_sessions", [])
    if isinstance(legacy_sessions, list):
        for session in legacy_sessions:
            if session not in sessions:
                sessions.append(session)
    normalized = []
    for session in sessions:
        value = str(session).strip()
        parts = value.rsplit(":", 2)
        if len(parts) == 3 and parts[1] == "GroupMessage" and parts[2].isdigit():
            value = parts[2]
        if value and value not in normalized:
            normalized.append(value)
    behavior["disabled_sessions"] = normalized
    behavior["response_enabled"] = bool(behavior.get("response_enabled", True))
    behavior.pop("show_download_fail_tip", None)
    config["behavior"] = behavior
    for key in (
        "platforms",
        "rendering",
        "result_cache",
        "bili_comment",
        "comment_settings_migrated",
        "config_v2_migrated",
        "disabled_sessions",
        "show_download_fail_tip",
    ):
        config.pop(key, None)
    integrations = config.get("integrations")
    if isinstance(integrations, dict):
        integrations = dict(integrations)
        integrations.pop("tieba_api_base", None)
        if integrations:
            config["integrations"] = integrations
        else:
            config.pop("integrations", None)
    config["config_slim_migrated"] = True
    return dict(config) != before
