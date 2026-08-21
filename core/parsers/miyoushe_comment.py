from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from astrbot.api import logger

from ..comment_canvas import (
    MIYOUSHE_THEME,
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


@dataclass(slots=True)
class _RawMiyousheFeed:
    items: list[dict]
    total: int
    has_more: bool


class MiyousheCommentFeed(SocialCommentFeedBase):
    COMMENT_URL = "https://bbs-api.miyoushe.com/post/wapi/getPostReplies"
    CACHE_VERSION = "miyoushe_comment_v6_unified_clean"
    PLATFORM_SLUG = "miyoushe"
    AVATAR_REFERER = "https://www.miyoushe.com/"

    def __init__(
        self,
        parser,
        canvas: SocialCommentCanvas,
        *,
        limit: int = 10,
    ):
        super().__init__(parser, canvas, limit=limit)

    async def fetch(self, post_id: str) -> _RawMiyousheFeed:
        try:
            response = await self.parser.http_get(
                self.COMMENT_URL,
                params={
                    "post_id": post_id,
                    "is_hot": "true",
                    "size": max(30, min(60, self.limit * 3)),
                },
                headers=self.parser.headers,
                timeout=10,
                retries=1,
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("retcode") not in (0, None):
                raise RuntimeError(
                    str(
                        payload.get("message") if isinstance(payload, dict) else payload
                    )
                )
        except Exception as exc:
            logger.debug(
                "[评论区][米游社] stage=fetch result=failed "
                f"error={type(exc).__name__}"
            )
            return _RawMiyousheFeed([], 0, False)

        data = payload.get("data") or {}
        items = [item for item in data.get("list") or [] if isinstance(item, dict)]
        try:
            total = int(data.get("total_reply_num") or len(items))
        except (TypeError, ValueError):
            total = len(items)
        return _RawMiyousheFeed(
            items=items,
            total=total,
            has_more=not bool(data.get("is_last", True)),
        )

    @staticmethod
    def _plain_content(reply: dict) -> str:
        content = str(reply.get("content") or "").strip()
        if content:
            return content
        raw = str(reply.get("struct_content") or "").strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        parts = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and isinstance(item.get("insert"), str):
                parts.append(item["insert"])
        return "".join(parts).strip()

    @classmethod
    def _rich_text(
        cls,
        reply: dict,
        emote_map: dict[str, str] | None = None,
    ) -> list[CommentRichPart]:
        output: list[CommentRichPart] = []

        def append_text(value: str) -> None:
            last = 0
            for start, end, token, url in iter_emote_matches(
                value,
                "miyoushe",
                catalog,
            ):
                append_plain(value[last:start])
                output.append(
                    CommentRichPart(
                        "emote" if url else "emoji-text",
                        text=token,
                        url=url,
                    )
                )
                last = end
            append_plain(value[last:])

        def append_plain(value: str) -> None:
            chunks = value.split("\n")
            for index, chunk in enumerate(chunks):
                if index:
                    output.append(CommentRichPart("line-break"))
                if chunk:
                    output.append(CommentRichPart("text", text=chunk))

        catalog = emote_map or fallback_emote_map("miyoushe")
        raw = str(reply.get("struct_content") or "").strip()
        if raw:
            try:
                structured = json.loads(raw)
            except (TypeError, ValueError):
                structured = None
            if isinstance(structured, list):
                handled = False
                for item in structured:
                    if not isinstance(item, dict):
                        continue
                    insert = item.get("insert")
                    if isinstance(insert, str):
                        append_text(insert)
                        handled = True
                        continue
                    if not isinstance(insert, dict):
                        continue
                    custom = insert.get("custom_emoticon")
                    if not isinstance(custom, dict):
                        continue
                    alt = str(insert.get("backup_text") or "[自定义表情]")
                    url = normalize_image_url(custom.get("url")) or ""
                    if not url:
                        output.append(CommentRichPart("emoji-text", text=alt))
                    handled = True
                if handled:
                    return output

        text = cls._plain_content(reply)
        if text:
            append_text(text)
        return output

    @staticmethod
    def _custom_sticker(reply: dict) -> str:
        raw = str(reply.get("struct_content") or "").strip()
        if not raw:
            return ""
        try:
            structured = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        for item in structured if isinstance(structured, list) else []:
            insert = item.get("insert") if isinstance(item, dict) else None
            custom = insert.get("custom_emoticon") if isinstance(insert, dict) else None
            if not isinstance(custom, dict):
                continue
            if url := normalize_image_url(custom.get("url")):
                return url
        return ""

    @staticmethod
    def _author(item: dict, owner_id: str) -> CommentAuthor:
        user = item.get("user") or {}
        user_id = str(user.get("uid") or "")
        badges = []
        if item.get("is_lz") or owner_id and user_id == owner_id:
            badges.append(CommentBadge("楼主"))
        certification = user.get("certification") or {}
        cert_label = str(certification.get("label") or "").strip()
        if cert_label:
            badges.append(
                CommentBadge(cert_label[:12], "#4c8df6", "#eaf2ff", "#cfe0ff")
            )
        level_exp = user.get("level_exp") or {}
        try:
            level = int(level_exp.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        if level > 0:
            badges.append(
                CommentBadge(
                    f"Lv.{level}",
                    "#4c8df6",
                    "#eaf2ff",
                    "#cfe0ff",
                )
            )
        return CommentAuthor(
            nickname=str(user.get("nickname") or "米游社用户"),
            avatar=normalize_image_url(user.get("avatar_url") or user.get("avatar"))
            or "",
            nickname_color="#4c8df6" if badges and badges[0].text == "楼主" else "",
            badges=badges,
        )

    @staticmethod
    def _images(item: dict) -> list[str]:
        output = []
        for image in item.get("images") or []:
            if isinstance(image, str):
                value = image
            elif isinstance(image, dict):
                value = image.get("url") or image.get("image_url")
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
        reply = item.get("reply") or {}
        content = self._rich_text(reply, emote_map)
        images = self._images(item)
        sticker = self._custom_sticker(reply)
        if not content and not images and not sticker:
            return None

        reply_user = item.get("r_user") or {}
        if nested and reply_user.get("nickname"):
            content = [
                CommentRichPart(
                    "highlight",
                    text=f"回复 @{reply_user['nickname']}：",
                ),
                *content,
            ]
        stat = item.get("stat") or {}
        try:
            reply_count = int(stat.get("sub_num") or item.get("sub_reply_count") or 0)
        except (TypeError, ValueError):
            reply_count = 0
        return CommentEntry(
            author=self._author(item, owner_id),
            content=content,
            images=images,
            sticker_image=sticker,
            time_text=self.time_text(
                reply.get("updated_at") or reply.get("created_at")
            ),
            location=str((item.get("user") or {}).get("ip_region") or ""),
            like_text=self.count_text(stat.get("like_num")),
            reply_text=(
                f"回复 {self.count_text(reply_count)}" if reply_count else "回复"
            ),
            pinned=not nested and bool(reply.get("is_top")),
        )

    async def build_document(
        self,
        post_id: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> CommentDocument | None:
        raw_feed, emote_map = await asyncio.gather(
            self.fetch(post_id),
            load_platform_emotes(self.parser, "miyoushe"),
        )
        if not raw_feed.items:
            return None

        owner_text = str(owner_id or "")
        candidates = []
        for item in raw_feed.items:
            entry = self.adapt_comment(item, owner_text, emote_map=emote_map)
            if entry is None:
                continue
            for reply in item.get("sub_replies") or []:
                if not isinstance(reply, dict):
                    continue
                nested = self.adapt_comment(
                    reply,
                    owner_text,
                    nested=True,
                    emote_map=emote_map,
                )
                if nested is not None:
                    entry.first_reply = nested
                    break
            candidates.append(entry)
        entries = await self.comment_filter.apply(candidates, limit=self.limit)
        if not entries:
            return None

        partial = raw_feed.total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=MIYOUSHE_THEME,
            work_title=work_title or "米游社文章",
            cover=normalize_image_url(cover) or "",
            total_text=f"{self.count_text(raw_feed.total)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · 米游社评论区"
            ),
        )
        await self._embed_avatars(document.entries)
        return document

    async def build_images(
        self,
        post_id: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> list[ImageContent]:
        document = await self.build_document(
            post_id,
            work_title=work_title,
            cover=cover,
            owner_id=owner_id,
        )
        if document is None:
            return []
        return self.render_document(post_id, document)


__all__ = ["MiyousheCommentFeed"]
