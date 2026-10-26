"""New platform shares must survive the common extraction entry point."""
import pytest
from astrbot_plugin_parser_x.core.utils import pick_supported_share_url
from astrbot_plugin_parser_x.core.card_theme import resolve_platform_glyph

@pytest.mark.parametrize('url', [
    'https://tieba.baidu.com/p/123456789',
    'https://mp.weixin.qq.com/s/exampleArticleToken',
    'https://music.163.com/song?id=123456',
    'https://bbs.nga.cn/read.php?tid=47697417',
    'https://www.zhihu.com/question/123456/answer/456789',
    'https://www.coolapk.com/feed/123456?shareKey=abc&s=def',
])
def test_new_platform_share_extraction(url):
    assert pick_supported_share_url([url]) == url

@pytest.mark.parametrize('url', [
    'https://tieba.baidu.com.evil.test/p/123456789',
    'https://mp.weixin.qq.com.evil.test/s/exampleArticleToken',
    'https://notmusic.163.com.evil.test/song?id=123456',
    'https://bbs.nga.cn.evil.test/read.php?tid=47697417',
    'https://www.zhihu.com.evil.test/question/123456/answer/456789',
    'https://www.coolapk.com.evil.test/feed/123456',
])
def test_new_platform_share_rejects_false_hosts(url):
    assert pick_supported_share_url([url]) is None

@pytest.mark.parametrize('key,name,glyph',[
    ('tieba','贴吧','贴'), ('wechat_article','微信公众号','微'),
    ('netease','网易云音乐','云'), ('nga','NGA','N'),
    ('zhihu','知乎','知'), ('coolapk','酷安','酷'),
])
def test_new_platform_placeholder(key, name, glyph):
    assert resolve_platform_glyph(key, name) == glyph


def test_new_platforms_are_registered_for_common_dispatch():
    from astrbot_plugin_parser_x.core.parsers import BaseParser
    names = {cls.platform.name for cls in BaseParser.get_all_subclass()}
    assert {'tieba', 'wechat_article', 'netease', 'nga', 'zhihu', 'coolapk'} <= names


@pytest.mark.parametrize('platform,url', [
    ('tieba', 'https://tieba.baidu.com/p/123456789'),
    ('wechat_article', 'https://mp.weixin.qq.com/s/exampleArticleToken'),
    ('netease', 'https://music.163.com/song?id=1372188635'),
    ('nga', 'https://bbs.nga.cn/read.php?tid=47697417'),
    ('zhihu', 'https://www.zhihu.com/question/123456/answer/456789'),
    ('coolapk', 'https://www.coolapk.com/feed/66072143?shareKey=abc&s=def'),
])
def test_new_platforms_survive_actual_dispatch_boundary(platform, url):
    import re
    from types import SimpleNamespace
    from urllib.parse import urlsplit, urlunsplit
    from astrbot_plugin_parser_x.main import ParserXPlugin
    from astrbot_plugin_parser_x.core.parsers import BaseParser

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {}
    plugin.parser_map = {}
    plugin.key_pattern_list = []
    for cls in BaseParser.get_all_subclass():
        parser = SimpleNamespace(platform=cls.platform)
        for keyword, pattern in cls._key_patterns:
            plugin.parser_map[keyword] = parser
            plugin.key_pattern_list.append((keyword, re.compile(pattern)))
    plugin.key_pattern_list.sort(key=lambda item: -len(item[0]))

    matches = plugin._collect_parser_matches('分享作品：' + url)
    assert len(matches) == 1
    assert matches[0][0].platform.name == platform
    parsed = urlsplit(url)
    suffix_trick = urlunsplit(parsed._replace(netloc=parsed.netloc + '.evil.example'))
    assert plugin._collect_parser_matches(suffix_trick) == []
    assert plugin._collect_parser_matches('https://evil.example/redirect?next=' + url) == []

