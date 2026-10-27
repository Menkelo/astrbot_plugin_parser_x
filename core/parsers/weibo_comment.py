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
from urllib.parse import parse_qs, urlsplit

from astrbot.api import logger
from msgspec import json as msgjson

from ..comment_canvas import (
    WEIBO_THEME,
    CommentAuthor,
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
)
from ..comment_filter import CommentFilter
from ..comment_settings import CommentFilterSettings
from ..data import ImageContent
from ..utils import cached_image_to_data_uri, normalize_image_url

# 楼中楼一次最多取几条回复：微博接口不保证顺序，多取只会拉长渲染时间。
_REPLY_FETCH_LIMIT = 5


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
        self._picture_link = False
        self.images: list[str] = []

    @staticmethod
    def _picture_target(href: str) -> str | None:
        parsed = urlsplit(href)
        if parsed.scheme != 'https' or parsed.hostname != 'weibo.cn' or parsed.path != '/sinaurl':
            return None
        target = parse_qs(parsed.query).get('u', [''])[0]
        image = urlsplit(target)
        host = image.hostname or ''
        if (image.scheme == 'https' and not image.username and not image.password
                and host.endswith('.sinaimg.cn')
                and re.fullmatch(r'/large/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|gif|webp)', image.path, re.I)):
            return target
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "br":
            self.parts.append(CommentRichPart("line-break"))
        elif tag == "a":
            self._highlight_depth += 1
            picture = self._picture_target(attr_map.get('href', ''))
            self._picture_link = bool(picture)
            if picture and picture not in self.images:
                self.images.append(picture)
        elif tag == "img":
            if self._picture_link:
                return
            url = normalize_image_url(attr_map.get("src")) or ""
            text = attr_map.get("alt") or attr_map.get("title") or ""
            if url:
                self.parts.append(CommentRichPart("emote", text=text, url=url))
            elif text:
                self.parts.append(CommentRichPart("emoji-text", text=text))

    def handle_endtag(self, tag: str):
        if tag == "a" and self._highlight_depth > 0:
            self._highlight_depth -= 1
            self._picture_link = False

    def handle_data(self, data: str):
        if data and not self._picture_link:
            kind = "highlight" if self._highlight_depth else "text"
            self.parts.append(CommentRichPart(kind, text=data))


