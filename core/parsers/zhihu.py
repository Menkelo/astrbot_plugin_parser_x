"""Anonymous public Zhihu content only; independently implemented.

ParseHub's signing provider declares cv-cat/ZhihuApis ancestry, whose license
could not be verified. No signing code/constants or login bypass are included.
"""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..card_flow import build_card_extra
from ..data import Author, Platform
from .base import BaseParser, ParseException, handle


def _image_url(value):
    if not isinstance(value, str):
        return None
    if value.startswith('//'):
        value = 'https:' + value
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        if (url.scheme == 'https' and (host == 'zhimg.com' or host.endswith('.zhimg.com'))
                and not url.username and not url.password and url.port in {None, 443}):
            return value
    except ValueError:
        pass
    return None


class _Body(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.flow, self.text, self.hidden = [], [], 0
        self.feed(source)
        self.flush()

    def flush(self):
        value = '\n'.join(line.strip() for line in ''.join(self.text).splitlines() if line.strip())
        self.text.clear()
        if value:
            self.flow.append(('text', value))

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'iframe'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == 'img':
            attrs = dict(attrs)
            url = _image_url(attrs.get('data-original') or attrs.get('data-actualsrc') or attrs.get('src'))
            if url:
                self.flush()
                self.flow.append(('image', url))
        elif tag in {'p', 'br', 'div', 'li', 'blockquote', 'h1', 'h2', 'h3'}:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'iframe'}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {'p', 'div', 'li', 'blockquote', 'h1', 'h2', 'h3'}:
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


class _Article(HTMLParser):
    VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img',
                           'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'})

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.canonical = self.author = self.avatar = ''
        self.title, self.body = [], []
        self.title_depth = self.body_depth = 0
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = (attr.get('class') or '').split()
        if tag == 'link' and attr.get('rel') == 'canonical':
            self.canonical = attr.get('href', '')
        if tag == 'meta' and attr.get('name') == 'author':
            self.author = attr.get('content', '')
        if tag == 'img' and 'Avatar' in classes:
            self.avatar = attr.get('src', '')
        if 'Post-Title' in classes:
            self.title_depth = 1
        elif self.title_depth and tag not in self.VOID_TAGS:
            self.title_depth += 1
        if self.body_depth:
            self.body.append(self.get_starttag_text())
            if tag not in self.VOID_TAGS:
                self.body_depth += 1
        elif 'Post-RichText' in classes:
            self.body_depth = 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS:
            return
        if self.title_depth:
            self.title_depth -= 1
        if self.body_depth:
            self.body_depth -= 1
            if self.body_depth:
                self.body.append(f'</{tag}>')

    def handle_data(self, data):
        if self.title_depth:
            self.title.append(data)
        if self.body_depth:
            self.body.append(escape(data))


class ZhihuParser(BaseParser):
    platform = Platform('zhihu', '知乎')

    def _assemble(self, content, title, author, url):
        flow = _Body(content).flow
        if not flow:
            raise ParseException('知乎未找到公开完整正文')
        avatar = _image_url(author.get('avatar_url'))
        writer = Author(name=author.get('name') or '匿名用户',
                        avatar=self.downloader.download_img(avatar, ext_headers=self.headers) if avatar else None)
        return self.result(title=title, author=writer, url=url,
            text='\n'.join(value for kind, value in flow if kind == 'text') or None,
            contents=self.create_image_contents([v for k, v in flow if k == 'image']),
            extra=build_card_extra(flow, extra={'suppress_comments': True}))

    @handle('zhuanlan.zhihu.com/p/', r'https?://zhuanlan\.zhihu\.com/p/(?P<article>\d+)(?![\w/])')
    async def parse_article(self, searched):
        url = f"https://zhuanlan.zhihu.com/p/{searched['article']}"
        response = await self.http_get(url, headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10, retries=1, allow_redirects=False)
        if response.status_code != 200:
            raise ParseException(f'知乎专栏公开 HTML 不可用 HTTP {response.status_code}')
        document = _Article(response.text)
        title = ''.join(document.title).strip()
        content = ''.join(document.body)
        if (document.canonical != url or not title or not content.strip()
                or document.body_depth
                or any(marker in response.text for marker in (
                    '登录后查看', '查看完整内容', '展开阅读全文', 'is-collapsed',
                    'ContentItem-more', 'zh-zse-ck'))):
            raise ParseException('知乎专栏身份不匹配或无可读完整正文')
        return self._assemble(content, title,
            {'name': document.author, 'avatar_url': document.avatar}, url)

    @handle('zhihu.com/question/', r'https?://(?:www\.)?zhihu\.com/question/(?P<question>\d+)/answer/(?P<answer>\d+)(?![\w/])')
    async def parse_answer(self, searched):
        question_id, answer_id = searched['question'], searched['answer']
        response = await self.http_get(f'https://api.zhihu.com/v4/answers/{answer_id}',
            params={'include': 'content,author,question'}, headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10, retries=1, allow_redirects=False)
        if response.status_code != 200:
            raise ParseException(f'知乎匿名回答不可用 HTTP {response.status_code}；未尝试签名或登录')
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise ParseException('知乎匿名回答返回非 JSON') from exc
        question = data.get('question') if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get('error')
                or data.get('type') != 'answer' or str(data.get('id')) != answer_id
                or not isinstance(question, dict) or str(question.get('id')) != question_id
                or not isinstance(question.get('title'), str) or not question['title'].strip()
                or not isinstance(data.get('author'), dict)
                or not isinstance(data.get('content'), str) or not data['content'].strip()
                or any(data.get(flag) for flag in ('is_truncated', 'is_locked', 'is_paid', 'has_more'))
                or data.get('is_full_content') is False
                or any(marker in data['content'] for marker in ('登录后查看', '查看完整内容', '展开阅读全文'))):
            raise ParseException('知乎回答身份不匹配或正文不完整，拒绝将摘要当全文')
        return self._assemble(data['content'], question['title'], data['author'],
            f'https://www.zhihu.com/question/{question_id}/answer/{answer_id}')
