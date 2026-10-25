from __future__ import annotations

import re
from html import unescape

from ..comment_canvas import (
    CommentAuthor,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    CommentTheme,
)
from ..comment_filter import CommentFilter
from ..utils import normalize_image_url
from .social_comment_feed import SocialCommentFeedBase


class AcFunCommentFeed(SocialCommentFeedBase):
    """Public or authenticated AcFun comments for the shared renderer."""

    COMMENT_URL = "https://www.acfun.cn/rest/pc-direct/comment/list"
    AVATAR_REFERER = "https://www.acfun.cn/"
    PLATFORM_SLUG = "acfun"

    def __init__(self, parser):
        self.settings = parser.comment_settings
        super().__init__(parser, limit=self.settings.display_count)
        self.comment_filter = CommentFilter(
            parser, self.settings.filter, platform="AcFun",
            headers=self._headers(authenticated=False), referer=self.AVATAR_REFERER,
        )

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        # Only the explicit AcFun setting may authenticate first-party API calls.
        headers = {key: value for key, value in self.parser.headers.items()
                   if key.lower() != "cookie"}
        if authenticated and (cookie := getattr(self.parser, "acfun_cookie", "")):
            headers["Cookie"] = cookie
        headers["Referer"] = self.AVATAR_REFERER
        return headers

    def _entry(self, raw: dict) -> CommentEntry:
        heads = raw.get("headUrl") or []
        avatar = heads[0] if isinstance(heads, list) and heads else heads
        if isinstance(avatar, dict):
            avatar = avatar.get("url")
        avatar = normalize_image_url(avatar) if isinstance(avatar, str) else ""
        images = []
        image_url = raw.get("imageUrl")
        if isinstance(image_url, str) and (url := normalize_image_url(image_url)):
            images.append(url)
        text = unescape(str(raw.get("content") or ""))
        def image(match):
            if url := normalize_image_url(match.group(1).strip()):
                images.append(url)
            return ""
        text = re.sub(r"\[img(?:=[^\]]*)?\](.*?)\[/img\]", image, text, flags=re.I | re.S)
        text = re.sub(r"\[url(?:=[^\]]*)?\](.*?)\[/url\]", r"\1", text, flags=re.I | re.S)
        text = re.sub(r"\[/?(?:b|i|u|s|color|size|font)(?:=[^\]]*)?\]", "", text, flags=re.I)
        # Pure sticker reactions are not useful discussion. Keep mixed text and
        # real image comments; opaque IDs must not count as meaningful words.
        if not re.sub(r"\[emot=acfun,\d+/\]", "", text, flags=re.I).strip():
            text = ""
        target = str(raw.get("replyToUserName") or "").strip()
        if target and text:
            text = f"回复 {target}：{text}"
        parts = []
        for index, line in enumerate(text.splitlines()):
            if index:
                parts.append(CommentRichPart("line-break"))
            if line:
                parts.append(CommentRichPart("text", line))
        return CommentEntry(
            author=CommentAuthor(
                nickname=raw.get("userName") or "匿名用户", avatar=avatar,
                is_up=bool(raw.get("isUp")),
            ),
            content=parts, images=list(dict.fromkeys(images)),
            time_text=str(raw.get("postDate") or ""),
            pinned=bool(raw.get("isSticky")),
            like_text=self.count_text(raw.get("likeCount")),
        )

    async def _resolve_emotes(self, entries: list[CommentEntry]) -> None:
        pattern = re.compile(r"\[emot=acfun,(\d+)/\]", re.I)
        if not any(pattern.search(part.text or "") for entry in entries for part in entry.content):
            return
        if not hasattr(self, "_emote_catalog"):
            try:
                response = await self.parser.client.post(
                    "https://www.acfun.cn/rest/pc-direct/emotion/getUserEmotion",
                    headers=self._headers(), timeout=15,
                )
                payload = response.json()
                if payload.get("result") != 0:
                    return
                catalog = {}
                for package in payload.get("emotionPackageList") or []:
                    for emotion in package.get("emotions") or []:
                        urls = ((emotion.get("bigImageInfo") or {}).get("thumbnailImage") or {}).get("cdnUrls") or []
                        if urls and (url := normalize_image_url(urls[0].get("url") or "")):
                            catalog[str(emotion.get("id"))] = url
                self._emote_catalog = catalog
            except Exception:
                # A transient catalog failure must not suppress valid comments.
                return
        for entry in entries:
            parts = []
            for part in entry.content:
                if part.kind != "text":
                    parts.append(part)
                    continue
                position = 0
                for match in pattern.finditer(part.text):
                    if match.start() > position:
                        parts.append(CommentRichPart("text", part.text[position:match.start()]))
                    url = self._emote_catalog.get(match.group(1), "")
                    parts.append(CommentRichPart("emote" if url else "text", match.group(0), url))
                    position = match.end()
                if position < len(part.text):
                    parts.append(CommentRichPart("text", part.text[position:]))
            entry.content = parts

    async def _first_reply(self, items: list) -> CommentEntry | None:
        candidates = [self._entry(item) for item in items if isinstance(item, dict)]
        usable = await self.comment_filter.apply(candidates, limit=1)
        return usable[0] if usable else None

    async def _populate_reply(self, source_id: str, identity: str, entry: CommentEntry,
                              raw: dict, block: dict) -> None:
        entry.first_reply = await self._first_reply(block.get("subComments") or [])
        if entry.first_reply is not None:
            return
        cursor = block.get("pcursor") or "0"
        if str(cursor) == "no_more" or not (raw.get("subCommentCount") or block):
            return
        seen_cursors = set()
        # Public sublist uses page; pcursor alone is ignored by this endpoint.
        for page in range(1, 3):
            if str(cursor) in seen_cursors:
                break
            seen_cursors.add(str(cursor))
            response = await self.parser.http_get(
                self.COMMENT_URL.replace("/list", "/sublist"),
                headers=self._headers(), timeout=self.settings.timeout, retries=1,
                params={"sourceId": source_id, "sourceType": 3,
                        "rootCommentId": identity, "pcursor": cursor, "page": page},
            )
            data = response.json()
            if not isinstance(data, dict) or data.get("result") != 0:
                break
            entry.first_reply = await self._first_reply(data.get("subComments") or [])
            cursor = data.get("pcursor")
            if entry.first_reply is not None or str(cursor) in {"None", "", "0", "no_more"}:
                break

    async def fetch_document(self, source_id: str) -> CommentDocument | None:
        if not self.settings.enabled:
            return None
        candidates = []
        roots = {}
        replies = {}
        seen = set()
        total = 0
        entries = []
        for page in range(1, 4):
            response = await self.parser.http_get(
                self.COMMENT_URL, headers=self._headers(), timeout=self.settings.timeout,
                retries=1, params={"sourceId": source_id, "sourceType": 3, "page": page,
                                   "pivotCommentId": 0, "newPivotCommentId": 0},
            )
            data = response.json()
            if not isinstance(data, dict) or data.get("result") != 0:
                break
            total = data.get("commentCount", total)
            replies.update(data.get("subCommentsMap") or {})
            for section in ("stickyComments", "hotComments", "godComments", "rootComments"):
                for item in data.get(section) or []:
                    if not isinstance(item, dict):
                        continue
                    identity = str(item.get("commentId") or "")
                    if identity and identity in seen:
                        continue
                    if identity:
                        seen.add(identity)
                    entry = self._entry(item)
                    entry.pinned = entry.pinned or section == "stickyComments"
                    candidates.append(entry)
                    roots[id(entry)] = (identity, item)
            entries = await self.comment_filter.apply(candidates, limit=self.limit)
            if (len(entries) >= self.limit or page >= int(data.get("totalPage") or 1)
                    or data.get("pcursor") == "no_more"):
                break
        if not entries:
            return None
        for entry in entries:
            identity, raw = roots[id(entry)]
            await self._populate_reply(source_id, identity, entry, raw, replies.get(identity) or {})
        await self._resolve_emotes(entries)
        await self._resolve_emotes([entry.first_reply for entry in entries if entry.first_reply is not None])
        return CommentDocument(
            theme=CommentTheme("AcFun"), work_title="", cover="",
            total_text=f"{self.count_text(total)} 条评论",
            entries=entries,
        )


__all__ = ["AcFunCommentFeed"]
