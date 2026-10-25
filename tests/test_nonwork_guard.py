"""Known navigation pages are not works and must not trigger parsing."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.exception import ParseException, SkipParseException
from astrbot_plugin_parser_x.core.nonwork_guard import is_nonwork_url
from astrbot_plugin_parser_x.core.parsers.base import BaseParser, handle


NONWORK_URLS = [
    "https://www.bilibili.com/", "https://space.bilibili.com/123",
    "https://www.bilibili.com/opus", "https://www.bilibili.com/read/home",
    "https://www.douyin.com/", "https://www.douyin.com/user/abc",
    "https://www.douyin.com/jingxuan", "https://www.douyin.com/search/cat",
    "https://www.iesdouyin.com/share/user/123", "https://m.douyin.com/share/user/123",
    "https://www.kuaishou.com/", "https://www.kuaishou.com/u/abc",
    "https://www.kuaishou.com/new-reco", "https://www.kuaishou.com/search/video",
    "https://www.kuaishou.com/profile/abc", "https://www.kuaishou.com/follow",
    "https://m.gifshow.com/", "https://m.gifshow.com/profile/abc",
    "https://chenzhongtech.com/search/video", "https://v.m.chenzhongtech.com/new-reco",
    "https://m.chenzhongtech.com/fw/user/abc", "https://www.chenzhongtech.com/fw/search",
    "https://weibo.com/", "https://weibo.com/u/123", "https://weibo.com/123",
    "https://weibo.com/123/follow", "https://m.weibo.cn/123/fans",
    "https://weibo.com/home", "https://weibo.com/p/100505123456",
    "https://www.xiaohongshu.com/", "https://www.xiaohongshu.com/explore",
    "https://www.xiaohongshu.com/user/profile/abc", "https://www.xiaohongshu.com/search_result?keyword=cat",
    "https://www.xiaoheihe.cn/", "https://www.xiaoheihe.cn/app/user/123",
    "https://www.acfun.cn/", "https://www.acfun.cn/u/123", "https://www.acfun.cn/search?keyword=cat",
]

WORK_URLS = [
    "https://www.bilibili.com/video/BV1QHnq6LEqv", "https://www.bilibili.com/opus/123",
    "https://www.bilibili.com/read/cv123", "https://t.bilibili.com/123",
    "https://www.douyin.com/video/123", "https://www.douyin.com/note/123",
    "https://www.douyin.com/?modal_id=123", "https://www.douyin.com/user/abc?modal_id=123",
    "https://www.douyin.com/jingxuan?modal_id=123", "https://www.douyin.com/search/cat?modal_id=123",
    "https://www.kuaishou.com/short-video/3abc", "https://www.kuaishou.com/?photoId=3abc",
    "https://www.kuaishou.com/fw/photo/3abc", "https://v.m.chenzhongtech.com/fw/long-video/3abc",
    "https://m.gifshow.com/fw/photo/3abc", "https://chenzhongtech.com/fw/photo/3abc",
    "https://m.gifshow.com/?photoId=3abc", "https://v.m.chenzhongtech.com/?photo_id=3abc",
    "https://m.gifshow.com/token", "https://v.m.chenzhongtech.com/fw/token",
    "https://weibo.com/123/NabcdEFGH", "https://weibo.com/u/123/NabcdEFGH",
    "https://weibo.com/status/123", "https://m.weibo.cn/detail/123",
    "https://weibo.com/tv/show/1034:123", "https://video.weibo.com/show?fid=1034:123",
    "https://weibo.com/p/230444123456",
    "https://www.xiaohongshu.com/explore/66aabbcc", "https://www.xiaohongshu.com/discovery/item/66aabbcc",
    "https://www.xiaoheihe.cn/app/bbs/link/123", "https://www.acfun.cn/v/ac123",
    "https://v.douyin.com/token/", "https://b23.tv/token", "https://xhslink.com/token",
    "https://v.kuaishou.com/token", "https://mapp.api.weibo.cn/fx/token.html",
]


@pytest.mark.parametrize("url", NONWORK_URLS)
def test_known_nonwork_routes(url):
    assert is_nonwork_url(url)


@pytest.mark.parametrize("url", WORK_URLS)
def test_works_and_unknown_shortlinks_are_preserved(url):
    assert not is_nonwork_url(url)


@pytest.mark.parametrize("url", [None, "", "   ", "explore/abc", "BV1QHnq6LEqv",
    "https://example.com/profile/123", "https://kuaishou.com.evil.test/search",
    "https://weibo.com/123/followingBid", "https://www.douyin.com/video/123?next=/user/abc",
    "https://www.kuaishou.com/new-recommendation"])
def test_guard_uses_exact_host_path_and_query(url):
    assert not is_nonwork_url(url)


@pytest.mark.parametrize("key", ["photoId", "photo_id"])
@pytest.mark.parametrize("identity", ["", "%20", "bad%20id", "bad%2Fid", "%E4%B8%AD"])
def test_kuaishou_invalid_photo_identity_does_not_override_navigation(key, identity):
    assert is_nonwork_url(f"https://www.kuaishou.com/new-reco?{key}={identity}")


@pytest.mark.parametrize("key", ["photoId", "photo_id"])
@pytest.mark.parametrize("identity", ["3abc", "123456", "3abc_DEF-123"])
def test_kuaishou_valid_photo_identity_overrides_navigation(key, identity):
    assert not is_nonwork_url(f"https://www.kuaishou.com/new-reco?{key}={identity}")


def test_guard_normalises_host_without_changing_path_case():
    assert is_nonwork_url("HTTPS://WWW.KUAISHOU.COM.:443/search/video")
    assert not is_nonwork_url("https://www.kuaishou.com/SEARCH/video")

class _NonworkProbeParser(BaseParser):
    platform = None

    @handle("kuaishou", r"(?:www\.)?kuaishou\.com/[^\s]+")
    async def _probe(self, searched):
        raise AssertionError("nonwork URL reached handler")


BaseParser._registry.remove(_NonworkProbeParser)


@pytest.mark.parametrize("url", NONWORK_URLS)
def test_base_parse_skips_nonwork_even_with_unknown_keyword(tmp_path, url):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse("unused", re.search(r".+", url)))
    assert parser._session is None


@pytest.mark.parametrize("method", ["get_redirect_url", "get_final_url"])
def test_redirect_helpers_skip_nonwork_entries_without_network(tmp_path, method):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)

    async def unexpected(*args, **kwargs):
        raise AssertionError("nonwork entry requested network")

    parser.http_get = unexpected
    with pytest.raises(SkipParseException):
        asyncio.run(getattr(parser, method)("https://www.kuaishou.com/profile/abc"))


def test_parse_skips_known_navigation_before_handler(tmp_path):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)
    keyword, match = parser.search_url("https://www.kuaishou.com/search/video?searchKey=cat")
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse(keyword, match))
    assert parser._session is None


@pytest.mark.parametrize("url", NONWORK_URLS)
def test_redirect_parse_skips_known_nonwork_entry_without_network(tmp_path, url):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)

    async def unexpected(*args, **kwargs):
        raise AssertionError("nonwork entry requested network")

    parser.get_redirect_url = unexpected
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse_with_redirect(url))


@pytest.mark.parametrize("landing", NONWORK_URLS)
def test_redirect_parse_skips_nonwork_landing_before_search(tmp_path, landing):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)

    async def resolve(*args, **kwargs):
        return landing

    parser.get_redirect_url = resolve
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse_with_redirect("https://b23.tv/token"))


@pytest.mark.parametrize("method", ["get_redirect_url", "get_final_url"])
def test_redirect_helpers_preserve_skip_instead_of_wrapping_or_swallowing(tmp_path, method):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)

    async def resolved(*args, **kwargs):
        return SimpleNamespace(status_code=302, headers={"Location": "https://www.xiaohongshu.com/user/profile/abc"}, url="https://www.xiaohongshu.com/user/profile/abc")

    parser.http_get = resolved
    with pytest.raises(SkipParseException):
        asyncio.run(getattr(parser, method)("https://xhslink.com/token"))


def test_router_collects_no_actual_matches_for_navigation():
    from astrbot_plugin_parser_x.core.parsers import (
        AcFunParser, BilibiliParser, DouyinParser, KuaiShouParser,
        WeiboParser, XiaoHeiHeParser, XiaoHongShuParser,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    classes = (AcFunParser, BilibiliParser, DouyinParser, KuaiShouParser,
               WeiboParser, XiaoHeiHeParser, XiaoHongShuParser)
    plugin.key_pattern_list = [item for cls in classes for item in cls._key_patterns]
    plugin.parser_map = {key: cls for cls in classes for key, _ in cls._key_patterns}
    for url in (
        "https://www.kuaishou.com/new-reco",
        "https://www.kuaishou.com/search/video?searchKey=cat",
        "https://www.kuaishou.com/profile/abc",
        "https://www.kuaishou.com/follow",
        "https://weibo.com/1234567890/follow",
        "https://m.weibo.cn/1234567890/fans",
    ):
        assert plugin._collect_parser_matches(url) == [], url
    for url in NONWORK_URLS:
        assert plugin._collect_parser_matches(url) == [], url
    for url in (
        "https://weibo.com/1234567890/NabcdEFGH",
        "https://www.acfun.cn/v/ac123",
        "https://www.xiaohongshu.com/explore/66aabbcc",
        "https://www.douyin.com/jingxuan?modal_id=123",
        "https://www.kuaishou.com/short-video/3abc",
    ):
        assert plugin._collect_parser_matches(url), url


@pytest.mark.parametrize("method", ["get_redirect_url", "get_final_url"])
def test_unknown_redirect_network_failure_keeps_original_semantics(tmp_path, method):
    parser = _NonworkProbeParser({"cache_dir": str(tmp_path)}, None)

    async def failed(*args, **kwargs):
        raise RuntimeError("offline probe")

    parser.http_get = failed
    url = "https://xhslink.com/token"
    if method == "get_final_url":
        assert asyncio.run(parser.get_final_url(url)) == url
    else:
        with pytest.raises(ParseException, match="重定向检测失败: offline probe"):
            asyncio.run(parser.get_redirect_url(url))
