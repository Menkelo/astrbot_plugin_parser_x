"""Retired links must stay inert, including when embedded in share cards."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_parser_x.main import ParserXPlugin

from core.parsers import BaseParser
from core.utils import extract_json_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.xiaoheihe.cn/app/bbs/link/123456789",
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123456789",
        "https://share.xiaoheihe.cn/app/bbs/link/123456789",
        "https://www.xiaoheihe.cn/games/detail/730",
        "https://www.miyoushe.com/ys/article/69857339",
        "https://m.miyoushe.com/sr/article/69857339",
    ],
)
@pytest.mark.parametrize("share_card", [False, True])
def test_retired_platform_links_have_no_matching_parser(url, share_card):
    plugin = object.__new__(ParserXPlugin)
    plugin.parser_map = {}
    plugin.key_pattern_list = []
    for parser in BaseParser.get_all_subclass():
        for keyword, pattern in parser._key_patterns:
            plugin.parser_map[keyword] = SimpleNamespace(platform=parser.platform)
            plugin.key_pattern_list.append((keyword, re.compile(pattern)))

    text = url
    if share_card:
        text = extract_json_url(json.dumps({"meta": {"detail_1": {"qqdocurl": url}}}))
        assert text == url
    assert plugin._collect_parser_matches(text) == []


def test_retired_platform_settings_are_absent_from_the_panel():
    root = Path(__file__).parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    for platform in ("xiaoheihe", "miyoushe"):
        assert platform not in schema["platforms"]["items"]
        assert platform not in schema["comments"]["items"]
    assert "xiaoheihe_cookie" not in schema["cookies"]["items"]
