from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..comment_canvas import CommentDocument, CommentEntry, SocialCommentCanvas
from ..data import ImageContent
from ..utils import cached_image_to_data_uri


class SocialCommentFeedBase:
    CACHE_VERSION = "social_comment_v1"
    PLATFORM_SLUG = "social"
    AVATAR_REFERER = ""

    def __init__(self, parser, canvas: SocialCommentCanvas, *, limit: int = 10):
        self.parser = parser
        self.canvas = canvas
        self.limit = max(1, int(limit))
        self._avatar_data_uri_cache: dict[str, str | None] = {}

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    @staticmethod
    def count_text(value: object) -> str:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number >= 100_000_000:
            return f"{number / 100_000_000:.1f}".removesuffix(".0") + "亿"
        if number >= 10_000:
            return f"{number / 10_000:.1f}".removesuffix(".0") + "万"
        return str(number)

    @staticmethod
    def time_text(value: object) -> str:
        try:
            created = datetime.fromtimestamp(int(value)).astimezone()
        except (TypeError, ValueError, OSError, OverflowError):
            return str(value or "").strip()

        now = datetime.now().astimezone()
        delta = max(0, int((now - created).total_seconds()))
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{delta // 60}分钟前"
        if created.date() == now.date():
            return f"今天{created:%H:%M}"
        if delta < 365 * 86400:
            return f"{created:%m月%d日 %H:%M}"
        return f"{created:%Y年%m月%d日}"

    @staticmethod
    def _walk_entries(entries: list[CommentEntry]) -> list[CommentEntry]:
        output = []
        pending = list(entries)
        while pending:
            entry = pending.pop(0)
            output.append(entry)
            if entry.first_reply is not None:
                pending.append(entry.first_reply)
        return output

    async def _avatar_to_data_uri(self, avatar: str) -> str | None:
        headers = self.parser.headers.copy()
        headers["Cache-Control"] = "no-cache"
        return await cached_image_to_data_uri(
            self._avatar_data_uri_cache,
            self.parser.http_get,
            avatar,
            headers=headers,
            referer=self.AVATAR_REFERER or None,
            max_bytes=2 * 1024 * 1024,
            timeout=8,
            debug_label=f"[{self.PLATFORM_SLUG}] comment avatar",
        )

    async def _embed_avatars(self, entries: list[CommentEntry]) -> None:
        all_entries = self._walk_entries(entries)
        avatar_urls = list(
            dict.fromkeys(
                entry.author.avatar
                for entry in all_entries
                if entry.author.avatar and not entry.author.avatar.startswith("data:")
            )
        )
        if not avatar_urls:
            return

        data_uris = await asyncio.gather(
            *(self._avatar_to_data_uri(url) for url in avatar_urls)
        )
        resolved = dict(zip(avatar_urls, data_uris, strict=True))
        for entry in all_entries:
            if data_uri := resolved.get(entry.author.avatar):
                entry.author.avatar = data_uri

    def render_document(
        self,
        cache_key: str,
        document: CommentDocument,
    ) -> list[ImageContent]:
        serialised = json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.CACHE_VERSION}|{serialised}".encode()
        ).hexdigest()[:12]
        safe_key = "".join(char for char in str(cache_key) if char.isalnum())[:32]
        out_path = self.cache_dir / (
            f"{self.PLATFORM_SLUG}_comment_{safe_key or 'item'}_{digest}.jpg"
        )
        if out_path.is_file() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def render() -> Path:
            await self._embed_avatars(document.entries)
            await self.canvas.render(out_path, document)
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    render(),
                    name=f"{self.PLATFORM_SLUG}_comment_canvas_{safe_key or 'item'}",
                )
            )
        ]


__all__ = ["SocialCommentFeedBase"]
