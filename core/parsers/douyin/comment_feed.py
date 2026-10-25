from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

from astrbot.api import logger
from msgspec import json as msgjson

from ...comment_canvas import (
    DOUYIN_THEME,
    CommentAuthor,
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from ...comment_filter import CommentFilter
from ...comment_settings import CommentFilterSettings
from ...data import ImageContent
from ...utils import cached_image_to_data_uri, ck2dict
from .a_bogus import SIGN_ARGS_DETAIL, SIGN_ARGS_REPLY, generate_a_bogus


@dataclass(slots=True)
class _RawDouyinFeed:
    items: list[dict]
    total: int
    has_more: bool


# 每条一级评论最多补取多少条楼中楼；只渲染首条回复，取多了纯属浪费请求。
_REPLY_FETCH_LIMIT = 5
_REPLY_CONCURRENCY = 5


class DouyinCommentFeed:
    """Adapt rconsole Douyin comment behavior to AstrBot Canvas."""

    COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"
    REPLY_URL = "https://www.douyin.com/aweme/v1/web/comment/list/reply/"
    EMOJI_URL = "https://www.douyin.com/aweme/v1/web/emoji/list"
    CACHE_VERSION = "douyin_comment_v18_reply_thread"

    def __init__(self, parser, *, limit: int = 10):
        self.parser = parser
        self.limit = max(1, int(limit))
        self._emoji_map: dict[str, str] = {}
        self._emoji_deadline = 0.0
        self._emoji_lock = asyncio.Lock()
        self._avatar_data_uri_cache: dict[str, str | None] = {}
        self.comment_filter = CommentFilter(
            parser,
            CommentFilterSettings.from_config(getattr(parser, "config", {})),
            platform="抖音",
            headers=self._headers(),
            referer="https://www.douyin.com/",
        )

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    @staticmethod
    def _normalize_url(value: object) -> str:
        url = str(value or "").strip().replace("\\", "")
        if url.startswith("//"):
            url = f"https:{url}"
        if url.lower().startswith("http://"):
            url = f"https://{url[7:]}"
        return url if url.startswith("https://") else ""

    @classmethod
    def _image_from_object(cls, value: object) -> str:
        if isinstance(value, str):
            return cls._normalize_url(value)
        if isinstance(value, list):
            urls = [url for item in value if (url := cls._image_from_object(item))]
            # The generic .image variant can return HEIC, which Chromium cannot
            # decode. Prefer a browser-compatible variant supplied by the API;
            # never rewrite a signed URL or its query parameters.
            for url in urls:
                if urllib.parse.urlsplit(url).path.lower().endswith(
                    (".jpeg", ".jpg", ".png", ".webp", ".gif", ".avif")
                ):
                    return url
            return urls[0] if urls else ""
        if not isinstance(value, dict):
            return ""
        for key in (
            "url_list",
            "urlList",
            "uri_list",
            "url",
            "image",
            "origin_url",
            "image_url",
            "icon_url",
            "emoji_url",
            "static_url",
            "animate_url",
        ):
            if key in value and (url := cls._image_from_object(value.get(key))):
                return url
        return ""

    def _headers(self) -> dict[str, str]:
        headers = dict(getattr(self.parser, "headers", {}))
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.douyin.com/",
                "Cookie": getattr(self.parser, "cookies", ""),
            }
        )
        return headers

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
        return await cached_image_to_data_uri(
            self._avatar_data_uri_cache,
            self.parser.http_get,
            avatar,
            headers=self._headers(),
            referer="https://www.douyin.com/",
            max_bytes=2 * 1024 * 1024,
            timeout=8,
            debug_label="[Douyin] comment avatar",
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

    def _common_params(self) -> dict[str, object]:
        params: dict[str, object] = {
            "device_platform": "webapp",
            "aid": 6383,
            "channel": "channel_pc_web",
            "pc_client_type": 1,
            "version_code": 170400,
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": 1920,
            "screen_height": 1080,
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "120.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "120.0.0.0",
            "os_name": "Windows",
            "os_version": 10,
            "cpu_core_num": 8,
            "device_memory": 8,
            "platform": "PC",
            "downlink": 10,
            "effective_type": "4g",
            "round_trip_time": 50,
            "webid": "7361743797237679616",
        }
        cookies = ck2dict(self.parser.cookies)
        if token := cookies.get("msToken"):
            params["msToken"] = token
        if verify_fp := cookies.get("s_v_web_id"):
            params["verifyFp"] = verify_fp
            params["fp"] = verify_fp
        return params

    async def _request_signed(
        self,
        url: str,
        params: dict[str, object],
        *,
        sign_args: tuple[int, int, int] = SIGN_ARGS_DETAIL,
    ) -> dict:
        query = urllib.parse.urlencode(params)
        signed_params = {
            **params,
            "a_bogus": generate_a_bogus(
                query,
                self.parser.headers.get("User-Agent", ""),
                sign_args=sign_args,
            ),
        }
        response = await self.parser.http_get(
            url,
            params=signed_params,
            headers=self._headers(),
            allow_redirects=True,
            timeout=10,
            retries=1,
        )
        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}，响应为空")
        payload = msgjson.decode(response.content)
        if not isinstance(payload, dict):
            raise RuntimeError("评论接口返回了非对象数据")
        status_code = payload.get("status_code")
        if status_code not in (None, 0):
            raise RuntimeError(
                f"status_code={status_code} "
                f"message={payload.get('status_msg') or payload.get('message') or ''}"
            )
        return payload

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("cid") or item.get("id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            output.append(item)
        return output

    async def fetch(self, aweme_id: str) -> _RawDouyinFeed:
        if not self.parser.cookies:
            return _RawDouyinFeed([], 0, False)

        items: list[dict] = []
        cursor = 0
        total = 0
        has_more = False
        for _ in range(3):
            params = {
                **self._common_params(),
                "aweme_id": aweme_id,
                "cursor": cursor,
                "count": 20,
                "item_type": 0,
                "insert_ids": "",
                "whale_cut_token": "",
                "cut_version": 1,
                "rcFT": "",
            }
            try:
                payload = await self._request_signed(self.COMMENT_URL, params)
            except Exception as exc:
                logger.debug(
                    "[评论区][抖音] stage=fetch result=failed "
                    f"error={type(exc).__name__}"
                )
                break

            page_items = payload.get("comments") or (payload.get("data") or {}).get(
                "comments"
            )
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))
            data = payload.get("data") or {}
            raw_total = payload.get("total", data.get("total", len(items)))
            try:
                total = int(raw_total or len(items))
            except (TypeError, ValueError):
                total = len(items)
            has_more = bool(payload.get("has_more", data.get("has_more", False)))
            next_cursor = payload.get("cursor", data.get("cursor", cursor))
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                next_cursor = cursor

            items = self._dedupe(items)
            if len(items) >= max(30, min(60, self.limit * 3)) or not has_more:
                break
            if next_cursor == cursor:
                break
            cursor = next_cursor

        return _RawDouyinFeed(items, total or len(items), has_more)

    async def _fetch_replies(self, aweme_id: str, comment: dict) -> None:
        """为一条一级评论补取楼中楼，结果就地写回 ``reply_comment``。

        抖音的评论列表接口默认只返回 ``reply_comment_total``，不带回复正文；
        回复正文必须另调 ``/comment/list/reply/``，而该接口使用的是另一套
        a_bogus 签名参数（见 a_bogus.SIGN_ARGS_REPLY）。这里失败不影响主评论。
        """
        comment_id = str(comment.get("cid") or comment.get("comment_id") or "").strip()
        if not comment_id:
            return
        params = {
            **self._common_params(),
            "item_id": aweme_id,
            "comment_id": comment_id,
            "cursor": 0,
            "count": _REPLY_FETCH_LIMIT,
            "item_type": 0,
            "cut_version": 1,
        }
        try:
            payload = await self._request_signed(
                self.REPLY_URL, params, sign_args=SIGN_ARGS_REPLY
            )
        except Exception as exc:
            logger.debug(
                "[评论区][抖音] stage=reply result=failed "
                f"error={type(exc).__name__}"
            )
            return
        replies = payload.get("comments")
        if not isinstance(replies, list):
            replies = (payload.get("data") or {}).get("comments")
        items = [item for item in (replies or []) if isinstance(item, dict)]
        if items:
            comment["reply_comment"] = self._dedupe(items)

    async def _populate_replies(
        self,
        aweme_id: str,
        items: list[dict],
        *,
        limit: int,
    ) -> None:
        """按热门顺序给候选主评论并发补取楼中楼。"""
        targets = [
            item
            for item in sorted(items, key=self._sort_key, reverse=True)[: max(1, limit)]
            if self._reply_total(item) > 0
        ]
        if not targets:
            return
        semaphore = asyncio.Semaphore(_REPLY_CONCURRENCY)

        async def run(item: dict) -> None:
            async with semaphore:
                await self._fetch_replies(aweme_id, item)

        await asyncio.gather(*(run(item) for item in targets))

    async def _load_emoji_map(self) -> dict[str, str]:
        now = time.monotonic()
        if self._emoji_map and now < self._emoji_deadline:
            return self._emoji_map

        async with self._emoji_lock:
            now = time.monotonic()
            if self._emoji_map and now < self._emoji_deadline:
                return self._emoji_map
            if not self.parser.cookies:
                return {}
            params = {
                **self._common_params(),
                "publish_video_strategy_type": 2,
                "need_all": "true",
                "update_version_code": 170400,
            }
            try:
                payload = await self._request_signed(self.EMOJI_URL, params)
                output = {}
                for item in payload.get("emoji_list") or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(
                        item.get("display_name")
                        or item.get("emoji_name")
                        or item.get("name")
                        or item.get("text")
                        or ""
                    ).strip()
                    url = self._image_from_object(item.get("emoji_url") or item)
                    if name and url:
                        key = name if name.startswith("[") else f"[{name}]"
                        output[key] = url
                self._emoji_map = output
                self._emoji_deadline = now + 6 * 60 * 60
            except Exception as exc:
                logger.debug(
                    "[评论区][抖音] stage=emoji result=failed "
                    f"error={type(exc).__name__}"
                )
                self._emoji_map = {}
                self._emoji_deadline = now + 10 * 60
            return self._emoji_map

    def _local_emoji_map(
        self,
        item: dict,
        global_map: dict[str, str],
    ) -> dict[str, str]:
        output = dict(global_map)
        candidates = []
        for key in ("emoji", "text_extra", "emoji_list", "emojis"):
            value = item.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        for extra in candidates:
            if not isinstance(extra, dict):
                continue
            name = str(
                extra.get("display_name")
                or extra.get("emoji_name")
                or extra.get("text")
                or extra.get("name")
                or ""
            ).strip()
            url = self._image_from_object(extra)
            if name and url:
                key = name if name.startswith("[") else f"[{name}]"
                output[key] = url
        return output

    @staticmethod
    def _append_plain(parts: list[CommentRichPart], value: str) -> None:
        highlight_pattern = re.compile(r"(@[^\s@#]{1,32}|#[^#\n]{1,64}#)")
        lines = value.split("\n")
        for line_index, line in enumerate(lines):
            cursor = 0
            for match in highlight_pattern.finditer(line):
                if match.start() > cursor:
                    parts.append(CommentRichPart("text", line[cursor : match.start()]))
                parts.append(CommentRichPart("highlight", match.group(0)))
                cursor = match.end()
            if cursor < len(line):
                parts.append(CommentRichPart("text", line[cursor:]))
            if line_index < len(lines) - 1:
                parts.append(CommentRichPart("line-break"))

    def _rich_text(
        self,
        item: dict,
        global_map: dict[str, str],
    ) -> list[CommentRichPart]:
        text = unescape(str(item.get("text") or ""))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return []
        emoji_map = self._local_emoji_map(item, global_map)
        parts: list[CommentRichPart] = []
        cursor = 0
        for match in re.finditer(r"\[[^\]\n]{1,32}\]", text):
            if match.start() > cursor:
                self._append_plain(parts, text[cursor : match.start()])
            token = match.group(0)
            if url := emoji_map.get(token):
                parts.append(CommentRichPart("emote", token, url))
            else:
                parts.append(CommentRichPart("emoji-text", token))
            cursor = match.end()
        if cursor < len(text):
            self._append_plain(parts, text[cursor:])
        return parts

    def _images(self, item: dict) -> list[str]:
        output = []
        seen = set()
        for key in (
            "image_list",
            "images",
            "pic_list",
            "picList",
            "image_urls",
            "imageUrlList",
        ):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for candidate in value:
                url = self._image_from_object(candidate)
                if url and url not in seen:
                    seen.add(url)
                    output.append(url)
        return output

    def _sticker(self, item: dict) -> str:
        for key in (
            "sticker",
            "sticker_detail",
            "sticker_info",
            "sticker_image",
            "sticker_url",
        ):
            value = item.get(key)
            if url := self._image_from_object(value):
                return url
        return ""

    @staticmethod
    def _identity(user: dict) -> tuple[str, ...]:
        return tuple(
            str(user.get(key) or "").strip().lower()
            for key in ("sec_uid", "uid", "unique_id", "short_id", "nickname")
        )

    @classmethod
    def _is_owner(cls, user: dict, owner: dict) -> bool:
        current = cls._identity(user)
        target = cls._identity(owner)
        return any(
            left and right and left == right for left, right in zip(current, target)
        )

    @classmethod
    def _author(cls, item: dict, owner: dict) -> CommentAuthor:
        user = item.get("user") or {}
        badges = []
        if cls._is_owner(user, owner):
            badges.append(CommentBadge("作者"))
        if user.get("enterprise_verify_reason"):
            badges.append(CommentBadge("认证", "#fff", "#1677ff"))
        return CommentAuthor(
            nickname=str(user.get("nickname") or "抖音用户"),
            avatar=cls._image_from_object(
                user.get("avatar_thumb") or user.get("avatar_medium") or {}
            ),
            badges=badges,
        )

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
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return ""
        if timestamp <= 0:
            return ""
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        now = datetime.now()
        created = datetime.fromtimestamp(timestamp)
        delta = max(0, int((now - created).total_seconds()))
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{delta // 60}分钟前"
        if created.date() == now.date():
            return f"今天{created:%H:%M}"
        if created.date() == (now - timedelta(days=1)).date():
            return f"昨天{created:%H:%M}"
        if delta < 30 * 86400:
            return f"{delta // 86400}天前"
        if delta < 365 * 86400:
            return f"{created:%m月%d日 %H:%M}"
        return f"{created:%Y年%m月%d日}"

    @staticmethod
    def _reply_total(item: dict) -> int:
        try:
            return int(item.get("reply_comment_total") or 0)
        except (TypeError, ValueError):
            return 0

    def adapt_comment(
        self,
        item: dict,
        owner: dict,
        emoji_map: dict[str, str],
        *,
        nested: bool = False,
    ) -> CommentEntry | None:
        content = self._rich_text(item, emoji_map)
        images = self._images(item)
        sticker = self._sticker(item)
        if not content and not images and not sticker:
            return None

        reply_count = self._reply_total(item)
        first_reply = None
        if not nested:
            replies = item.get("reply_comment")
            if isinstance(replies, dict):
                replies = [replies]
            if isinstance(replies, list) and replies and isinstance(replies[0], dict):
                first_reply = self.adapt_comment(
                    replies[0],
                    owner,
                    emoji_map,
                    nested=True,
                )

        return CommentEntry(
            author=self._author(item, owner),
            content=content,
            images=images,
            sticker_image=sticker,
            time_text=self._time_text(item.get("create_time")),
            location=str(item.get("ip_label") or "").strip(),
            like_text=self._count_text(item.get("digg_count")),
            reply_text=(
                f"回复 {self._count_text(reply_count)}" if reply_count else "回复"
            ),
            pinned=not nested
            and bool(
                item.get("is_stick")
                or item.get("is_pinned")
                or item.get("stick_position")
            ),
            creator_liked=bool(
                item.get("is_author_digged") or item.get("is_aweme_author_digged")
            ),
            first_reply=first_reply,
        )

    @staticmethod
    def _sort_key(item: dict) -> tuple[int, int]:
        pinned = bool(
            item.get("is_stick") or item.get("is_pinned") or item.get("stick_position")
        )
        try:
            likes = int(item.get("digg_count") or 0)
        except (TypeError, ValueError):
            likes = 0
        return int(pinned), likes

    async def build_document(
        self,
        aweme_id: str,
        *,
        work_title: str,
        cover: str | None,
        owner: dict | None = None,
    ) -> CommentDocument | None:
        if not self.parser.cookies:
            logger.debug(
                "[评论区][抖音] stage=fetch result=skipped reason=missing_cookie"
            )
            return None

        raw_feed = await self.fetch(str(aweme_id))
        if not raw_feed.items:
            return None
        await self._populate_replies(str(aweme_id), raw_feed.items, limit=self.limit)
        emoji_map = await self._load_emoji_map()
        ordered = sorted(raw_feed.items, key=self._sort_key, reverse=True)
        candidates = []
        for item in ordered:
            entry = self.adapt_comment(item, owner or {}, emoji_map)
            if entry is not None:
                candidates.append(entry)
        entries = await self.comment_filter.apply(candidates, limit=self.limit)
        if not entries:
            return None

        partial = raw_feed.total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=DOUYIN_THEME,
            work_title=work_title or "抖音作品",
            cover=self._normalize_url(cover),
            total_text=f"{self._count_text(raw_feed.total)} 条评论",
            entries=entries,
        )
        await self._embed_avatars(document.entries)
        return document


__all__ = ["DouyinCommentFeed"]
