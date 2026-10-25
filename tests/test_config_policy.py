import asyncio
import re
from copy import deepcopy
from unittest.mock import patch

import pytest

from core.comment_settings import CommentFilterSettings, CommentSettings
from core.config_policy import migrate_config
from core.data import ParseResult, Platform
from core.download import Downloader
from core.html_renderer import HtmlRenderService
from core.result_cache import CacheSettings, ParseResultCache


def test_migration_preserves_user_data_and_removes_retired_controls():
    cookies = {
        "bili_ck": "private",
        "custom_cookie": "do-not-touch",
        "ytdlp_cookie_file": ["cookies.txt"],
    }
    config = {
        "cookies": cookies,
        "comments": {
            "bilibili": False,
            "douyin": True,
            "display_count": 17,
            "timeout": 18,
            "filter": {"enabled": False, "ad_threshold": 3},
        },
        "performance": {
            "video_codec": "avc",
            "source_max_size": 900,
            "max_concurrent_downloads": 1,
        },
        "platforms": {"bilibili": False},
        "rendering": {"timeout": 180},
        "result_cache": {"enabled": False},
        "behavior": {"disabled_sessions": ["current"], "show_download_fail_tip": False},
        "disabled_sessions": ["legacy", "current"],
        "debug": {"enabled": True},
        "bili_comment": False,
        "comment_settings_migrated": True,
        "config_v2_migrated": True,
    }
    assert migrate_config(config) is True
    assert config["comments"] == {
        "enabled": True,
        "display_count": 17,
    }
    assert config["cookies"] is cookies
    assert cookies == {"bili_ck": "private", "custom_cookie": "do-not-touch"}
    assert config["performance"] == {"video_codec": "avc"}
    assert config["behavior"] == {
        "disabled_sessions": ["current", "legacy"],
        "response_enabled": True,
    }
    assert config["debug"]["enabled"] is True
    for key in (
        "platforms",
        "rendering",
        "result_cache",
        "disabled_sessions",
        "bili_comment",
        "comment_settings_migrated",
        "config_v2_migrated",
    ):
        assert key not in config
    snapshot = deepcopy(config)
    assert migrate_config(config) is False
    assert config == snapshot


@pytest.mark.parametrize(
    "comments,expected",
    [
        ({}, True),
        ({"bilibili": False, "douyin": False}, False),
        ({"bilibili": "false", "weibo": "true"}, True),
        ({"enabled": False, "douyin": True}, False),
    ],
)
def test_migration_unifies_comment_switch(comments, expected):
    config = {"comments": comments}
    migrate_config(config)
    assert config["comments"]["enabled"] is expected


def test_migration_legacy_fields_and_new_fields_have_clear_precedence():
    config = {
        "bili_comment": False,
        "disabled_sessions": ["old"],
    }
    migrate_config(config)
    assert config["comments"]["enabled"] is False
    assert config["behavior"] == {
        "disabled_sessions": ["old"],
        "response_enabled": True,
    }
    config = {
        "comments": {
            "enabled": False,
                "filter": {"enabled": True},
        }
    }
    migrate_config(config)
    assert "filter_enabled" not in config["comments"]


def test_unified_comments_ignore_old_platform_and_timeout_values():
    config = {
        "comments": {
            "enabled": False,
            "bilibili": True,
            "douyin": True,
            "timeout": 16,
            "display_count": 12,
        }
    }
    for platform in ("bilibili", "douyin", "weibo", "xiaohongshu", "xiaoheihe"):
        settings = CommentSettings.from_config(config, platform)
        assert settings.enabled is False
        assert settings.timeout == 90
        assert settings.display_count == 12
    assert (
        CommentSettings.from_config(
            {"comments": {"bilibili": False}}, "bilibili"
        ).enabled
        is False
    )


def test_new_filter_switch_uses_fixed_balanced_policy():
    settings = CommentFilterSettings.from_config(
        {
            "comments": {
                        "filter": {
                    "enabled": True,
                    "mention_mode": "strict",
                    "qrcode": False,
                    "ads": False,
                    "duplicates": False,
                    "low_information": True,
                    "ad_threshold": 3,
                },
            }
        }
    )
    assert settings == CommentFilterSettings(
        True, "balanced", True, True, True, False, 6
    )
    assert (
        CommentFilterSettings.from_config(
            {"comments": {"filter": {"mention_mode": "strict", "ad_threshold": 4}}}
        ).mention_mode
        == "balanced"
    )


def test_runtime_policy_ignores_stale_raw_values(tmp_path):
    config = {
        "cache_dir": str(tmp_path),
        "performance": {"source_max_size": 999, "max_concurrent_downloads": 1},
        "result_cache": {"enabled": False, "ttl": 15, "max_entries": 1},
        "rendering": {"timeout": 180, "jpeg_quality": 40},
    }
    assert CacheSettings.from_config(config) == CacheSettings(True, 300, 128)
    assert HtmlRenderService.from_config(config).timeout == 45
    assert HtmlRenderService.from_config(config).jpeg_quality == 84
    with patch("core.download.AsyncSession"):
        downloader = Downloader(config)
    assert downloader.default_max_size == 100
    assert downloader.sem._value == 9


def test_fixed_cache_policy_applies_to_real_expiration_and_eviction():
    class Parser:
        calls = 0

        async def parse(self, keyword, searched):
            self.calls += 1
            return ParseResult(Platform("demo", "Demo"), text=searched.group())

    async def run():
        now = [0]
        parser = Parser()
        cache = ParseResultCache(clock=lambda: now[0])
        config = {"result_cache": {"enabled": False, "ttl": 15, "max_entries": 1}}

        async def lookup(text):
            return await cache.parse(parser, "demo", re.search(r".+", text), text, config)

        await lookup("first")
        now[0] = 299
        await lookup("first")
        assert parser.calls == 1
        now[0] = 300
        await lookup("first")
        assert parser.calls == 2
        for index in range(128):
            await lookup(f"entry-{index}")
        assert len(cache._entries) == 128
        before = parser.calls
        await lookup("first")
        assert parser.calls == before + 1
        await cache.close()

    asyncio.run(run())


def test_session_management_remains_visible_and_filter_is_not_a_setting():
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    assert not schema["behavior"].get("invisible", False)
    assert not schema["behavior"]["items"]["disabled_sessions"].get("invisible", False)
    assert set(schema["comments"]["items"]) == {"enabled", "display_count"}
    assert CommentFilterSettings.from_comments({"filter_enabled": False}).enabled is True
