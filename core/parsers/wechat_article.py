"""Public mp.weixin.qq.com articles only; never WeChat Channels.

Field names informed by ParseHub (MIT, Copyright (c) 2024 梓澪),
provider_api/weixin.py and parsers/parser/weixin.py. Parsing uses no JS eval.
"""
import ast
import math
import re
from html import unescape

from ..data import Platform
from .base import BaseParser, ParseException, handle
from .coolapk import CoolapkParser, _Document, _flow, _image_url, _fetch_public


class _LiteralReader:
    """A bounded subset of JSON/JS literals; calls/expressions are not executed."""
    token = re.compile(r'''\s*(?:("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(-?\d+(?:\.\d+)?)|([A-Za-z_$][\w$]*)|([{}\[\]:,*]))''')

    def __init__(self, source):
        self.source, self.pos = source, 0

    def take(self):
        match = self.token.match(self.source, self.pos)
        if not match:
            raise ValueError('unsupported JS literal')
        self.pos = match.end()
        return match

    def peek(self):
        return self.source[self.pos:].lstrip()[:1]

    def value(self, depth=0):
        if depth > 32:
            raise ValueError('payload nesting limit')
        match = self.take()
        quoted, number, name, punct = match.groups()
        if quoted:
            value = ast.literal_eval(quoted)
            if self.peek() == '*':
                self.take()
                if self.take().group(2) != '1':
                    raise ValueError('unsupported multiplication')
                if len(value) > 64 or not re.fullmatch(r'-?\d+(?:\.\d+)?', value, flags=re.ASCII):
                    raise ValueError('unsupported numeric string')
                value = float(value) if '.' in value else int(value)
                if not math.isfinite(value):
                    raise ValueError('nonfinite numeric string')
            return value
        if number:
            return float(number) if '.' in number else int(number)
        if name in {'true', 'false', 'null', 'undefined'}:
            return {'true': True, 'false': False, 'null': None, 'undefined': None}[name]
        if punct == '{':
            result = {}
            while self.peek() != '}':
                key = self.take()
                key = ast.literal_eval(key.group(1)) if key.group(1) else key.group(3)
                if not key or self.take().group(4) != ':':
                    raise ValueError('invalid object key')
                result[key] = self.value(depth + 1)
                if self.peek() != ',':
                    break
                self.take()
            if self.take().group(4) != '}':
                raise ValueError('invalid object')
            return result
        if punct == '[':
            result = []
            while self.peek() != ']':
                result.append(self.value(depth + 1))
                if self.peek() != ',':
                    break
                self.take()
            if self.take().group(4) != ']':
                raise ValueError('invalid array')
            return result
        raise ValueError('unsupported JS value')


def _payload(root):
    scripts = []
    def visit(node):
        if node.tag == 'script':
            scripts.append(''.join(c for c in node.children if isinstance(c, str)))
        for child in node.children:
            if not isinstance(child, str):
                visit(child)
    visit(root)
    for script in scripts:
        if match := re.search(r'\b(?:window\.)?cgiDataNew\s*=\s*(?={)', script):
            try:
                result = _LiteralReader(script[match.end():]).value()
                if isinstance(result, dict):
                    return result
            except (ValueError, SyntaxError, RecursionError) as exc:
                raise ParseException('微信公众号正文数据格式不受支持') from exc
    return None


def _legacy_identity(root):
    """Read only declared legacy string constants, never JS expressions/eval.

    No cgiDataNew identity mapping is assumed without a captured page fixture.
    Comments and other quoted strings are consumed to avoid matching decoys.
    """
    quoted = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
    tokens = re.compile(
        r'//[^\n]*|/\*[\s\S]*?\*/|/(?:\\.|[^/\\\n])+/[A-Za-z]*|'
        r'\b(?:var|let|const)\s+(nickname|round_head_img)\s*=\s*('
        + quoted + r')\s*(?=;|$)|' + quoted)
    fields = {}

    def visit(node):
        if node.attrs.get('id') == 'js_content':
            return
        if node.tag == 'script':
            source = ''.join(c for c in node.children if isinstance(c, str))
            # Template interpolation is outside this legacy-literal subset.
            if '`' in source:
                return
            for match in tokens.finditer(source):
                if match.group(1):
                    try:
                        value = ast.literal_eval(match.group(2).replace(r'\/', '/'))
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(value, str) and value.strip():
                        fields.setdefault(match.group(1), unescape(value).strip())
        for child in node.children:
            if not isinstance(child, str):
                visit(child)

    visit(root)
    return fields


