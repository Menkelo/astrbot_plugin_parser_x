"""Anonymous first-party hot comments adapted to the existing shared pipeline."""
from __future__ import annotations

import asyncio

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from ..comment_settings import CommentSettings
from .social_comment_feed import SocialCommentFeedBase


class NeteaseCommentFeed(SocialCommentFeedBase):
    PLATFORM_SLUG = "netease"
    AVATAR_REFERER = "https://music.163.com/"

    async def fetch_document(self, song_id: str, title: str, cover: str) -> CommentDocument | None:
        settings = CommentSettings.from_config(self.parser.config, "netease")
        if not settings.enabled:
            return None
        try:
            async with asyncio.timeout(settings.timeout):
                data = await self.parser._request_json(
                    f"/api/v1/resource/hotcomments/R_SO_4_{song_id}",
                    {"limit": min(60, settings.display_count * 3), "offset": 0},
                )
                if data.get("code") != 200:
                    return None
                entries = []
                for raw in (data.get("hotComments") or [])[:60]:
                    if not isinstance(raw, dict):
                        continue
                    user = raw.get("user") or {}
                    if not isinstance(user, dict):
                        user = {}
                    entries.append(CommentEntry(
                        author=CommentAuthor(str(user.get("nickname") or "匿名听众"),
                            avatar=self.parser._media_url(user.get("avatarUrl"))),
                        content=[CommentRichPart("text", str(raw.get("content") or "")[:8000])],
                        like_text=self.count_text(raw.get("likedCount")),
                        time_text=self.time_text(float(raw.get("time") or 0) / 1000),
                    ))
                entries = await self.comment_filter.apply(entries, limit=settings.display_count)
                if not entries:
                    return None
                return CommentDocument(CommentTheme("网易云音乐"), title, cover,
                    f"{self.count_text(data.get('total'))} 条评论", entries)
        except Exception:
            # Comments are optional; never affect already constructed audio/detail.
            return None
