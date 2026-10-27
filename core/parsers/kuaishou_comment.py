from __future__ import annotations

import re

from astrbot.api import logger

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentTheme
from ..comment_filter import CommentFilter
from .social_comment_feed import SocialCommentFeedBase
from .kuaishou_emotes import kuaishou_comment_parts


class KuaishouCommentFeed(SocialCommentFeedBase):
    """Bounded REST V2 adapter with a first-party share REST fallback."""

    COMMENT_URL = "https://www.kuaishou.com/rest/v/photo/comment/list"
    SHARE_COMMENT_URL = "https://v.m.chenzhongtech.com/rest/wd/photo/comment/list"
    SHARE_PAGE_URL = "https://v.m.chenzhongtech.com/fw/photo/"
    AVATAR_REFERER = "https://www.kuaishou.com/"
    PLATFORM_SLUG = "kuaishou"

    def __init__(self, parser):
        self.settings = parser.comment_settings
        super().__init__(parser, limit=self.settings.display_count)
        self.comment_filter = CommentFilter(
            parser, self.settings.filter, platform="快手",
            headers=self._headers(authenticated=False), referer=self.AVATAR_REFERER,
        )

    def _headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {key: value for key, value in self.parser.headers.items()
                   if key.lower() != "cookie"}
        headers["Referer"] = self.AVATAR_REFERER
        if authenticated and (cookie := getattr(self.parser, "kuaishou_cookie", "")):
            headers["Cookie"] = cookie
        return headers

    def _entry(self, raw: dict, owner_id: str) -> CommentEntry:
        author_id = str(raw.get("author_id") or raw.get("authorId") or "")
        parts = kuaishou_comment_parts(str(raw.get("content") or ""))
        try:
            timestamp = int(raw.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return CommentEntry(
            author=CommentAuthor(
                nickname=str(raw.get("author_name") or raw.get("authorName") or "匿名用户"),
                avatar=str(raw.get("headurl") or ""),
                is_up=bool(owner_id and author_id == str(owner_id)),
            ), content=parts, time_text=self.time_text(timestamp) if timestamp else "",
        )

    async def _request(self, url: str, body: dict, *, headers: dict | None = None) -> dict | None:
        try:
            response = await self.parser.client.post(
                url, json=body, headers=self._headers() if headers is None else headers,
                timeout=self.settings.timeout,
                allow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("result") == 1:
                return data
        except Exception:
            # Exceptions and response bodies can contain cookies; never log them.
            logger.debug("[快手] 评论请求失败")
        return None

    async def _first_reply(self, photo_id: str, raw: dict, owner_id: str) -> CommentEntry | None:
        if not raw.get("hasSubComments"):
            return None
        try:
            identity = int(raw.get("comment_id") or raw.get("commentId") or "")
        except (TypeError, ValueError):
            return None
        cursor = ""
        cursors = set()
        for _ in range(2):
            cursors.add(cursor)
            data = await self._request(
                self.COMMENT_URL.replace("/list", "/sublist"),
                {"photoId": photo_id, "rootCommentId": identity, "pcursor": cursor},
            )
            if data is None:
                break
            items = data.get("subCommentsV2") or []
            if not isinstance(items, list):
                break
            replies = await self.comment_filter.apply(
                [self._entry(item, owner_id) for item in items
                 if isinstance(item, dict)], limit=1,
            )
            if replies:
                return replies[0]
            cursor = str(data.get("pcursorV2") or "")
            if not cursor or cursor == "no_more" or cursor in cursors:
                break
        return None

    async def _share_document(self, photo_id: str, owner_id: str) -> CommentDocument | None:
        # Never derive request hosts from input or forward the configured Cookie.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", photo_id):
            return None
        headers = {key: value for key, value in self.parser.ios_headers.items()
                   if key.lower() not in {"cookie", "referer"}}
        headers["Referer"] = self.SHARE_PAGE_URL + photo_id
        cursor = ""
        cursors = set()
        seen = set()
        candidates = []
        previews = {}
        entries = []
        total = 0
        for _ in range(3):
            cursors.add(cursor)
            body = {"photoId": photo_id, "count": 20}
            if cursor:
                body["pcursor"] = cursor
            data = await self._request(self.SHARE_COMMENT_URL, body, headers=headers)
            if data is None:
                break
            total = data.get("commentCount", total)
            items = data.get("rootComments")
            if not isinstance(items, list):
                break
            sub_map = data.get("subCommentsMap")
            if not isinstance(sub_map, dict):
                sub_map = {}
            for raw in items[:20]:
                # Only the observed text schema is supported; no media placeholders.
                if (not isinstance(raw, dict) or raw.get("type") != 0
                        or not isinstance(raw.get("content"), str)):
                    continue
                identity = str(raw.get("comment_id") or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                entry = self._entry(raw, owner_id)
                candidates.append(entry)
                preview = sub_map.get(identity)
                previews[id(entry)] = preview.get("subComments") if isinstance(preview, dict) else None
            entries = await self.comment_filter.apply_keeping_pinned(candidates, limit=self.limit)
            cursor = str(data.get("pcursor") or "")
            if len(entries) >= self.limit or not cursor or cursor == "no_more" or cursor in cursors:
                break
        if not entries:
            return None
        for entry in entries:
            replies = previews[id(entry)]
            if not isinstance(replies, list):
                continue
            # The share response already contains reply previews; do not guess a sublist API.
            filtered = await self.comment_filter.apply(
                [self._entry(raw, owner_id) for raw in replies[:20]
                 if isinstance(raw, dict) and raw.get("type") == 0
                 and isinstance(raw.get("content"), str)], limit=1,
            )
            entry.first_reply = filtered[0] if filtered else None
        await self._embed_avatars(entries)
        return CommentDocument(CommentTheme("快手"), "", "",
                               f"{self.count_text(total)} 条评论", entries)

    async def fetch_document(self, photo_id: str, *, owner_id: str = "") -> CommentDocument | None:
        if not self.settings.enabled or not photo_id:
            return None
        cursor = ""
        cursors = set()
        seen = set()
        candidates = []
        roots = {}
        entries = []
        total = 0
        for _ in range(3):
            cursors.add(cursor)
            data = await self._request(self.COMMENT_URL, {"photoId": photo_id, "pcursor": cursor})
            if data is None:
                break
            total = data.get("commentCountV2", total)
            items = data.get("rootCommentsV2") or []
            if not isinstance(items, list):
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                identity = str(item.get("comment_id") or item.get("commentId") or "")
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                entry = self._entry(item, owner_id)
                candidates.append(entry)
                roots[id(entry)] = item
            entries = await self.comment_filter.apply(candidates, limit=self.limit)
            cursor = str(data.get("pcursorV2") or "")
            if len(entries) >= self.limit or not cursor or cursor == "no_more" or cursor in cursors:
                break
        if not entries:
            return await self._share_document(photo_id, owner_id)
        for entry in entries:
            entry.first_reply = await self._first_reply(photo_id, roots[id(entry)], owner_id)
        await self._embed_avatars(entries)
        return CommentDocument(CommentTheme("快手"), "", "",
                               f"{self.count_text(total)} 条评论", entries)
