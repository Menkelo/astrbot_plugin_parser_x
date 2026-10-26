"""Offline public article contracts; no online credentials or live messages."""
from pathlib import Path
import pytest


@pytest.mark.asyncio
async def test_wechat_three_mib_article_stream_is_accepted(tmp_path, monkeypatch):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    import aiohttp
    class Content:
        async def iter_chunked(self, size):
            yield b'<script>'
            for _ in range(48):
                yield b' ' * 65536
            yield b'</script><div id="js_content">complete public body</div>'
    class Response:
        status = 200
        headers = {'Content-Type': 'text/html', 'Content-Length': '100'}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    keyword, match = parser.search_url('https://mp.weixin.qq.com/s/abc')
    result = await parser.parse(keyword, match)
    assert result.text == 'complete public body'


@pytest.mark.asyncio
@pytest.mark.parametrize('platform,limit', [('coolapk', 2), ('wechat', 4)])
async def test_platform_stream_cap_stops_before_next_chunk(tmp_path, monkeypatch, platform, limit):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    import aiohttp
    class Content:
        async def iter_chunked(self, size):
            for _ in range(limit * 16): yield b' ' * 65536
            yield b'x'
            pytest.fail('stream consumed after size exceeded')
    class Response:
        status = 200
        headers = {'Content-Type': 'text/html', 'Content-Length': '100'}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    cls, url = (CoolapkParser, 'https://www.coolapk.com/feed/123') if platform == 'coolapk' else (WechatArticleParser, 'https://mp.weixin.qq.com/s/abc')
    parser = cls({'cache_dir': str(tmp_path)}, None)
    keyword, match = parser.search_url(url)
    with pytest.raises(ParseException, match='超过大小上限'):
        await parser.parse(keyword, match)


