"""Public/full Zhihu content with optional configured cookies; independently implemented.

ParseHub's signing provider declares cv-cat/ZhihuApis ancestry, whose license
could not be verified. No signing code/constants or login bypass are included.
"""
from html import escape
from html.parser import HTMLParser
import re
from re import fullmatch, sub
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ..card_flow import apply_author_block, build_card_extra
from ..comment_settings import CommentSettings
from ..data import Author, Platform
from ..utils import maybe_close_session
from .base import BaseParser, ParseException, handle
from .zhihu_comment import ZhihuCommentFeed


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


# A ``video-box`` anchor also shows up as visible body text: Zhihu renders
# the bare video URL as a link, which is noise once real video exists.
_VIDEO_LINK = re.compile(r"\s*https?://(?:www\.)?zhihu\.com/video/\d+\s*")


class _Body(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.flow, self.text, self.hidden = [], [], 0
        self.video = None  # (lens_id, poster_url) when the body embeds one
        self.feed(source)
        self.flush()

    def flush(self):
        # ``<p>`` boundaries both open and close with a newline, so naively
        # dropping empty lines turned every paragraph break into a plain line
        # wrap: the body rendered as one dense wall of text. Collapse runs of
        # three or more newlines to one blank line instead, which keeps the
        # paragraph structure visible while trimming incidental whitespace.
        raw = ''.join(self.text).replace('\r\n', '\n').replace('\r', '\n')
        self.text.clear()
        value = sub(r'[ \t]+', ' ', raw)
        value = sub(r' *\n[ \t]*', '\n', value)
        value = sub(r'\n{3,}', '\n\n', value).strip()
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
        elif tag == 'a':
            # A video answer embeds ``<a class="video-box" data-lens-id=...>``;
            # neither the lens id nor the poster are in any JSON field, so the
            # only way to know this answer has a video is to read the anchor.
            attrs = dict(attrs)
            lens_id = attrs.get('data-lens-id') or ''
            if 'video-box' in (attrs.get('class') or '') and lens_id.isdigit():
                self.flush()
                poster = _image_url(attrs.get('data-poster')) or ''
                self.video = (lens_id, poster)
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

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        cookies = config.get('cookies', {})
        cookie = cookies.get('zhihu_cookie', '') if isinstance(cookies, dict) else ''
        self.zhihu_cookie = cookie.strip() if isinstance(cookie, str) and '\n' not in cookie and '\r' not in cookie else ''
        comment_settings = CommentSettings.from_config(config, 'zhihu', legacy_enabled=False)
        self.enable_comment_card = comment_settings.enabled
        self.comment_timeout = comment_settings.timeout
        self.comment_feed = ZhihuCommentFeed(self, limit=comment_settings.display_count)

    @staticmethod
    def _is_content_url(url):
        # Match the entire URL, not suffixes, userinfo, ports or arbitrary paths.
        return isinstance(url, str) and fullmatch(
            r'https://(?:www\.zhihu\.com/api/v4/answers/[0-9]+'
            r'(?:/comments\?[A-Za-z0-9._%&=,-]*)?'
            # 楼中楼: /api/v4/comments/{comment_id}/child_comments
            r'|www\.zhihu\.com/api/v4/comments/[0-9]+/child_comments'
            r'(?:\?[A-Za-z0-9._%&=,-]*)?'
            # Video playback info: <a class="video-box"> in an answer body only
            # carries the lens id, the mp4 itself lives on lens.zhihu.com.
            r'|lens\.zhihu\.com/api/v4/videos/[0-9]+'
            # 想法(pin)：/api/v4/pins/{id}
            r'|www\.zhihu\.com/api/v4/pins/[0-9]+'
            # Zvideo object ids differ from the underlying lens video ids.
            r'|www\.zhihu\.com/api/v4/zvideos/[0-9]+'
            r'|zhuanlan\.zhihu\.com/p/[0-9]+)',
            url,
        ) is not None

    def _zhihu_headers(self, url):
        # Leave User-Agent to impersonate='chrome120'; a short UA header
        # overrides that fingerprint and Zhihu answers with HTTP 403.
        headers = {}
        if self.zhihu_cookie and self._is_content_url(url):
            headers['Cookie'] = self.zhihu_cookie
        return headers

    async def http_get(self, url, *, headers=None, params=None,
                       allow_redirects=False, timeout=10, retries=1):
        """Isolated verified content transport; never use BaseParser's fallback.

        Accept the base call shape so existing parser-level fakes still work.
        Security settings and headers cannot be overridden by callers.
        """
        if not self._is_content_url(url):
            raise ParseException('知乎内容请求地址不受信任')
        try:
            request_timeout = max(1, min(float(timeout), 10))
            session = AsyncSession(
                impersonate='chrome120', verify=True, timeout=request_timeout,
                allow_redirects=False, discard_cookies=True,
            )
            try:
                return await session.get(
                    url, headers=self._zhihu_headers(url), params=params,
                    verify=True, allow_redirects=False, timeout=request_timeout,
                )
            finally:
                await maybe_close_session(session)
        except Exception:
            # Transport errors may embed request headers, cookies or response data.
            # Do not log, chain, retry insecurely or expose those details.
            raise ParseException('知乎内容请求失败，请稍后重试') from None

    async def _assemble(self, content, title, author, url, *, answer_id=None):
        body = _Body(content)
        flow = body.flow
        if not flow:
            raise ParseException('知乎未找到公开完整正文')
        avatar = _image_url(author.get('avatar_url'))
        writer = Author(name=author.get('name') or '匿名用户',
                        avatar=self.downloader.download_img(avatar, ext_headers=self.headers) if avatar else None)
        # A video answer has no picture in its body, so the card would render
        # with no hero at all. Use the video poster as the cover in that case;
        # a text answer keeps its own first image as the hero.
        video = await self._video_content(*body.video) if body.video else None
        if video is not None:
            # Cards render text_card_flow, not just result.text. Clean both
            # representations by stripping the anchor echo before building extra.
            flow = [(kind, _VIDEO_LINK.sub('', value).strip() if kind == 'text' else value)
                    for kind, value in flow]
            flow = [(kind, value) for kind, value in flow if value]
        cover = _image_url(body.video[1]) if body.video else None
        # The unified card reads the signature-row avatar from
        # extra['text_card_avatar'] only, so hand the validated URL over.
        extra = apply_author_block(
            build_card_extra(flow, cover=cover or None, is_video=bool(video),
                             extra={'suppress_comments': True}),
            avatar=avatar,
        )
        extra.update(self._comment_extra(answer_id, title=title, cover=cover,
                                        owner_id=author.get('id')))
        # ParseResult.__post_init__ drops every comment key when this flag is
        # set, so the opt-out must be cleared once a document factory exists.
        if 'text_card_comment_factory' in extra:
            extra.pop('suppress_comments', None)
        contents = self.create_image_contents([v for k, v in flow if k == 'image'])
        text = '\n'.join(value for kind, value in flow if kind == 'text') or None
        if body.video:
            if video is not None:
                contents.insert(0, video)
                # The body's own ``a.video-box`` anchor text is just the bare
                # video URL; once a real video content exists that line is
                # noise in the caption, so strip it.
                text = _VIDEO_LINK.sub('', text or '').strip() or None
        return self.result(title=title, author=writer, url=url,
            text=text,
            contents=contents,
            extra=extra)

    async def _video_content(self, lens_id, poster):
        """Resolve a ``video-box`` lens id into real mp4 content, or None.

        The answer body only carries the lens id; playback URLs come from
        lens.zhihu.com. Any failure yields None so text still ships.
        """
        try:
            info = await self._video_lookup(lens_id)
        except Exception:
            return None
        return await self._video_content_from_info(info, poster)

    async def _video_content_from_info(self, info, poster):
        """Build VideoContent from an already-fetched lens payload."""
        playlist = info.get('playlist') or {}
        if not isinstance(playlist, dict):
            return None
        playlist = {str(key).upper(): value for key, value in playlist.items()}
        # Lens uses upper-case qualities; zvideo metadata uses lower-case.
        # Prefer the AVC playlist for QQ compatibility over HEVC playlist_v2.
        # Prefer the highest available rendition, fall back downwards.
        best = None
        for key in ('FHD', 'HD', 'SD', 'LD'):
            entry = playlist.get(key)
            if isinstance(entry, dict) and entry.get('play_url'):
                best = entry
                break
        if best is None:
            return None
        cover = _image_url(info.get('cover_url')) or poster
        return self.create_video_content(
            best['play_url'],
            cover,
            duration=best.get('duration'),
            ext_headers={**self.headers, 'Referer': 'https://www.zhihu.com/'},
        )

    async def _video_lookup(self, lens_id):
        url = f'https://lens.zhihu.com/api/v4/videos/{lens_id}'
        if not self._is_content_url(url):
            return {}
        response = await self.http_get(url, timeout=10, retries=1, allow_redirects=False)
        if response.status_code != 200:
            return {}
        try:
            data = response.json()
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _comment_extra(self, answer_id, *, title, cover, owner_id):
        """Answer comments only; 专栏 articles have no answer id in this parser."""
        if not self.enable_comment_card or not answer_id:
            return {}

        async def build_comment_document():
            return await self.comment_feed.build_document(
                str(answer_id),
                work_title=title or '',
                cover=cover,
                owner_id=str(owner_id or ''),
            )

        return {
            'text_card_comment_factory': build_comment_document,
            'comment_timeout': self.comment_timeout,
        }

    @handle('zhuanlan.zhihu.com/p/', r'https?://zhuanlan\.zhihu\.com/p/(?P<article>\d+)(?![\w/])')
    async def parse_article(self, searched):
        url = f"https://zhuanlan.zhihu.com/p/{searched['article']}"
        response = await self.http_get(url, headers=self._zhihu_headers(url),
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
        return await self._assemble(content, title,
            {'name': document.author, 'avatar_url': document.avatar}, url)

    @handle('zhihu.com/zvideo/', r'https?://(?:www\.)?zhihu\.com/zvideo/(?P<video>\d+)(?![\w/])')
    async def parse_zvideo(self, searched):
        """Fetch the zvideo object before resolving its distinct playback id."""
        video_id = searched['video']
        endpoint = f'https://www.zhihu.com/api/v4/zvideos/{video_id}'
        response = await self.http_get(endpoint)
        if response.status_code != 200:
            raise ParseException(f'知乎视频作品接口不可用 HTTP {response.status_code}')
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise ParseException('知乎视频作品接口返回非 JSON') from None
        if (not isinstance(data, dict) or data.get('error')
                or str(data.get('id')) != video_id
                or data.get('type') != 'zvideo'
                or data.get('is_visible') is False or data.get('invisible') is True):
            raise ParseException('知乎视频作品身份不匹配或不可见')
        info = data.get('video')
        if not isinstance(info, dict):
            raise ParseException('知乎视频作品缺少播放信息')
        if info.get('is_paid') or info.get('is_trial'):
            raise ParseException('知乎付费或试看视频不支持下载')
        cover = _image_url(data.get('image_url')) or _image_url(info.get('thumbnail'))
        video = await self._video_content_from_info(info, cover or '')
        if video is None:
            lens_id = str(info.get('video_id') or '')
            if lens_id.isdigit():
                video = await self._video_content(lens_id, cover or '')
        if video is None:
            raise ParseException('知乎视频没有可用播放地址')
        author = data.get('author') if isinstance(data.get('author'), dict) else {}
        avatar = _image_url(author.get('avatar_url'))
        body = _Body(str(data.get('description') or ''))
        extra = apply_author_block(
            build_card_extra(body.flow, cover=cover, is_video=True,
                             extra={'suppress_comments': True}),
            avatar=avatar,
        )
        return self.result(
            title=data.get('title') or '知乎视频',
            author=Author(name=author.get('name') or '匿名用户',
                          avatar=self.downloader.download_img(avatar, ext_headers=self.headers) if avatar else None),
            url=f'https://www.zhihu.com/zvideo/{video_id}',
            text='\n'.join(v for k, v in body.flow if k == 'text') or None,
            contents=[video], extra=extra,
        )

    @handle('zhihu.com/pin/', r'https?://(?:www\.)?zhihu\.com/pin/(?P<pin>\d+)(?![\w/])')
    async def parse_pin(self, searched):
        """知乎想法 (pin) — a short post that may carry an inline video."""
        pin_id = searched['pin']
        url = f'https://www.zhihu.com/api/v4/pins/{pin_id}'
        response = await self.http_get(url, headers=self._zhihu_headers(url),
            timeout=10, retries=1, allow_redirects=False)
        if response.status_code != 200:
            raise ParseException(f'知乎想法接口不可用 HTTP {response.status_code}；请检查访问权限或配置的 Cookie')
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise ParseException('知乎想法接口返回非 JSON') from None
        if (not isinstance(data, dict) or data.get('error')
                or data.get('type') != 'pin' or str(data.get('id')) != pin_id
                or data.get('is_deleted') or not isinstance(data.get('author'), dict)):
            raise ParseException('知乎想法身份不匹配或已删除')

        blocks = data.get('content')
        if not isinstance(blocks, list) or not blocks:
            raise ParseException('知乎想法没有可读正文')
        # Text blocks carry HTML-ish bodies; video blocks carry their own
        # playlist, so no separate lens round-trip is needed here.
        text_html = '\n'.join(
            str(block.get('content') or '')
            for block in blocks if block.get('type') == 'text'
        ).strip()
        title = (data.get('excerpt_title') or '').split('<br>')[0].strip() or '知乎想法'
        author = data['author']
        avatar = _image_url(author.get('avatar_url'))
        writer = Author(name=author.get('name') or '匿名用户',
                        avatar=self.downloader.download_img(avatar, ext_headers=self.headers) if avatar else None)
        body = _Body(text_html)
        # A video pin's only picture is the video poster; without it the card
        # renders headless. Pass it as the cover so the hero matches the video.
        pin_videos = [b for b in blocks if b.get('type') == 'video']
        pin_cover = _image_url(pin_videos[0].get('thumbnail')) if pin_videos else None
        extra = apply_author_block(
            build_card_extra(body.flow, cover=pin_cover, is_video=bool(pin_videos),
                             extra={'suppress_comments': True}),
            avatar=avatar,
        )
        contents = self.create_image_contents(
            [value for kind, value in body.flow if kind == 'image'])
        for block in blocks:
            if block.get('type') != 'video':
                continue
            video = self._pin_video_content(block)
            if video is not None:
                contents.insert(0, video)
        return self.result(title=title, author=writer,
            url=f'https://www.zhihu.com/pin/{pin_id}',
            text='\n'.join(v for k, v in body.flow if k == 'text') or None,
            contents=contents, extra=extra)

    def _pin_video_content(self, block):
        """Build a VideoContent from a pin's inline ``playlist``, or None.

        Entries are format/quality/size records; pick the largest rendition so
        the card gets the best available picture without an extra request.
        """
        playlist = block.get('playlist')
        if not isinstance(playlist, list) or not playlist:
            return None
        best = None
        for entry in playlist:
            if not isinstance(entry, dict) or not entry.get('url'):
                continue
            if best is None or (entry.get('size') or 0) > (best.get('size') or 0):
                best = entry
        if best is None:
            return None
        cover = _image_url(block.get('thumbnail')) or _image_url(block.get('video_info', {}).get('thumbnail') if isinstance(block.get('video_info'), dict) else None)
        return self.create_video_content(
            best['url'],
            cover,
            duration=block.get('duration') or best.get('duration'),
            ext_headers={**self.headers, 'Referer': 'https://www.zhihu.com/'},
        )

    @handle('zhihu.com/question/', r'https?://(?:www\.)?zhihu\.com/question/(?P<question>\d+)/answer/(?P<answer>\d+)(?![\w/])')
    async def parse_answer(self, searched):
        question_id, answer_id = searched['question'], searched['answer']
        response = await self.http_get(f'https://www.zhihu.com/api/v4/answers/{answer_id}',
            params={'include': 'content,author,question'}, headers=self._zhihu_headers(f'https://www.zhihu.com/api/v4/answers/{answer_id}'),
            timeout=10, retries=1, allow_redirects=False)
        if response.status_code != 200:
            raise ParseException(f'知乎回答接口不可用 HTTP {response.status_code}；请检查访问权限或配置的 Cookie')
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise ParseException('知乎回答接口返回非 JSON') from None
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
        return await self._assemble(data['content'], question['title'], data['author'],
            f'https://www.zhihu.com/question/{question_id}/answer/{answer_id}',
            answer_id=answer_id)
