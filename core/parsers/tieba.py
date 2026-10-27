"""Public first-floor Tieba posts.

Historical field reference: ParseHub src/parsehub/provider_api/tieba.py and
src/parsehub/parsers/parser/tieba.py (MIT, Copyright (c) 2024 梓澪).
Android HTTPS protocol and post_list shapes independently observed in two
anonymous requests on 2026-10-26 (validation_tieba_client evidence).
No AGPL source/functions copied. Independent adapter, not a port.
Historical reference license:
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import aiohttp

from ..card_flow import apply_author_block, build_card_extra
from ..comment_settings import CommentSettings
from .base import BaseParser, ParseException, Platform, handle
from .tieba_media import portrait_url, emote_url


class TiebaParser(BaseParser):
    platform = Platform("tieba", "贴吧")

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.comment_settings = CommentSettings.from_config(config, "tieba")

    @classmethod
    def search_url(cls, url):
        keyword, match = super().search_url(url)
        query = parse_qs(urlsplit(match.group(0)).query)
        if any("live" in str(value).lower() for value in query.values()):
            raise ParseException("贴吧直播不解析")
        return keyword, match

    def _public_headers(self):
        return {"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"}

    @staticmethod
    def _media_url(value):
        if not isinstance(value, str):
            return ""
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        allowed = ("imgsrc.baidu.com", "tiebapic.baidu.com", "imgsa.baidu.com",
                   "tb.himg.baidu.com", "tb-video.bdstatic.com", "tb-video.baidupcs.com")
        # Only this observed CDN supports identity-preserving HTTPS upgrade.
        # Keep path/query byte-for-byte; do not broaden to arbitrary HTTP media.
        if (parsed.scheme == "http" and host == "tb-video.bdstatic.com"
                and not parsed.username and not parsed.password and port is None):
            value = "https" + value[4:]
            parsed = urlsplit(value)
        if (parsed.scheme != "https" or host not in allowed
                or parsed.username or parsed.password or port not in {None, 443}):
            return ""
        return value

    async def _request_json(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "tieba.baidu.com"
                or parsed.path != "/c/f/pb/page"):
            raise ParseException("贴吧请求主机或路径不允许")
        body = await self._request_bytes(method, url, **kwargs)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ParseException("贴吧响应不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise ParseException("贴吧响应结构无效")
        return data

    async def _request_bytes(self, method, url, **kwargs):
        # A separate cookie-less session cannot inherit another platform's auth.
        # Never follow a Location, even one pointing to a first-party host.
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "tieba.baidu.com"
                or not (parsed.path == "/c/f/pb/page"
                        or re.fullmatch(r"/p/[0-9]{1,20}", parsed.path))):
            raise ParseException("贴吧请求主机或路径不允许")
        limit = 2 * 1024 * 1024
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            ) as session:
                async with session.request(method, url, headers=self._public_headers(),
                        allow_redirects=False, **kwargs) as response:
                    if response.status != 200:
                        raise ParseException(f"贴吧接口 HTTP {response.status}")
                    if response.content_length is not None and response.content_length > limit:
                        raise ParseException("贴吧响应超过大小限制")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > limit:
                            raise ParseException("贴吧响应超过大小限制")
                    return bytes(body)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ParseException("贴吧公开接口请求失败") from exc

    async def _fetch_page(self, tid, page=1):
        if not re.fullmatch(r"[0-9]{1,20}", str(tid)) or page != 1:
            raise ParseException("贴吧帖子或页码无效")
        data = {"_client_type": "2", "_client_version": "12.35.1.0",
                "kz": str(tid), "lz": "1", "pn": str(page), "rn": "2", "with_floor": "0"}
        source = "".join(f"{key}={value}" for key, value in sorted(data.items()))
        data["sign"] = hashlib.md5((source + "tiebaclient!!!").encode()).hexdigest().upper()
        payload = await self._request_json("POST", "https://tieba.baidu.com/c/f/pb/page", data=data)
        if str(payload.get("error_code", "0")) != "0":
            raise ParseException("贴吧帖子不可用")
        return payload

    @handle("tieba.baidu.com", r"(?<![\w/.:@-])https?://tieba\.baidu\.com/p/(?P<tid>[0-9]{1,20})(?:\?[^\s<>]*)?(?=$|[\s<>])")
    async def parse_post(self, searched):
        tid = searched.group("tid")
        payload = await self._fetch_page(tid)
        return self._build_result(payload, tid)

    def _build_result(self, payload, tid):
        if not isinstance(payload, dict) or str(payload.get("error_code")) != "0":
            raise ParseException("贴吧帖子不可用")
        thread = payload.get("thread")
        if not isinstance(thread, dict) or str(thread.get("id", "")) != tid:
            raise ParseException("贴吧帖子 ID 不匹配")
        posts = payload.get("post_list")
        roots = [post for post in posts if isinstance(post, dict) and str(post.get("floor")) == "1"] if isinstance(posts, list) else []
        if len(roots) != 1:
            raise ParseException("贴吧首楼内容不可用，不能使用其他楼层替代")
        root = roots[0]
        root_id = str(root.get("id") or "")
        if (not root_id or (thread.get("post_id") is not None
                and str(thread["post_id"]) != root_id)):
            raise ParseException("贴吧首楼 ID 不匹配")
        content = root.get("content")
        # Title-only threads return content=null; absent body must not fail the
        # whole post when a title or thread video exists. A malformed body of
        # another type still violates the parser contract.
        if content is None:
            content = []
        elif not isinstance(content, list):
            raise ParseException("贴吧首楼内容结构无效")
        author_id = str(root.get("author_id") or "")
        users = payload.get("user_list")
        authors = [user for user in users if isinstance(user, dict)
                   and author_id and str(user.get("id")) == author_id] if isinstance(users, list) else []
        creator = thread.get("author")
        if len(authors) != 1 or (isinstance(creator, dict) and creator.get("id") is not None
                                and str(creator["id"]) != author_id):
            raise ParseException("贴吧首楼作者关联无效")
        author = authors[0]
        video = thread.get("video_info")
        video = video if isinstance(video, dict) else {}
        video_url = self._media_url(video.get("video_url"))
        cover = self._media_url(video.get("thumbnail_url"))
        duration_value = video.get("video_duration")
        flow, emotes = [], {}

        def append_text(text):
            if flow and flow[-1][0] == "text":
                flow[-1] = ("text", flow[-1][1] + text)
            else:
                flow.append(("text", text))

        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type"))
            if kind == "0" and isinstance(item.get("text"), str) and item["text"]:
                append_text(item["text"])
            elif kind == "2":
                # Labels may be absent; the asset id is the actual identity.
                label = str(item.get('c') or item.get('text') or '表情')
                token = '[' + label + ']'
                append_text(token)
                if url := emote_url(item.get('text')):
                    emotes[token] = url
            elif kind == "3":
                image = next((url for key in ("origin_src", "big_cdn_src", "cdn_src", "cdn_src_active")
                              if (url := self._media_url(item.get(key)))), "")
                if not image:
                    raise ParseException("贴吧正文媒体地址不受支持")
                flow.append(("image", image))
            elif kind == "5" and not video_url:
                video_url = self._media_url(item.get("link"))
                if not video_url:
                    raise ParseException("贴吧正文媒体地址不受支持")
                cover = self._media_url(item.get("src")) or cover
                duration_value = item.get("during_time")
        if video and not video_url:
            raise ParseException("贴吧正文媒体地址不受支持")
        if not flow and not video_url:
            title_text = thread.get("title")
            if not (isinstance(title_text, str) and title_text.strip()):
                raise ParseException("贴吧首楼内容不可用")
        avatar = portrait_url(author.get("portrait"))
        headers = self._public_headers()
        contents = self.create_image_contents([v for k, v in flow if k == "image"], ext_headers=headers)
        if video_url:
            try:
                duration = float(duration_value or 0)
                if not 0 <= duration <= 86400:
                    duration = 0
            except (TypeError, ValueError):
                duration = 0
            contents.insert(0, self.create_video_content(video_url, cover, duration, ext_headers=headers))
        # Client video-only bodies often contain a lone newline. It is not a
        # paragraph and must not become a padded empty flow-text container.
        flow = [(kind, value) for kind, value in flow if kind != 'text' or value.strip()]
        extra = apply_author_block(build_card_extra(flow, cover=cover,
            is_video=bool(video_url)), avatar=avatar)
        if emotes:
            extra["card_emotes"] = emotes
            plain = "\n".join(value for kind, value in flow if kind == "text")
            shown = [
                token
                for token in emotes
                if token in plain
            ]
            if shown:
                extra["card_emote_captures"] = shown
        if self.comment_settings.enabled:
            from .tieba_comment import TiebaCommentFeed
            feed = TiebaCommentFeed(self)
            extra["text_card_comment_factory"] = lambda: feed.fetch_document(tid, author_id)
            extra["comment_timeout"] = self.comment_settings.timeout
        try:
            timestamp = int(root.get("time") or 0) or None
        except (TypeError, ValueError):
            timestamp = None
        body_text = "\n".join(value for kind, value in flow if kind == "text")
        return self.result(title=thread.get("title"), timestamp=timestamp,
            text=body_text or None,
            url=f"https://tieba.baidu.com/p/{tid}",
            author=self.create_author(author.get("name_show") or author.get("name") or "贴吧用户", avatar,
                description=f"贴吧用户 {author_id}", ext_headers=headers),
            contents=contents, extra=extra)