class WechatArticleParser(BaseParser):
    platform = Platform('wechat_article', '微信公众号')
    image_hosts = {'mmbiz.qpic.cn', 'mmbiz.qlogo.cn'}
    _assemble = CoolapkParser._assemble

    @handle('mp.weixin.qq.com', r'https?://mp\.weixin\.qq\.com/s(?:/[A-Za-z0-9_-]+)?(?:\?[^\s<>"\u3000]*)?(?![\w/])')
    async def parse_public_article(self, searched):
        html, url = await _fetch_public(self, searched.group(0),
            {'mp.weixin.qq.com'}, r'/s(?:/[A-Za-z0-9_-]+)?', max_page_bytes=4 * 1024 * 1024)
        return self._parse_html(html, url)

    def _parse_html(self, html, url):
        root = _Document(html).root
        if root.find(lambda n: n.attrs.get('id') in {'js_verify', 'js_captcha', 'verify_container', 'captcha_container'}):
            raise ParseException('微信公众号页面要求验证，无法读取公开正文')
        # Account identity must not come from the article byline or body images.
        def outside_body(node, predicate):
            if node.attrs.get('id') == 'js_content':
                return None
            if predicate(node):
                return node
            for child in node.children:
                if not isinstance(child, str):
                    if found := outside_body(child, predicate):
                        return found
            return None

        name_node = outside_body(root, lambda n: n.attrs.get('id') == 'js_name')
        name = '\n'.join(v for k, v in _flow(name_node, set()) if k == 'text') if name_node else None
        avatar_node = outside_body(root, lambda n: n.tag == 'img'
            and 'profile_avatar' in (n.attrs.get('class') or '').split())
        avatar = _image_url(avatar_node.attrs.get('data-src') or avatar_node.attrs.get('src'),
            self.image_hosts) if avatar_node else None
        legacy = _legacy_identity(root)
        name = name or legacy.get('nickname')
        avatar = avatar or _image_url(legacy.get('round_head_img'), self.image_hosts)
        author = self.create_author(name) if name else None
        data = _payload(root)
        if data and str(data.get('item_show_type')) not in {'0', '8'}:
            raise ParseException('微信公众号仅支持公开文章与图片消息，不支持音视频')
        if data and str(data.get('item_show_type')) == '8':
            source = data.get('content_noencode') or ''
            flow = _flow(_Document(unescape(source)).root, self.image_hosts) if isinstance(source, str) else []
            for entry in data.get('picture_page_info_list') or []:
                if not isinstance(entry, dict):
                    continue
                watermark = entry.get('watermark_info')
                candidate = watermark.get('cdn_url') if isinstance(watermark, dict) else None
                if image := _image_url(candidate or entry.get('cdn_url'), self.image_hosts):
                    flow.append(('image', image))
            return self._assemble(flow, url, title=data.get('title') or None,
                author=author, avatar=avatar)
        body = root.find(lambda n: n.attrs.get('id') == 'js_content')
        if body is None and data and str(data.get('item_show_type')) == '0':
            source = data.get('content_noencode')
            if isinstance(source, str) and source.strip():
                return self._assemble(_flow(_Document(unescape(source)).root, self.image_hosts),
                    url, title=data.get('title') or None, author=author, avatar=avatar)
        if body is None:
            raise ParseException('微信公众号公开正文不可用，可能需要验证或文章已删除')
        title_node = root.find(lambda n: n.attrs.get('id') == 'activity-name')
        title = '\n'.join(v for k, v in _flow(title_node, set()) if k == 'text') if title_node else None
        return self._assemble(_flow(body, self.image_hosts), url, title=title, author=author, avatar=avatar)
