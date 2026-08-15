from __future__ import annotations

import json
import re
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
from ..utils import normalize_image_url
from .social_comment_feed import SocialCommentFeedBase


@dataclass(slots=True)
class _RawMiyousheFeed:
    items: list[dict]
    total: int
    has_more: bool


class MiyousheCommentFeed(SocialCommentFeedBase):
    COMMENT_URL = "https://bbs-api.miyoushe.com/post/wapi/getPostReplies"
    CACHE_VERSION = "miyoushe_comment_v2_unified_minimal"
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
                    "size": max(20, self.limit),
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
            logger.debug(f"[Miyoushe] 评论接口请求失败: {exc}")
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
    def _rich_text(cls, reply: dict) -> list[CommentRichPart]:
        text = cls._plain_content(reply)
        if not text:
            return []
        output: list[CommentRichPart] = []
        for part in re.split(r"(_\([^()\n]{1,48}\))", text):
            if not part:
                continue
            if part.startswith("_(") and part.endswith(")"):
                output.append(CommentRichPart("emoji-text", text=part))
                continue
            chunks = part.split("\n")
            for index, chunk in enumerate(chunks):
                if index:
                    output.append(CommentRichPart("line-break"))
                if chunk:
                    output.append(CommentRichPart("text", text=chunk))
        return output

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
    ) -> CommentEntry | None:
        reply = item.get("reply") or {}
        content = self._rich_text(reply)
        images = self._images(item)
        if not content and not images:
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

    async def build_images(
        self,
        post_id: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> list[ImageContent]:
        raw_feed = await self.fetch(post_id)
        if not raw_feed.items:
            return []

        owner_text = str(owner_id or "")
        entries = []
        for item in raw_feed.items:
            entry = self.adapt_comment(item, owner_text)
            if entry is None:
                continue
            for reply in item.get("sub_replies") or []:
                if not isinstance(reply, dict):
                    continue
                nested = self.adapt_comment(reply, owner_text, nested=True)
                if nested is not None:
                    entry.first_reply = nested
                    break
            entries.append(entry)
            if len(entries) >= self.limit:
                break
        if not entries:
            return []

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
        return self.render_document(post_id, document)


__all__ = ["MiyousheCommentFeed"]
