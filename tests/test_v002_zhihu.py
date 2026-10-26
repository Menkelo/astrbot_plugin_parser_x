"""Anonymous-only Zhihu contracts; all content here is synthetic."""
import asyncio
from pathlib import Path
import pytest


def parser(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.zhihu import ZhihuParser
    class Downloads:
        def __init__(self): self.urls = []
        def download_img(self, url, **kwargs):
            self.urls.append(url)
            return Path('/tmp/offline.jpg')
    return ZhihuParser({'cache_dir': str(tmp_path)}, Downloads())


def answer():
    return {'id': 22, 'type': 'answer', 'question': {'id': 11, 'title': 'Question'},
            'author': {'name': 'Writer', 'avatar_url': 'https://pic1.zhimg.com/avatar.jpg'},
            'content': '<p>Before</p><img src="https://pic1.zhimg.com/one.jpg"><p>Middle</p><img data-original="https://pic2.zhimg.com/two.jpg"><p>After</p>'}


def test_answer_public_api_preserves_flow_author_and_identity(tmp_path):
    p = parser(tmp_path)
    calls = []
    class Response:
        status_code = 200
        def json(self): return answer()
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    p.http_get = get
    keyword, match = p.search_url('https://www.zhihu.com/question/11/answer/22')
    result = asyncio.run(p.parse(keyword, match))
    assert result.title == 'Question'
    assert result.text == 'Before\nMiddle\nAfter'
    assert result.author.name == 'Writer'
    assert result.author.avatar == Path('/tmp/offline.jpg')
    assert result.extra['text_card_flow'] == [
        {'type': 'text', 'text': 'Before'}, {'type': 'image', 'url': 'https://pic1.zhimg.com/one.jpg'},
        {'type': 'text', 'text': 'Middle'}, {'type': 'image', 'url': 'https://pic2.zhimg.com/two.jpg'},
        {'type': 'text', 'text': 'After'}]
    assert calls[0][0] == 'https://api.zhihu.com/v4/answers/22'
    assert calls[0][1]['params'] == {'include': 'content,author,question'}
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['timeout'] <= 12
    assert calls[0][1]['retries'] == 1
    assert result.extra['suppress_comments'] is True
    assert not any('factory' in k for k in result.extra)


@pytest.mark.parametrize('change', [
    {'id': 99}, {'type': 'article'}, {'question': {'id': 99, 'title': 'Wrong'}},
    {'question': {'id': 11, 'title': ''}}, {'content': ''}, {'content': None},
    {'is_truncated': True}, {'is_full_content': False}, {'content': '<p>登录后查看完整内容</p>'},
    {'error': {'need_login': True}}, {'author': None},
    {'is_locked': True}, {'is_paid': True}, {'has_more': True},
])
def test_answer_rejects_wrong_identity_or_incomplete_content(tmp_path, change):
    from astrbot_plugin_parser_x.core.exception import ParseException
    p = parser(tmp_path)
    data = {**answer(), **change}
    class Response:
        status_code = 200
        def json(self): return data
    async def get(*args, **kwargs): return Response()
    p.http_get = get
    key, match = p.search_url('https://www.zhihu.com/question/11/answer/22')
    with pytest.raises(ParseException):
        asyncio.run(p.parse(key, match))
    assert p.downloader.urls == []


def article_html():
    return '''<html><head><link rel="canonical" href="https://zhuanlan.zhihu.com/p/33"><meta name="author" content="Writer"></head><body><h1 class="Post-Title">Column title</h1><img class="Avatar" src="https://pic1.zhimg.com/avatar.jpg"><div class="RichText ztext Post-RichText"><p>Public first</p><img src="https://pic1.zhimg.com/a.jpg"><p>Public last</p></div></body></html>'''


def test_public_column_reads_visible_html_only(tmp_path):
    p = parser(tmp_path)
    class Response:
        status_code = 200
        text = article_html()
    calls = []
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    p.http_get = get
    key, match = p.search_url('https://zhuanlan.zhihu.com/p/33?utm_source=x')
    result = asyncio.run(p.parse(key, match))
    assert result.title == 'Column title'
    assert result.text == 'Public first\nPublic last'
    assert result.author.name == 'Writer'
    assert result.author.avatar == Path('/tmp/offline.jpg')
    assert result.url == 'https://zhuanlan.zhihu.com/p/33'
    assert calls[0][0] == result.url
    assert calls[0][1]['allow_redirects'] is False
    assert result.extra['suppress_comments']


@pytest.mark.parametrize('html', [
    article_html().replace('/p/33', '/p/44'),
    article_html().replace('Column title', ''),
    article_html().replace('Public first', '登录后查看完整内容'),
    article_html().replace('Post-RichText', 'Summary'),
    article_html().replace('<body>', '<body><div class="RichContent is-collapsed">'),
])
def test_column_rejects_identity_missing_or_truncated_body(tmp_path, html):
    from astrbot_plugin_parser_x.core.exception import ParseException
    p = parser(tmp_path)
    class Response:
        status_code = 200
        text = html
    async def get(*args, **kwargs): return Response()
    p.http_get = get
    key, match = p.search_url('https://zhuanlan.zhihu.com/p/33')
    with pytest.raises(ParseException):
        asyncio.run(p.parse(key, match))
    assert p.downloader.urls == []


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'https://pic1.zhimg.com.evil.org/x',
    'https://user:pass@pic1.zhimg.com/x', 'https://pic1.zhimg.com:999/x', 'data:image/png;base64,abc'])
def test_untrusted_images_never_reach_downloader(tmp_path, url):
    p = parser(tmp_path)
    data = answer()
    data['author']['avatar_url'] = url
    data['content'] = f'<p>Safe</p><img src="{url}"><script>hidden</script>'
    class Response:
        status_code = 200
        def json(self): return data
    async def get(*args, **kwargs): return Response()
    p.http_get = get
    key, match = p.search_url('https://www.zhihu.com/question/11/answer/22')
    result = asyncio.run(p.parse(key, match))
    assert result.text == 'Safe'
    assert p.downloader.urls == []


@pytest.mark.parametrize('void_markup', [
    '<img src="https://pic1.zhimg.com/a.jpg"/>', '<br/>',
    '<img src="https://pic1.zhimg.com/a.jpg"></img>', '<br></br>',
])
def test_column_void_tags_preserve_trailing_body(tmp_path, void_markup):
    p = parser(tmp_path)
    class Response:
        status_code = 200
        text = article_html().replace('<img src="https://pic1.zhimg.com/a.jpg">', void_markup)
    async def get(*args, **kwargs): return Response()
    p.http_get = get
    key, match = p.search_url('https://zhuanlan.zhihu.com/p/33')
    result = asyncio.run(p.parse(key, match))
    assert result.text == 'Public first\nPublic last'
    if '<img' in void_markup:
        assert len(result.contents) == 1
