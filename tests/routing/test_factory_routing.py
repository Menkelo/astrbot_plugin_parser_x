"""Offline factory routing regressions using the real platform decorators."""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.main import ParserXPlugin
from astrbot_plugin_parser_x.core.parsers import BaseParser


@pytest.fixture
def plugin():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {}
    plugin.parser_map = {}
    plugin.key_pattern_list = []
    for parser_cls in BaseParser.get_all_subclass():
        parser = SimpleNamespace(platform=parser_cls.platform)
        for keyword, pattern in parser_cls._key_patterns:
            plugin.parser_map[keyword] = parser
            plugin.key_pattern_list.append((keyword, re.compile(pattern)))
    plugin.key_pattern_list.sort(key=lambda item: -len(item[0]))
    return plugin


@pytest.mark.parametrize("url", [
    "https://notdouyin.com/video/1234567890123456789",
    "https://notweibo.com/123456/AbCdEf",
    "https://notkuaishou.com/short-video/abc123",
    "https://notv.douyin.com/abc123",
    "https://notxhslink.com/a/abc123",
    "https://notxiaohongshu.com/explore/abc123?xsec_token=token",
    "https://evil.example/douyin.com/video/1234567890123456789",
    "https://evil.example/?url=weibo.com/123456/AbCdEf",
    "notdouyin.com/video/1234567890123456789",
    "notweibo.com/123456/AbCdEf",
    "notkuaishou.com/short-video/abc123",
    "https://xiaohongshu.com.evil.example/explore/abc123",
    "https://evil.example/xiaohongshu.com/discovery/item/abc123",
    "https://www.xiaohongshu.com/?next=explore/abc123",
])
def test_factory_rejects_platform_substrings_in_unrelated_hosts(plugin, url):
    assert plugin._collect_parser_matches(url) == []


@pytest.mark.parametrize("url,platform", [
    ("https://www.douyin.com/video/1234567890123456789", "douyin"),
    ("douyin.com/video/1234567890123456789", "douyin"),
    ("https://v.douyin.com/abc123", "douyin"),
    ("https://m.weibo.cn/detail/AbCdEf", "weibo"),
    ("weibo.com/123456/AbCdEf", "weibo"),
    ("https://www.kuaishou.com/short-video/abc123", "kuaishou"),
    ("v.kuaishou.com/abc123", "kuaishou"),
    ("https://v.m.chenzhongtech.com/fw/photo/abc123", "kuaishou"),
    ("https://www.xiaohongshu.com/explore/abc123?xsec_token=token", "xiaohongshu"),
    ("xiaohongshu.com/discovery/item/abc123?xsec_token=token", "xiaohongshu"),
    ("https://xhslink.cn/a/abc123", "xiaohongshu"),
    ("BV1xx411c7mD", "bilibili"),
    ("av123456", "bilibili"),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
    ("b23.tv/abc123", "bilibili"),
])
def test_factory_preserves_real_hosts_and_identifier_routes(plugin, url, platform):
    matches = plugin._collect_parser_matches(url)
    assert len(matches) == 1
    assert matches[0][0].platform.name == platform


@pytest.mark.parametrize("host", ["xhslink.com", "xhslink.cn"])
def test_token_note_keeps_independent_unknown_short_links(plugin, host):
    short = f"https://{host}/a/unresolved"
    note = "https://www.xiaohongshu.com/explore/abc123?xsec_token=token"
    matches = plugin._collect_parser_matches(f"{short} {note}")
    assert [keyword for _, keyword, _ in matches] == [host, "hongshu.com/explore"]


def test_short_link_path_is_not_proof_of_note_identity(plugin):
    text = (
        "https://xhslink.com/a/explore/abc123 "
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=token"
    )
    matches = plugin._collect_parser_matches(text)
    assert [keyword for _, keyword, _ in matches] == [
        "xhslink.com", "hongshu.com/explore"
    ]


def test_same_known_note_id_prefers_token_without_removing_other_notes(plugin):
    text = (
        "https://www.xiaohongshu.com/explore/abc123 "
        "https://www.xiaohongshu.com/discovery/item/abc123?xsec_token=token "
        "https://www.xiaohongshu.com/explore/def456 "
        "https://xhslink.com/a/unresolved"
    )
    matches = plugin._collect_parser_matches(text)
    assert [searched.group(0) for _, _, searched in matches] == [
        "discovery/item/abc123?xsec_token=token",
        "explore/def456",
        "xhslink.com/a/unresolved",
    ]


def test_factory_preserves_order_overlap_dedup_and_regex_capture_groups(plugin):
    text = (
        "https://www.douyin.com/video/1234567890123456789 "
        "https://www.douyin.com/video/1234567890123456789 "
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=token "
        "https://weibo.com/123456/AbCdEf"
    )
    matches = plugin._collect_parser_matches(text)
    assert [parser.platform.name for parser, _, _ in matches] == [
        "douyin", "xiaohongshu", "weibo"
    ]
    assert matches[0][2].group("vid") == "1234567890123456789"
    assert matches[1][2].group("xhs_id") == "abc123"


def test_production_inbound_ignores_unsupported_adapter_before_reading_message(plugin):
    # No message fields: reading them would prove the existing runtime gate failed.
    asyncio.run(plugin.on_message(SimpleNamespace()))


def test_debug_parse_still_uses_fake_capture_event(plugin):
    from unittest.mock import AsyncMock
    from astrbot_plugin_parser_x.core.data import ParseResult
    from astrbot_plugin_parser_x.core.debug_page import DebugCaptureEvent, DebugMediaRegistry

    text = "https://www.douyin.com/video/1234567890123456789"
    parser = plugin._collect_parser_matches(text)[0][0]
    plugin._parse_with_cache = AsyncMock(return_value=ParseResult(platform=parser.platform))
    plugin._send_parse_result = AsyncMock()
    plugin.debug_media = DebugMediaRegistry()
    emit = AsyncMock()
    asyncio.run(plugin._run_debug_page_parse(text, owner="admin", emit=emit))
    capture = plugin._send_parse_result.await_args.args[0]
    assert isinstance(capture, DebugCaptureEvent)
    assert any(call.args[0].get("completed") == 1 for call in emit.await_args_list)
