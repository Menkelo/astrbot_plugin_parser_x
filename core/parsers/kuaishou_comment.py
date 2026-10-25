from __future__ import annotations

from astrbot.api import logger

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from ..comment_filter import CommentFilter
from .social_comment_feed import SocialCommentFeedBase


class KuaishouCommentFeed(SocialCommentFeedBase):
    """Bounded REST V2 adapter; unknown rich fields deliberately stay empty."""

    COMMENT_URL = "https://www.kuaishou.com/rest/v/photo/comment/list"
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
        parts = []
        for index, line in enumerate(str(raw.get("content") or "").splitlines()):
            if index:
                parts.append(CommentRichPart("line-break"))
            if line:
                parts.append(CommentRichPart("text", line))
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

    async def _request(self, url: str, body: dict) -> dict | None:
        try:
            response = await self.parser.client.post(
                url, json=body, headers=self._headers(), timeout=self.settings.timeout,
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
            return None
        for entry in entries:
            entry.first_reply = await self._first_reply(photo_id, roots[id(entry)], owner_id)
        await self._embed_avatars(entries)
        return CommentDocument(CommentTheme("快手"), "", "",
                               f"{self.count_text(total)} 条评论", entries)
