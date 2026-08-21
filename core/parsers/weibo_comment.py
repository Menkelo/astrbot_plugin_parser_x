from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from astrbot.api import logger
from msgspec import json as msgjson

from ..comment_canvas import (
    WEIBO_THEME,
    CommentAuthor,
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from ..comment_filter import CommentFilter
from ..comment_settings import CommentFilterSettings
from ..constants import COMMENT_FOOTER_BRAND
from ..data import ImageContent
from ..utils import cached_image_to_data_uri, normalize_image_url


@dataclass(slots=True)
class _RawWeiboFeed:
    items: list[dict]
    total: int
    has_more: bool


class _WeiboRichTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[CommentRichPart] = []
        self._highlight_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "br":
            self.parts.append(CommentRichPart("line-break"))
        elif tag == "a":
            self._highlight_depth += 1
        elif tag == "img":
            url = normalize_image_url(attr_map.get("src")) or ""
            text = attr_map.get("alt") or attr_map.get("title") or ""
            if url:
                self.parts.append(CommentRichPart("emote", text=text, url=url))
            elif text:
                self.parts.append(CommentRichPart("emoji-text", text=text))

    def handle_endtag(self, tag: str):
        if tag == "a" and self._highlight_depth > 0:
            self._highlight_depth -= 1

    def handle_data(self, data: str):
        if data:
            kind = "highlight" if self._highlight_depth else "text"
            self.parts.append(CommentRichPart(kind, text=data))


class WeiboCommentFeed:
    COMMENT_URL = "https://m.weibo.cn/comments/hotflow"
    CACHE_VERSION = "weibo_comment_v13_unified_clean"

    def __init__(
        self,
        parser,
        canvas: SocialCommentCanvas,
        *,
        limit: int = 10,
    ):
        self.parser = parser
        self.canvas = canvas
        self.limit = max(1, int(limit))
        self._avatar_data_uri_cache: dict[str, str | None] = {}
        self.comment_filter = CommentFilter(
            parser,
            CommentFilterSettings.from_config(getattr(parser, "config", {})),
            platform="微博",
            headers=self._headers(),
            referer="https://m.weibo.cn/",
        )

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    def _headers(self) -> dict[str, str]:
        headers = dict(getattr(self.parser, "headers", {}))
        if cookie := getattr(self.parser, "cookie", ""):
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or item.get("mid") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            output.append(item)
        return output

    async def fetch(self, mid: str) -> _RawWeiboFeed:
        items: list[dict] = []
        max_id: int | str = 0
        max_id_type: int | str = 0
        total = 0
        has_more = False

        for _ in range(3):
            params: dict[str, object] = {
                "id": mid,
                "mid": mid,
                "max_id_type": max_id_type,
            }
            if max_id:
                params["max_id"] = max_id
            try:
                response = await self.parser.http_get(
                    self.COMMENT_URL,
                    params=params,
                    headers=self._headers(),
                    allow_redirects=True,
                    timeout=10,
                    retries=1,
                )
                if response.status_code != 200 or not response.content:
                    raise RuntimeError(f"HTTP {response.status_code}")
                payload = msgjson.decode(response.content)
                if not isinstance(payload, dict) or payload.get("ok") != 1:
                    raise RuntimeError(
                        str(
                            payload.get("msg") if isinstance(payload, dict) else payload
                        )
                    )
            except Exception as exc:
                logger.debug(
                    "[评论区][微博] stage=fetch result=failed "
                    f"error={type(exc).__name__}"
                )
                break

            block = payload.get("data") or {}
            page_items = block.get("data") or []
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))
            items = self._dedupe(items)
            try:
                total = int(block.get("total_number") or len(items))
            except (TypeError, ValueError):
                total = len(items)
            next_max_id = block.get("max_id") or 0
            next_max_id_type = block.get("max_id_type") or 0
            has_more = str(next_max_id).strip() not in {"", "0"}
            if len(items) >= max(30, min(60, self.limit * 3)) or not has_more:
                break
            if str(next_max_id) == str(max_id):
                break
            max_id = next_max_id
            max_id_type = next_max_id_type

        return _RawWeiboFeed(items, total or len(items), has_more)

    @staticmethod
    def _count_text(value: object) -> str:
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
    def _time_text(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            created = parsedate_to_datetime(raw).astimezone()
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
        except Exception:
            return raw

    @staticmethod
    def _rich_text(value: object) -> list[CommentRichPart]:
        raw = str(value or "")
        if not raw:
            return []
        parser = _WeiboRichTextParser()
        try:
            parser.feed(raw)
            parser.close()
        except Exception:
            return [CommentRichPart("text", text=unescape(re.sub(r"<[^>]+>", "", raw)))]

        output = []
        for part in parser.parts:
            if part.kind in {"text", "highlight"}:
                text = unescape(part.text)
                if not text:
                    continue
                if output and output[-1].kind == part.kind:
                    output[-1].text += text
                else:
                    output.append(CommentRichPart(part.kind, text=text))
            elif part.kind == "line-break":
                if output and output[-1].kind != "line-break":
                    output.append(part)
            else:
                output.append(part)
        while output and output[-1].kind == "line-break":
            output.pop()
        return output

    @staticmethod
    def _author(item: dict, owner_id: str) -> CommentAuthor:
        user = item.get("user") or {}
        badges = []
        if owner_id and str(user.get("id") or "") == owner_id:
            badges.append(CommentBadge("原博"))
        if user.get("verified"):
            badges.append(CommentBadge("V", "#fff", "#ff8200"))
        try:
            member_rank = int(user.get("mbrank") or 0)
        except (TypeError, ValueError):
            member_rank = 0
        if member_rank > 0:
            badges.append(
                CommentBadge(
                    f"VIP{member_rank}",
                    "#ff8200",
                    "#fff1e4",
                    "#ffd2aa",
                )
            )
        return CommentAuthor(
            nickname=str(user.get("screen_name") or "微博用户"),
            avatar=normalize_image_url(
                user.get("avatar_hd")
                or user.get("avatar_large")
                or user.get("profile_image_url")
            )
            or "",
            nickname_color="#ff8200" if user.get("verified") else "",
            badges=badges,
        )

    @staticmethod
    def _images(item: dict) -> list[str]:
        candidates = []
        for key in ("pics", "pic_infos", "pic"):
            value = item.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.extend(value.values() if key == "pic_infos" else [value])

        output = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = ""
            for key in ("large", "original", "bmiddle", "thumbnail"):
                nested = candidate.get(key)
                if isinstance(nested, dict) and nested.get("url"):
                    url = normalize_image_url(nested.get("url")) or ""
                    break
            if not url:
                url = normalize_image_url(candidate.get("url")) or ""
            if url and url not in seen:
                seen.add(url)
                output.append(url)
        return output

    def adapt_comment(
        self,
        item: dict,
        owner_id: str,
        *,
        nested: bool = False,
    ) -> CommentEntry | None:
        content = self._rich_text(item.get("text"))
        images = self._images(item)
        if not content and not images:
            return None
        try:
            reply_count = int(item.get("total_number") or 0)
        except (TypeError, ValueError):
            reply_count = 0

        first_reply = None
        if not nested:
            replies = item.get("comments") or []
            if isinstance(replies, list) and replies and isinstance(replies[0], dict):
                first_reply = self.adapt_comment(
                    replies[0],
                    owner_id,
                    nested=True,
                )

        source = re.sub(r"^来自", "", str(item.get("source") or "")).strip()
        return CommentEntry(
            author=self._author(item, owner_id),
            content=content,
            images=images,
            time_text=self._time_text(item.get("created_at")),
            location=source,
            like_text=self._count_text(item.get("like_count")),
            reply_text=(
                f"回复 {self._count_text(reply_count)}" if reply_count else "回复"
            ),
            pinned=not nested and bool(item.get("is_top") or item.get("isTop")),
            creator_liked=bool(
                item.get("isLikedByMblogAuthor") or item.get("is_mblog_author_like")
            ),
            first_reply=first_reply,
        )

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
        headers = self._headers()
        headers["Cache-Control"] = "no-cache"
        return await cached_image_to_data_uri(
            self._avatar_data_uri_cache,
            self.parser.http_get,
            avatar,
            headers=headers,
            referer="https://m.weibo.cn/",
            max_bytes=2 * 1024 * 1024,
            timeout=8,
            debug_label="[Weibo] comment avatar",
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

    async def build_document(
        self,
        mid: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> CommentDocument | None:
        raw_feed = await self.fetch(str(mid))
        if not raw_feed.items:
            return None
        candidates = []
        for item in raw_feed.items:
            entry = self.adapt_comment(item, str(owner_id or ""))
            if entry is not None:
                candidates.append(entry)
        entries = await self.comment_filter.apply(candidates, limit=self.limit)
        if not entries:
            return None

        partial = raw_feed.total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=WEIBO_THEME,
            work_title=work_title or "微博",
            cover=normalize_image_url(cover) or "",
            total_text=f"{self._count_text(raw_feed.total)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · 微博评论区"
            ),
        )
        await self._embed_avatars(document.entries)
        return document

    async def build_images(
        self,
        mid: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> list[ImageContent]:
        document = await self.build_document(
            mid,
            work_title=work_title,
            cover=cover,
            owner_id=owner_id,
        )
        if document is None:
            return []
        serialised = json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.CACHE_VERSION}|{serialised}".encode()
        ).hexdigest()[:12]
        out_path = self.cache_dir / f"weibo_comment_{mid}_{digest}.jpg"
        if out_path.is_file() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def render() -> Path:
            await self.canvas.render(out_path, document)
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    render(),
                    name=f"weibo_comment_canvas_{mid}",
                )
            )
        ]


__all__ = ["WeiboCommentFeed"]
