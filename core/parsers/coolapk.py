"""Public Coolapk articles.

Selectors informed by ParseHub (MIT, Copyright (c) 2024 梓澪),
provider_api/coolapk.py and parsers/parser/coolapk.py. No API signing.
"""
import re
import aiohttp
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

from ..card_flow import build_card_extra
from ..data import ImageContent, Platform
from .base import BaseParser, ParseException, handle
from .gallery import ordered_media_plan


class _Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def find(self, predicate):
        if predicate(self):
            return self
        for child in self.children:
            if isinstance(child, _Node) and (found := child.find(predicate)):
                return found
        return None


class _Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 128:
            raise ParseException('公开文章 HTML 嵌套超过上限')
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'embed', 'area', 'base', 'col', 'param', 'track'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _image_url(value, hosts):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('//'):
        value = 'https:' + value
    try:
        p = urlsplit(value)
        if (p.scheme in {'http', 'https'} and p.hostname in hosts
                and not p.username and not p.password and p.port in {None, 80, 443}):
            return value
    except ValueError:
        pass
    return None


def _flow(node, hosts):
    parts = []
    text = []
    def flush():
        value = '\n'.join(line.strip() for line in ''.join(text).splitlines() if line.strip())
        text.clear()
        if value:
            parts.append(('text', value))
    def walk(item):
        if isinstance(item, str):
            text.append(item)
            return
        if item.tag in {'script', 'style', 'noscript', 'iframe', 'video', 'audio', 'mp-video', 'mp-audio', 'mpvoice'}:
            return
        if item.tag == 'img':
            url = _image_url(item.attrs.get('data-src') or item.attrs.get('src'), hosts)
            if url:
                flush()
                parts.append(('image', url))
            return
        if item.tag in {'br', 'p', 'div', 'section', 'li', 'h1', 'h2', 'h3', 'blockquote'}:
            text.append('\n')
        for child in item.children:
            walk(child)
        if item.tag in {'p', 'div', 'section', 'li', 'h1', 'h2', 'h3', 'blockquote'}:
            text.append('\n')
    walk(node)
    flush()
    return parts


MAX_PAGE_BYTES = 2 * 1024 * 1024


def _trusted_page(url, hosts, path_pattern):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme in {'http', 'https'} and parsed.hostname in hosts
                and parsed.port in {None, 80, 443} and not parsed.username
                and not parsed.password and re.fullmatch(path_pattern, parsed.path)):
            return url
    except ValueError:
        pass
    raise ParseException('公开文章地址或跳转不可信')


async def _fetch_public(parser, url, hosts, path_pattern, *, max_page_bytes=MAX_PAGE_BYTES):
    current = _trusted_page(url, hosts, path_pattern)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                headers=parser.headers, trust_env=False) as session:
            for _ in range(4):
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get('Location')
                        if not location:
                            raise ParseException('公开文章跳转缺少地址')
                        current = _trusted_page(urljoin(current, location), hosts, path_pattern)
                        continue
                    if response.status != 200:
                        raise ParseException(f'公开文章请求失败 HTTP {response.status}')
                    if 'html' not in response.headers.get('Content-Type', '').lower():
                        raise ParseException('公开文章返回非 HTML 正文')
                    length = response.headers.get('Content-Length')
                    if length and int(length) > max_page_bytes:
                        raise ParseException('公开文章超过大小上限')
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > max_page_bytes:
                            raise ParseException('公开文章超过大小上限')
                    return chunks.decode('utf-8', errors='replace'), current
            raise ParseException('公开文章跳转次数过多')
    except ParseException:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise ParseException('公开文章请求失败或正文不合法') from exc


class CoolapkParser(BaseParser):
    platform = Platform('coolapk', '酷安')
    image_hosts = {'image.coolapk.com'}

    @handle('coolapk.com', r'https?://(?:www\.)?coolapk\.com/(?:feed|picture)/\d+(?:\?[^\s<>"\u3000]*)?(?![\w/])')
    async def parse_public_article(self, searched):
        html, url = await _fetch_public(self, searched.group(0),
            {'www.coolapk.com', 'coolapk.com'}, r'/(?:feed|picture)/\d+')
        return self._parse_html(html, url)

    def _parse_html(self, html, url):
        root = _Document(html).root
        def by_class(name):
            return root.find(lambda n: name in (n.attrs.get('class') or '').split())
        title_node = by_class('message-title')
        if title_node is not None:
            body = by_class('feed-article-message')
            if body is None:
                raise ParseException('酷安长文正文不可用，请保留 shareKey 或 s 参数')
            title = '\n'.join(v for k, v in _flow(title_node, set()) if k == 'text')
            return self._assemble(_flow(body, self.image_hosts), url, title=title or None)
        body = by_class('feed-message')
        if body is None:
            raise ParseException('酷安未找到公开正文，请保留 shareKey 或 s 参数')
        flow = _flow(body, self.image_hosts)
        if group := by_class('message-image-group'):
            flow.extend(_flow(group, self.image_hosts))
        return self._assemble(flow, url)

    def _assemble(self, flow, url, title=None, author=None):
        if not flow:
            raise ParseException('未找到公开文章正文或可信图片')
        images = [value for kind, value in flow if kind == 'image']
        contents = [ImageContent(self.downloader.download_img(image, ext_headers=self.headers)) for image in images]
        return self.result(title=title, author=author,
            text='\n'.join(value for kind, value in flow if kind == 'text') or None,
            url=url, contents=contents, delivery=ordered_media_plan(contents),
            extra=build_card_extra(flow, extra={'suppress_comments': True}))
