"""Anonymous public NGA threads, independently implemented from observed API fields.

No account cookies, private endpoints, or external parser source are used.
"""
from __future__ import annotations

import json
import re
import time

import aiohttp
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from ..card_flow import build_card_extra
from ..comment_settings import CommentSettings
from .base import BaseParser, ParseException, Platform, handle

HOSTS = frozenset({"bbs.nga.cn", "nga.178.com", "ngabbs.com"})
_EMOTE_TOKEN_RE = re.compile(r"\[s:[a-zA-Z0-9]{1,4}:[^\[\]\n]{1,16}\]")
IMAGE_HOSTS = frozenset({"img.nga.cn", "img4.nga.cn", "img.nga.178.com",
                         "img4.nga.178.com", "pic1.178.com", "attachments.nga.178.com"})


def image_url(value: str, prefix: str) -> str:
    try:
        url = urljoin(prefix, unescape(value.strip()).removeprefix("./"))
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or parsed.hostname not in IMAGE_HOSTS
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return ""
        return url
    except ValueError:
        return ""


def bbcode_flow(content: str, prefix: str) -> list[tuple[str, str]]:
    """把 BBCode 拆成交错文本/图片。

    ``[s:ac:汗]`` 这类小黄脸是平台 token，不是 BBCode 标签，不能随其它方括号标记
    一起删掉（否则评论区/正文的表情会整段消失），由渲染层查官方目录替换。
    """
    flow = []
    for index, chunk in enumerate(re.split(r"\[img\](.*?)\[/img\]", content, flags=re.I | re.S)):
        if index % 2:
            if url := image_url(chunk, prefix):
                flow.append(("image", url))
        else:
            text = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
            text = re.sub(r"<[^>]*>", "", text)
            # NGA 小黄脸写成 [s:ac:汗]，旧实现把整段方括号标记删掉，表情直接消失。
            # 先临时包住表情 token，再清理其它 BBCode，最后还原 token 原样。
            placeholders = []

            def _stash(matched):
                placeholders.append(matched.group(0))
                return f"\x00{len(placeholders) - 1}\x00"

            text = _EMOTE_TOKEN_RE.sub(_stash, text)
            text = re.sub(r"\[/?[a-zA-Z*][^\]]*\]", "", text)
            for index, token in enumerate(placeholders):
                text = text.replace(f"\x00{index}\x00", token)
            text = unescape(unescape(text))
            if text:
                flow.append(("text", text))
    return flow


class NgaParser(BaseParser):
    platform = Platform("nga", "NGA")

    @handle("bbs.nga.cn", r"https?://bbs\.nga\.cn/read\.php\?[^\s<>\"']+")
    @handle("nga.178.com", r"https?://nga\.178\.com/read\.php\?[^\s<>\"']+")
    @handle("ngabbs.com", r"https?://ngabbs\.com/read\.php\?[^\s<>\"']+")
    async def _handle_thread(self, searched):
        return await self.parse_thread(searched.group(0))

    @staticmethod
    def thread_id(url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ParseException("NGA 主题 URL 无效") from exc
        tids = parse_qs(parsed.query).get("tid", [])
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in HOSTS
                or parsed.username or parsed.password or port not in {None, 80, 443}
                or parsed.path != "/read.php" or len(tids) != 1
                or not re.fullmatch(r"[1-9]\d{0,11}", tids[0])):
            raise ParseException("NGA 仅支持公开主题 read.php?tid")
        return tids[0]

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    async def _fetch_thread(self, tid: str) -> dict:
        if not re.fullmatch(r"[1-9]\d{0,11}", tid):
            raise ParseException("NGA 主题 ID 无效")
        headers = {"X-User-Agent": "NGA_skull/6.0.5(iPhone10,3;iOS 12.0.1)",
                   "Cookie": "guestJs=" + str(int(time.time()))}
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as session:
                async with session.post(
                    "https://ngabbs.com/app_api.php?__lib=post&__act=list",
                    data={"tid": tid, "page": "1"}, headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ParseException("NGA 公开接口请求失败")
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > self.MAX_RESPONSE_BYTES:
                            raise ParseException("NGA 响应超出大小限制")
            data = json.loads(content)
            if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                raise ParseException("NGA 公开接口返回无效数据")
            return data
        except ParseException:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ParseException("NGA 匿名读取失败") from exc

    async def parse_thread(self, url: str):
        tid = self.thread_id(url)
        data = await self._fetch_thread(tid)
        posts = data.get("result", [])
        first = next((p for p in posts if isinstance(p, dict)
                      and str(p.get("pid")) == "0" and str(p.get("lou")) == "0"
                      and str(p.get("tid")) == tid), None)
        if data.get("code") != 0 or first is None:
            raise ParseException("NGA 公开主题不可用或首帖身份不符")
        flow = bbcode_flow(str(first.get("content") or ""), str(data.get("attachPrefix") or ""))
        extra = build_card_extra(flow, extra={"nga_body_flow": flow})
        # 正文里的 [s:ac:汗] 交给渲染层换成官方小黄脸；取不到就按原 token 显示。
        body_text = "".join(v for k, v in flow if k == "text")
        if _EMOTE_TOKEN_RE.search(body_text):
            from ..platform_emotes import fallback_emote_map, select_text_emotes
            emotes = select_text_emotes(body_text, "nga", fallback_emote_map("nga"))
            if emotes:
                extra["card_emotes"] = emotes
        settings = CommentSettings.from_config(self.config, "nga")
        if settings.enabled:
            from .nga_comment import NgaCommentFeed
            feed = NgaCommentFeed(self, limit=settings.display_count)
            async def document_factory():
                try:
                    return await feed.build_document(data, tid)
                except Exception:
                    # Optional comments must never invalidate the public first post.
                    return None
            extra.update(text_card_comment_factory=document_factory,
                         comment_timeout=settings.timeout)
        return self.result(title=str(data.get("tsubject") or first.get("subject") or ""),
                           text="".join(v for k, v in flow if k == "text").strip(),
                           author=self.create_author(str(data.get("tauthor") or
                               (first.get("author") or {}).get("username") or "匿名用户")),
                           timestamp=first.get("postdatetimestamp"),
                           url="https://bbs.nga.cn/read.php?tid=" + tid,
                           contents=self.create_image_contents([v for k, v in flow if k == "image"]),
                           extra=extra)