def test_wechat_decimal_literal_coercion_preserves_article(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser, _LiteralReader
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    result = parser._parse_html('''<script>window.cgiDataNew={item_show_type:'0'*1,bottom_right_brightness:'0.13536859'*1};</script><div id="js_content">complete body</div>''', 'https://mp.weixin.qq.com/s/abc')
    assert result.text == 'complete body'
    assert _LiteralReader("'0.13536859'*1").value() == 0.13536859


@pytest.mark.parametrize('expression', ["'NaN'*1", "'Infinity'*1", "'1e309'*1", "'x'*1", "'" + '1' * 65 + "'*1", "'0.1'*2", "Number('0.1')", "'0.1'+1", "'1+1'*1"])
def test_wechat_rejects_nonliteral_numeric_coercion(tmp_path, expression):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException, match='格式不受支持'):
        parser._parse_html('<script>window.cgiDataNew={item_show_type:0,brightness:' + expression + '};</script><div id="js_content">body</div>', 'https://mp.weixin.qq.com/s/abc')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers,chunks', [
    (403, {'Content-Type': 'text/html'}, []),
    (200, {'Content-Type': 'application/octet-stream'}, []),
    (200, {'Content-Type': 'text/html', 'Content-Length': '2097153'}, []),
    (200, {'Content-Type': 'text/html'}, [b'a' * 1048576, b'b' * 1048577]),
    (302, {'Location': '/feed/123'}, []),
])
async def test_fetch_limits_are_explicit_failures(tmp_path, monkeypatch, status, headers, chunks):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser, _fetch_public
    from astrbot_plugin_parser_x.core.exception import ParseException
    import aiohttp
    class Content:
        async def iter_chunked(self, size):
            for chunk in chunks: yield chunk
    class Response:
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    Response.status, Response.headers = status, headers
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    parser = CoolapkParser({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException):
        await _fetch_public(parser, 'https://www.coolapk.com/feed/123', {'www.coolapk.com'}, r'/feed/\d+')


@pytest.mark.parametrize('value', ['http://127.0.0.1/x', 'https://image.coolapk.com.evil/x', 'https://evil@image.coolapk.com/x', 'https://image.coolapk.com:999/x', 'data:image/png;base64,x', 'file:///tmp/x'])
def test_image_host_boundary(value):
    from astrbot_plugin_parser_x.core.parsers.coolapk import _image_url
    assert _image_url(value, {'image.coolapk.com'}) is None


def test_wechat_javascript_is_not_executed(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException):
        parser._parse_html('<script>window.cgiDataNew={item_show_type:8,title:__import__("os").system("false")};</script>', 'https://mp.weixin.qq.com/s/a')


def test_extremely_nested_markup_fails_explicitly(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    parser = CoolapkParser({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException):
        parser._parse_html('<div>' * 1500 + 'body' + '</div>' * 1500, 'https://www.coolapk.com/feed/123')


@pytest.mark.parametrize('url', ['https://www.coolapk.com/feed/123evil', 'https://www.coolapk.com/feed/123/other', 'https://www.coolapk.com.evil/feed/123'])
def test_coolapk_rejects_nonwork_path_suffix(tmp_path, url):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    with pytest.raises(ParseException):
        CoolapkParser.search_url(url)


def test_wechat_rejects_declared_video_payload_even_with_summary(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException):
        parser._parse_html('<script>window.cgiDataNew={item_show_type:5};</script><div id="js_content">video summary</div>', 'https://mp.weixin.qq.com/s/abc')


@pytest.mark.parametrize('platform,html', [
    ('coolapk', '<h1 class="message-title">title</h1><div class="feed-message">summary</div>'),
    ('coolapk', '<div>login please</div>'),
    ('wechat', '<h1>verify</h1>'),
    ('wechat', '<div id="js_content"><video src="http://127.0.0.1/a">not article</video></div>'),
    ('wechat', '<div id="js_content"><iframe src="https://channels.weixin.qq.com/a">embedded</iframe></div>'),
    ('wechat', '<div id="js_content"><img data-src="http://127.0.0.1/a"></div>'),
    ('wechat', '<div id="js_content">完成验证后继续访问</div><div id="js_verify">验证码</div>'),
])
def test_nonpublic_or_verification_is_explicit_failure(tmp_path, platform, html):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    parser = (CoolapkParser if platform == 'coolapk' else WechatArticleParser)({'cache_dir': str(tmp_path)}, None)
    with pytest.raises(ParseException):
        parser._parse_html(html, 'https://mp.weixin.qq.com/s/abc')


def test_wechat_article_literal_payload_fallback(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    result = parser._parse_html('''<script>window.cgiDataNew = {item_show_type:0,title:"payload title",content_noencode:"&lt;p&gt;body&lt;/p&gt;"};</script>''', 'https://mp.weixin.qq.com/s/abc')
    assert result.text == 'body'
    assert result.title == 'payload title'


@pytest.mark.asyncio
async def test_wechat_handler_fetches_full_public_query_url(tmp_path, monkeypatch):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    import aiohttp
    calls = []
    class Content:
        async def iter_chunked(self, size):
            yield b'<div id="js_content">public body</div>'
    class Response:
        status = 200
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append(url)
            return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, None)
    url = 'https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=key'
    keyword, match = parser.search_url(url)
    result = await parser.parse(keyword, match)
    assert calls == [url]
    assert result.text == 'public body'
    assert result.url == url


@pytest.mark.asyncio
async def test_public_fetch_checks_redirect_before_request_and_bounds_body(tmp_path, monkeypatch):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    from astrbot_plugin_parser_x.core.exception import ParseException
    import aiohttp
    calls = []
    class Response:
        status = 302
        headers = {'Location': 'http://127.0.0.1/private'}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    parser = CoolapkParser({'cache_dir': str(tmp_path)}, None)
    url = 'https://www.coolapk.com/feed/123?shareKey=a%2Bb&s=c'
    keyword, searched = parser.search_url(url)
    with pytest.raises(ParseException):
        await parser.parse(keyword, searched)
    assert [url for url, _ in calls] == [url]
    assert calls[0][1]['allow_redirects'] is False


def test_wechat_picture_payload_preserves_order_and_original_urls(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    class Downloads:
        def __init__(self): self.urls = []
        def download_img(self, url, **kwargs):
            self.urls.append(url)
            return Path('/tmp/offline.jpg')
    downloads = Downloads()
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, downloads)
    result = parser._parse_html("""<script>window.cgiDataNew = {item_show_type: '8' * 1, title: 'album', content_noencode: '', picture_page_info_list: [{cdn_url:'https://mmbiz.qpic.cn/b/1'}, {cdn_url:'https://mmbiz.qpic.cn/b/thumb',watermark_info:{cdn_url:'https://mmbiz.qpic.cn/b/2'}},],}; try { ignored(); } catch(e) {}</script>""", 'https://mp.weixin.qq.com/s/album')
    assert result.title == 'album'
    assert result.text is None
    assert downloads.urls == ['https://mmbiz.qpic.cn/b/1', 'https://mmbiz.qpic.cn/b/2']
    assert len(result.contents) == 2
    assert result.extra['text_card_detach_images']


def test_wechat_article_dom_body_only(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser
    class Downloads:
        def download_img(self, url, **kwargs): return Path('/tmp/offline.jpg')
    parser = WechatArticleParser({'cache_dir': str(tmp_path)}, Downloads())
    result = parser._parse_html('<h1 id="activity-name">headline</h1><nav>not body</nav><div id="js_content"><p>first</p><img data-src="https://mmbiz.qpic.cn/a/1"><p>second</p><img data-src="https://mmbiz.qpic.cn/a/2"><p>last</p></div>', 'https://mp.weixin.qq.com/s/abc')
    assert result.title == 'headline'
    assert result.text == 'first\nsecond\nlast'
    assert [block['type'] for block in result.extra['text_card_flow']] == ['text', 'image', 'text', 'image', 'text']
    assert result.extra['suppress_comments']
    assert not result.has_video


def test_coolapk_public_feed_uses_body_and_share_parameters(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    class Downloads:
        def __init__(self): self.urls = []
        def download_img(self, url, **kwargs):
            self.urls.append(url)
            return Path('/tmp/offline.jpg')
    downloads = Downloads()
    parser = CoolapkParser({'cache_dir': str(tmp_path)}, downloads)
    url = 'https://www.coolapk.com/feed/123?shareKey=a%2Bb&s=abc'
    result = parser._parse_html('<nav>not body</nav><div class="feed-message">first<br>second</div><div class="message-image-group"><img src="//image.coolapk.com/a.jpg"><img src="https://image.coolapk.com/b.jpg"></div>', url)
    assert result.text == 'first\nsecond'
    assert result.url == url
    assert downloads.urls == ['https://image.coolapk.com/a.jpg', 'https://image.coolapk.com/b.jpg']
    assert result.extra['suppress_comments'] is True
    assert not result.has_video


def test_coolapk_long_article_flow_is_in_source_order(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser
    class Downloads:
        def download_img(self, url, **kwargs): return Path('/tmp/offline.jpg')
    parser = CoolapkParser({'cache_dir': str(tmp_path)}, Downloads())
    result = parser._parse_html('<h1 class="message-title">long title</h1><article class="feed-article-message"><p>first</p><img class="message-image" src="//image.coolapk.com/a.jpg"><p>second</p><img class="message-image" src="//image.coolapk.com/b.jpg"><p>last</p></article><div class="feed-message">summary only</div>', 'https://www.coolapk.com/picture/123?s=abc')
    assert result.title == 'long title'
    assert result.text == 'first\nsecond\nlast'
    assert result.extra['text_card_flow'] == [
        {'type': 'text', 'text': 'first'},
        {'type': 'image', 'url': 'https://image.coolapk.com/a.jpg'},
        {'type': 'text', 'text': 'second'},
        {'type': 'image', 'url': 'https://image.coolapk.com/b.jpg'},
        {'type': 'text', 'text': 'last'},
    ]
