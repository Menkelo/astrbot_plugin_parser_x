from __future__ import annotations

import re
from html import unescape

from ..comment_canvas import (
    XIAOHEIHE_THEME,
    CommentAuthor,
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from ..constants import COMMENT_FOOTER_BRAND
from ..data import ImageContent
from ..platform_emotes import (
    fallback_emote_map,
    iter_emote_matches,
    load_platform_emotes,
)
from ..utils import normalize_image_url
from .social_comment_feed import SocialCommentFeedBase


class XiaoheiheCommentFeed(SocialCommentFeedBase):
    CACHE_VERSION = "xiaoheihe_comment_v7_unified_clean"
    PLATFORM_SLUG = "xiaoheihe"
    AVATAR_REFERER = "https://www.xiaoheihe.cn/"

    def __init__(
        self,
        parser,
        canvas: SocialCommentCanvas,
        *,
        limit: int = 10,
    ):
        super().__init__(parser, canvas, limit=limit)

    @staticmethod
    def _plain(value: object) -> str:
        text = str(value or "")
        text = re.sub(r"<br\s*/?>|</(?:p|div|li)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return unescape(text).strip()

    @classmethod
    def _rich_text(
        cls,
        value: object,
        emote_map: dict[str, str] | None = None,
    ) -> list[CommentRichPart]:
        text = cls._plain(value)
        if not text:
            return []
        output: list[CommentRichPart] = []

        def append_text(value_part: str) -> None:
            chunks = value_part.split("\n")
            for index, chunk in enumerate(chunks):
                if index:
                    output.append(CommentRichPart("line-break"))
                if chunk:
                    output.append(CommentRichPart("text", text=chunk))

        catalog = emote_map or fallback_emote_map("xiaoheihe")
        last = 0
        for start, end, token, url in iter_emote_matches(
            text,
            "xiaoheihe",
            catalog,
        ):
            append_text(text[last:start])
            output.append(
                CommentRichPart(
                    "emote" if url else "emoji-text",
                    text=token,
                    url=url,
                )
            )
            last = end
        append_text(text[last:])
        return output

    @staticmethod
    def _author(item: dict, owner_id: str) -> CommentAuthor:
        user = item.get("user") or {}
        user_id = str(user.get("userid") or item.get("userid") or "")
        badges = []
        if item.get("is_link_owner") or owner_id and user_id == owner_id:
            badges.append(CommentBadge("作者"))
        level_info = user.get("level_info") or {}
        try:
            level = int(level_info.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        if level > 0:
            badges.append(
                CommentBadge(
                    f"Lv.{level}",
                    "#ff6a00",
                    "#fff0e5",
                    "#ffd4b3",
                )
            )
        return CommentAuthor(
            nickname=str(user.get("username") or "小黑盒用户"),
            avatar=normalize_image_url(user.get("avatar") or user.get("avartar")) or "",
            nickname_color="#ff6a00" if badges and badges[0].text == "作者" else "",
            badges=badges,
        )

    @staticmethod
    def _images(item: dict) -> list[str]:
        output = []
        for image in item.get("imgs") or []:
            if isinstance(image, str):
                value = image
            elif isinstance(image, dict):
                value = image.get("url") or image.get("src")
            else:
                continue
            if normalized := normalize_image_url(value):
                output.append(normalized)
        return list(dict.fromkeys(output))

    def adapt_comment(
        self,
        item: dict,
        owner_id: str,
        *,
        nested: bool = False,
        emote_map: dict[str, str] | None = None,
    ) -> CommentEntry | None:
        content = self._rich_text(item.get("text"), emote_map)
        images = self._images(item)
        if not content and not images:
            return None

        reply_user = item.get("replyuser") or {}
        if nested and reply_user.get("username"):
            content = [
                CommentRichPart(
                    "highlight",
                    text=f"回复 @{reply_user['username']}：",
                ),
                *content,
            ]
        try:
            reply_count = int(item.get("child_num") or 0)
        except (TypeError, ValueError):
            reply_count = 0
        return CommentEntry(
            author=self._author(item, owner_id),
            content=content,
            images=images,
            time_text=self.time_text(item.get("create_at")),
            location=str(item.get("ip_location") or ""),
            like_text=self.count_text(item.get("up")),
            reply_text=(
                f"回复 {self.count_text(reply_count)}" if reply_count else "回复"
            ),
            pinned=not nested and bool(item.get("is_top")),
        )

    def _build_document(
        self,
        link_id: str,
        threads: list[dict],
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
        total: int | None = None,
        emote_map: dict[str, str] | None = None,
    ) -> CommentDocument | None:
        def floor_number(thread: dict) -> int:
            comments = thread.get("comment")
            if not isinstance(comments, list) or not comments:
                return 0
            first = comments[0]
            if not isinstance(first, dict):
                return 0
            try:
                return int(first.get("floor_num") or 0)
            except (TypeError, ValueError):
                return 0

        owner_text = str(owner_id or "")
        ordered = [item for item in threads if isinstance(item, dict)]
        ordered.sort(key=floor_number)

        entries = []
        raw_count = 0
        for thread in ordered:
            comments = [
                item for item in thread.get("comment") or [] if isinstance(item, dict)
            ]
            raw_count += len(comments)
            if not comments:
                continue
            entry = self.adapt_comment(
                comments[0],
                owner_text,
                emote_map=emote_map,
            )
            if entry is None:
                continue
            for reply in comments[1:]:
                nested = self.adapt_comment(
                    reply,
                    owner_text,
                    nested=True,
                    emote_map=emote_map,
                )
                if nested is not None:
                    entry.first_reply = nested
                    break
            entries.append(entry)

        if not entries:
            return None
        total_value = max(int(total or 0), raw_count, len(entries))
        partial = total_value > len(entries)
        document = CommentDocument(
            theme=XIAOHEIHE_THEME,
            work_title=work_title or "小黑盒帖子",
            cover=normalize_image_url(cover) or "",
            total_text=f"{self.count_text(total_value)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · 小黑盒评论区"
            ),
        )
        return document

    async def build_document(
        self,
        link_id: str,
        threads: list[dict],
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
        total: int | None = None,
    ) -> CommentDocument | None:
        emote_map = await load_platform_emotes(self.parser, "xiaoheihe")
        document = self._build_document(
            link_id,
            threads,
            work_title=work_title,
            cover=cover,
            owner_id=owner_id,
            total=total,
            emote_map=emote_map,
        )
        if document is not None:
            original_count = len(document.entries)
            document.entries = await self.comment_filter.apply(
                document.entries,
                limit=self.limit,
            )
            if not document.entries:
                return None
            if len(document.entries) < original_count:
                document.footer_text = (
                    f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                )
            await self._embed_avatars(document.entries)
        return document

    async def build_images(
        self,
        link_id: str,
        threads: list[dict],
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
        total: int | None = None,
    ) -> list[ImageContent]:
        document = await self.build_document(
            link_id,
            threads,
            work_title=work_title,
            cover=cover,
            owner_id=owner_id,
            total=total,
        )
        if document is None:
            return []
        return self.render_document(link_id, document)


__all__ = ["XiaoheiheCommentFeed"]
