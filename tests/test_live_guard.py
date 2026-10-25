"""直播链接在所有平台都必须被静默丢弃：不回复、不解析。"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_parser_x.core.exception import SkipParseException
from astrbot_plugin_parser_x.core.live_guard import (
    is_live_share_caption,
    is_live_url,
    is_short_link,
    looks_like_live_card,
    looks_like_live_share,
)
from astrbot_plugin_parser_x.core.parsers.base import BaseParser, handle


LIVE_URLS = [
    "https://live.bilibili.com/22603245",
    "https://live.bilibili.com/blanc/22603245",
    "https://live.douyin.com/70679141246",
    "https://webcast.douyin.com/some-room",
    "https://live.kuaishou.com/u/KPL666",
    "https://live.acfun.cn/live/123456",
    "https://live.weibo.com/show?id=1042152:abcdef",
    "https://weibo.com/l/wblive/p/show/1022:2321324abcdef",
    "https://www.xiaohongshu.com/livestream/1234567890",
    "https://live.iqiyi.com/123456",
]

POST_URLS = [
    "https://www.bilibili.com/video/BV1QHnq6LEqv",
    "https://v.douyin.com/aflk-s6Zx90/",
    "https://www.douyin.com/note/7699348604255371236",
    "https://www.kuaishou.com/short-video/3xabc",
    "https://weibo.com/1234567890/NabcdEFGH",
    "https://www.xiaohongshu.com/explore/66aabbcc",
    "https://www.xiaoheihe.cn/app/bbs/link/190978471",
    "https://www.acfun.cn/v/ac44332211",
]


@pytest.mark.parametrize("url", LIVE_URLS)
def test_live_room_urls_are_recognised(url):
    assert is_live_url(url) is True


@pytest.mark.parametrize("url", POST_URLS)
def test_post_urls_are_not_treated_as_live(url):
    assert is_live_url(url) is False


def test_empty_and_blank_urls_are_not_live():
    assert is_live_url("") is False
    assert is_live_url(None) is False
    assert is_live_url("   ") is False


@pytest.mark.parametrize(
    "text",
    [
        "【正在直播】快来围观",
        "XXX的直播间",
        "直接观看直播",
        "点击进入直播间",
        "https://example.com/livestream/1",
    ],
)
def test_live_share_captions_are_recognised(text):
    assert looks_like_live_share(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "【星穹铁道】新版本PV",
        "视频：每日一练",
        "https://www.bilibili.com/video/BV1QHnq6LEqv",
    ],
)
def test_ordinary_captions_are_not_live(text):
    assert looks_like_live_share(text) is False
    assert is_live_share_caption(text) is False


@pytest.mark.parametrize(
    "text",
    ["【正在直播】快来", "来看直播", "直接观看直播", "点击进入直播间"],
)
def test_strong_live_captions_drop_short_links(text):
    assert is_live_share_caption(text) is True


@pytest.mark.parametrize("text", ["直播间同款好物测评", "直播中可以买的零食"])
def test_weak_live_wording_does_not_drop_short_links(text):
    # 普通视频文案里出现「直播间」不能当成直播，短链仍要解析；
    # 跳转后如果是直播间，BaseParser 会再拦一次。
    assert is_live_share_caption(text) is False


class _LiveProbeParser(BaseParser):
    """最小解析器：handler 一旦被调用就说明入口拦截失效。"""

    platform = None
    calls: list[str] = []

    @handle("live.bilibili.com", r"https?://live\.bilibili\.com/\d+")
    async def _probe(self, searched):
        type(self).calls.append(searched.group(0))
        raise AssertionError("直播链接不应进入 handler")


# 探针类只用于验证入口拦截，不能留在全局解析器注册表里（否则 _register_parser 会遍历它）。
if _LiveProbeParser in BaseParser._registry:
    BaseParser._registry.remove(_LiveProbeParser)


def _make_parser(tmp_path) -> _LiveProbeParser:
    return _LiveProbeParser(config={"cache_dir": str(tmp_path)}, downloader=None)


def test_parse_rejects_live_urls_before_dispatch(tmp_path):
    parser = _make_parser(tmp_path)
    _LiveProbeParser.calls.clear()

    keyword, searched = parser.search_url("https://live.bilibili.com/22603245")
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse(keyword, searched))

    assert _LiveProbeParser.calls == []


def test_parse_with_redirect_rejects_live_landing(tmp_path):
    parser = _make_parser(tmp_path)
    _LiveProbeParser.calls.clear()

    async def fake_redirect(url, headers=None):
        return "https://live.bilibili.com/22603245"

    parser.get_redirect_url = fake_redirect

    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse_with_redirect("https://b23.tv/abc"))

    assert _LiveProbeParser.calls == []


def test_router_ignores_live_links_on_every_platform():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    for url in LIVE_URLS:
        for keyword in ("bilibili", "v.douyin", "kuaishou", "weibo", "xhs", "acfun"):
            assert plugin._should_ignore_live_share(keyword, url, url) is True


def test_router_ignores_live_url_when_pattern_drops_host_prefix():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    # 快手正则只截到 "kuaishou.com/u/..."，原文里仍是 live.kuaishou.com。
    assert (
        plugin._should_ignore_live_share(
            "kuaishou", "kuaishou.com/u/KPL666", "https://live.kuaishou.com/u/KPL666"
        )
        is True
    )


def test_router_keeps_ordinary_posts():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    for url in POST_URLS[:1]:
        assert plugin._should_ignore_live_share("bilibili", url, url) is False


def test_router_drops_short_link_shares_with_live_captions():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    assert (
        plugin._should_ignore_live_share("v.douyin", "https://v.douyin.com/abc/", "6.43 来看直播")
        is True
    )
    assert (
        plugin._should_ignore_live_share("b23.tv", "https://b23.tv/abc", "【正在直播】标题")
        is True
    )
    assert (
        plugin._should_ignore_live_share("v.douyin", "https://v.douyin.com/abc/", "视频标题")
        is False
    )
    assert (
        plugin._should_ignore_live_share(
            "v.douyin", "https://v.douyin.com/abc/", "直播间同款好物"
        )
        is False
    )


# ---------------------------------------------------------------- 分享卡片（小程序）

BILI_LIVE_CARD = {
    "app": "com.tencent.miniapp_01",
    "prompt": "[分享]哔哩哔哩",
    "meta": {
        "detail_1": {
            "title": "⚡直播中⚡镜的直播间",
            "desc": "哔哩哔哩（bilibili.com）",
            "qqdocurl": "https://live.bilibili.com/22603245?share_source=qq",
            "preview": "https://i0.hdslb.com/bfs/face/abc.jpg",
        }
    },
}

BILI_LIVE_SHORT_CARD = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "title": "镜正在直播",
            "qqdocurl": "https://b23.tv/1qfzDiw",
        }
    },
}

BILI_VIDEO_CARD = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "title": "【星穹铁道】新版本PV",
            "qqdocurl": "https://www.bilibili.com/video/BV1QHnq6LEqv",
        }
    },
}

# 标题里出现「直播间」但确实是普通视频
BILI_VIDEO_CARD_WITH_LIVE_WORDING = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "title": "直播间翻车名场面",
            "qqdocurl": "https://www.bilibili.com/video/BV1QHnq6LEqv",
        }
    },
}


def _card_chain(payload):
    from astrbot.core.message.components import Json

    return [Json(data=payload)]


def test_live_url_normalises_port_and_case():
    assert is_live_url("https://LIVE.Bilibili.com:443/22603245") is True
    assert is_live_url("https://live.bilibili.com./22603245") is True
    assert is_live_url("http://user:pw@live.douyin.com:80/123") is True


def test_short_link_detection():
    assert is_short_link("https://b23.tv/1qfzDiw") is True
    assert is_short_link("https://v.douyin.com/aflk-s6Zx90/") is True
    assert is_short_link("https://www.bilibili.com/video/BV1QHnq6LEqv") is False


def test_live_card_payload_detection():
    import json as _json

    assert looks_like_live_card(_json.dumps(BILI_LIVE_CARD, ensure_ascii=False)) is True
    assert looks_like_live_card(_json.dumps(BILI_VIDEO_CARD, ensure_ascii=False)) is False


def test_router_drops_live_share_cards_without_parsing():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    # 卡片里的地址能直接读出直播间时按链接判定，否则按卡片文案判定；
    # 两者都会让整条消息被丢弃。
    assert plugin._live_card_reason(_card_chain(BILI_LIVE_CARD)) in {
        "直播卡片链接",
        "直播分享卡片",
    }
    assert plugin._live_card_reason(_card_chain(BILI_LIVE_SHORT_CARD)) == "直播分享卡片"


def test_router_keeps_ordinary_video_cards():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)

    assert plugin._live_card_reason(_card_chain(BILI_VIDEO_CARD)) == ""
    # 标题里有「直播间」但链接是普通视频，不能误杀
    assert plugin._live_card_reason(_card_chain(BILI_VIDEO_CARD_WITH_LIVE_WORDING)) == ""