class WeiboCommentFeed:
    COMMENT_URL = "https://m.weibo.cn/comments/hotflow"
    CACHE_VERSION = "weibo_comment_v19_reply_picture_links"

    def __init__(self, parser, *, limit: int = 10):
        self.parser = parser
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
        # 解析器历史上 `cookie` / `cookies` 两种属性名都出现过，都认。
        cookie = getattr(self.parser, "cookie", "") or getattr(
            self.parser, "cookies", ""
        )
        if cookie:
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
            params = {
                "id": mid,
                "mid": mid,
                "max_id": max_id,
                "max_id_type": max_id_type,
            }
            try:
                resp = await self.parser.http_get(
                    self.COMMENT_URL,
                    params=params,
                    headers=self._headers(),
                    timeout=10,
                )
                data = msgjson.decode(resp.content)
            except Exception as exc:
                logger.warning(f"[WeiboComment] fetch failed: {exc}")
                break

            if not isinstance(data, dict):
                break
            if data.get("ok") != 1:
                break
            page_data = data.get("data", {})
            page_items = page_data.get("data", [])
            if not page_items:
                break
            items.extend(page_items)
            total = page_data.get("total_number", 0)
            max_id = page_data.get("max_id", 0)
            max_id_type = page_data.get("max_id_type", 0)
            # 微博游标既可能是 int 0，也可能是字符串 "0"；直接和 0 比会
            # 把 "0" 当成"还有下一页"再空翻两轮。
            has_more = str(max_id).strip() not in {"", "0"}
            if not has_more:
                break

        return _RawWeiboFeed(items=items, total=total, has_more=has_more)

    def _parse_user(self, user: dict) -> CommentAuthor:
        nickname = str(user.get("screen_name") or "").strip()
        # 微博接口不同端点分别给 profile_image_url / avatar_large，两个都要认。
        avatar = str(
            user.get("profile_image_url")
            or user.get("avatar_large")
            or user.get("avatar_hd")
            or ""
        ).strip()
        verified_type = user.get("verified_type", -1)
        verified_reason = str(user.get("verified_reason") or "").strip()
        badges: list[CommentBadge] = []
        verified = bool(user.get("verified")) or verified_type in {
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            220,
        }
        if verified:
            # 认证用户先挂金色「V」；接口给了认证说明时用它代替通用 V。
            badges.append(
                CommentBadge(
                    text=verified_reason or "V",
                    background="#3a2b16",
                    foreground="#e8b45f",
                    border="#e8b45f",
                )
            )
        # mbrank 是微博会员等级（1~7），大于 0 才显示 VIP 徽章。
        try:
            mbrank = int(user.get("mbrank") or 0)
        except (TypeError, ValueError):
            mbrank = 0
        if mbrank > 0:
            badges.append(
                CommentBadge(
                    text=f"VIP{mbrank}",
                    background="#3a2b16",
                    foreground="#e8b45f",
                    border="#e8b45f",
                )
            )
        # 原博（发帖人自己回复）单独用「原博」标记，和认证/会员共处一行。
        return CommentAuthor(nickname=nickname, avatar=avatar, badges=badges)

    @staticmethod
    def _matches_owner(
        user: dict,
        owner: dict | None,
        work_author_id: object = None,
    ) -> bool:
        """判断这条评论是不是作品作者本人发的。

        微博调用方有的传 ``owner={'id': ...}``，有的只传作者 id 本体，
        还有传空 dict 的；三种都要能认，否则「原博」标记会随机丢失。
        """
        user_id = user.get("id")
        if user_id is None:
            return False
        if isinstance(owner, dict):
            owner_id = owner.get("id")
        else:
            owner_id = owner
        if owner_id in (None, "") and work_author_id is not None:
            owner_id = work_author_id
        if owner_id in (None, ""):
            return False
        return str(user_id) == str(owner_id)

    def _parse_text(self, text: str) -> list[CommentRichPart]:
        parser = _WeiboRichTextParser()
        parser.feed(text)
        return parser.parts

    def _parse_images(self, pics: list[dict] | None) -> list[str]:
        if not pics:
            return []
        urls = []
        for pic in pics:
            if not isinstance(pic, dict) or pic.get('type') == 'video':
                continue
            url = (
                normalize_image_url((pic.get("large") or {}).get("url"))
                or normalize_image_url(pic.get("url"))
                or ""
            )
            if url:
                urls.append(url)
        return urls

    @staticmethod
    def _sort_key(item: dict) -> tuple[int, int]:
        likes = int(item.get("like_count") or 0)
        raw_created = item.get("created_at") or 0
        try:
            created = int(raw_created)
        except (TypeError, ValueError):
            try:
                created = int(datetime.strptime(str(raw_created), "%a %b %d %H:%M:%S %z %Y").timestamp())
            except (TypeError, ValueError, OverflowError):
                created = 0
        return (likes, created)

    def adapt_comment(self, item: dict, owner: dict, emoji_map: dict[str, str]) -> CommentEntry | None:
        user = item.get("user", {})
        if not user:
            return None
        author = self._parse_user(user)
        text = str(item.get("text") or "").strip()
        rich = _WeiboRichTextParser()
        rich.feed(text)
        parts = rich.parts
        pics = item.get("pics")
        images = self._parse_images(pics if isinstance(pics, list) else [])
        if isinstance(item.get("pic"), dict):
            images = list(dict.fromkeys([*images, *self._parse_images([item["pic"]])]))
        images = list(dict.fromkeys([*images, *rich.images]))
        if not parts and not images:
            return None
        created_at = item.get("created_at")
        time_text = self._format_time(created_at)
        like_count = int(item.get("like_count") or 0)
        reply_count = int(item.get("total_number") or 0)
        pinned = item.get("pinned") == 1
        is_up = self._matches_owner(user, owner)

        entry = CommentEntry(
            author=author,
            content=parts,
            images=images,
            sticker_image="",
            time_text=time_text,
            location="",
            like_text=self._count_text(like_count),
            reply_text=f"回复 {reply_count}" if reply_count else "回复",
            pinned=pinned,
            creator_liked=bool(item.get("isLikedByMblogAuthor")),
            meta_items=[],
        )
        entry.author.is_up = is_up
        if pinned:
            entry.pinned = True

        replies = item.get("comments", [])
        if replies and isinstance(replies, list):
            for reply in replies[:_REPLY_FETCH_LIMIT]:
                reply_user = reply.get("user", {})
                if not reply_user:
                    continue
                reply_author = self._parse_user(reply_user)
                reply_text = str(reply.get("text") or "").strip()
                reply_rich = _WeiboRichTextParser()
                reply_rich.feed(reply_text)
                reply_parts = reply_rich.parts
                reply_images = list(dict.fromkeys([
                    *reply_rich.images,
                    *self._parse_images(reply.get("pics") if isinstance(reply.get("pics"), list) else []),
                    *self._parse_images([reply["pic"]] if isinstance(reply.get("pic"), dict) else []),
                ]))
                if not reply_parts and not reply_images:
                    continue
                reply_created = reply.get("created_at")
                reply_entry = CommentEntry(
                    author=reply_author,
                    content=reply_parts,
                    images=reply_images,
                    time_text=self._format_time(reply_created),
                    like_text=self._count_text(reply.get("like_count")),
                    reply_text="",
                    creator_liked=bool(reply.get("isLikedByMblogAuthor")),
                    meta_items=[],
                )
                reply_entry.author.is_up = owner.get("id") and reply_user.get("id") == owner.get("id")
                entry.first_reply = reply_entry
                break

        return entry

    @staticmethod
    def _format_time(ts: int | None) -> str:
        if not ts:
            return ""
        try:
            dt = datetime.fromtimestamp(int(ts))
        except (TypeError, ValueError, OSError):
            return ""
        now = datetime.now()
        delta = int((now - dt).total_seconds())
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{delta // 60}分钟前"
        if dt.date() == now.date():
            return dt.strftime("今天%H:%M")
        if delta < 365 * 86400:
            return dt.strftime("%m月%d日 %H:%M")
        return dt.strftime("%Y年%m月%d日")

    async def _embed_avatars(self, entries: list[CommentEntry]) -> None:
        all_entries: list[CommentEntry] = []
        pending = list(entries)
        while pending:
            entry = pending.pop(0)
            all_entries.append(entry)
            if entry.first_reply is not None:
                pending.append(entry.first_reply)

        avatar_urls = list(
            dict.fromkeys(
                entry.author.avatar
                for entry in all_entries
                if entry.author.avatar and not entry.author.avatar.startswith("data:")
            )
        )
        if not avatar_urls:
            return

        async def to_data_uri(url: str) -> str | None:
            return await cached_image_to_data_uri(
                self._avatar_data_uri_cache,
                self.parser.http_get,
                url,
                headers=self._headers(),
                referer="https://m.weibo.cn/",
                max_bytes=2 * 1024 * 1024,
                timeout=8,
                debug_label="[WeiboComment] avatar",
            )

        data_uris = await asyncio.gather(*(to_data_uri(url) for url in avatar_urls))
        resolved = dict(zip(avatar_urls, data_uris, strict=True))
        for entry in all_entries:
            if data_uri := resolved.get(entry.author.avatar):
                entry.author.avatar = data_uri

    async def _embed_comment_images(self, entries: list[CommentEntry]) -> None:
        pending = list(entries)
        all_entries = []
        while pending:
            entry = pending.pop(0)
            all_entries.append(entry)
            if entry.first_reply is not None:
                pending.append(entry.first_reply)
        urls = list(dict.fromkeys(url for entry in all_entries for url in entry.images
                                  if url and not url.startswith('data:')))
        semaphore = asyncio.Semaphore(4)

        async def resolve(url):
            async with semaphore:
                return await cached_image_to_data_uri(
                    self._avatar_data_uri_cache, self.parser.http_get, url,
                    headers=self._headers(), referer='https://m.weibo.cn/',
                    max_bytes=8 * 1024 * 1024, timeout=8,
                    debug_label='[WeiboComment] picture',
                )

        resolved = dict(zip(urls, await asyncio.gather(*(resolve(url) for url in urls))))
        for entry in all_entries:
            entry.images = [resolved.get(url) or url for url in entry.images]

    async def _populate_replies(self, mid: str, items: list[dict], limit: int) -> None:
        pass

    async def _load_emoji_map(self) -> dict[str, str]:
        return {}

    async def build_document(
        self,
        mid: str,
        *,
        work_title: str,
        cover: str | None,
        owner: dict | None = None,
        owner_id: str | int | None = None,
    ) -> CommentDocument | None:
        if not (getattr(self.parser, "cookie", "") or getattr(self.parser, "cookies", "")):
            logger.debug(
                "[评论区][微博] stage=fetch result=skipped reason=missing_cookie"
            )
            return None

        raw_feed = await self.fetch(str(mid))
        if not raw_feed.items:
            return None
        emoji_map = await self._load_emoji_map()
        ordered = sorted(raw_feed.items, key=self._sort_key, reverse=True)
        candidates = []
        for item in ordered:
            entry = self.adapt_comment(item, owner or {"id": owner_id}, emoji_map)
            if entry is not None:
                candidates.append(entry)
        entries = await self.comment_filter.apply_keeping_pinned(candidates, limit=self.limit)
        if not entries:
            return None

        partial = raw_feed.total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=WEIBO_THEME,
            work_title=work_title or "微博作品",
            cover=cover or "",
            total_text=f"{self._count_text(raw_feed.total)} 条评论",
            entries=entries,
        )
        await self._embed_avatars(document.entries)
        await self._embed_comment_images(document.entries)
        return document

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


__all__ = ["WeiboCommentFeed"]