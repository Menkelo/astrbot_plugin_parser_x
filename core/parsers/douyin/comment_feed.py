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
from ...constants import COMMENT_FOOTER_BRAND
from ...data import ImageContent
from ...utils import ck2dict
from .a_bogus import generate_a_bogus


@dataclass(slots=True)
class _RawDouyinFeed:
    items: list[dict]
    total: int
    has_more: bool


class DouyinCommentFeed:
    """Adapt rconsole Douyin comment behavior to AstrBot Canvas."""

    COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"
    EMOJI_URL = "https://www.douyin.com/aweme/v1/web/emoji/list"
    CACHE_VERSION = "douyin_comment_v7_official_render_crop"

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
        self._emoji_map: dict[str, str] = {}
        self._emoji_deadline = 0.0
        self._emoji_lock = asyncio.Lock()

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
            for item in value:
                if url := cls._image_from_object(item):
                    return url
            return ""
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
        headers = self.parser.headers.copy()
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.douyin.com/",
                "Cookie": self.parser.cookies,
            }
        )
        return headers

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
    ) -> dict:
        query = urllib.parse.urlencode(params)
        signed_params = {
            **params,
            "a_bogus": generate_a_bogus(
                query,
                self.parser.headers.get("User-Agent", ""),
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
                logger.debug(f"[Douyin] 评论接口请求失败: {exc}")
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
            if len(items) >= max(20, self.limit) or not has_more:
                break
            if next_cursor == cursor:
                break
            cursor = next_cursor

        return _RawDouyinFeed(items, total or len(items), has_more)

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
                logger.debug(f"[Douyin] 评论表情列表获取失败: {exc}")
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
        sticker = item.get("sticker")
        return self._image_from_object(sticker) if isinstance(sticker, dict) else ""

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

        try:
            reply_count = int(item.get("reply_comment_total") or 0)
        except (TypeError, ValueError):
            reply_count = 0
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

    async def build_images(
        self,
        aweme_id: str,
        *,
        work_title: str,
        cover: str | None,
        owner: dict | None = None,
    ) -> list[ImageContent]:
        if not self.parser.cookies:
            logger.debug("[Douyin] 未配置 douyin_ck，跳过评论区")
            return []

        raw_feed = await self.fetch(str(aweme_id))
        if not raw_feed.items:
            return []
        emoji_map = await self._load_emoji_map()
        ordered = sorted(raw_feed.items, key=self._sort_key, reverse=True)
        entries = []
        for item in ordered:
            entry = self.adapt_comment(item, owner or {}, emoji_map)
            if entry is not None:
                entries.append(entry)
            if len(entries) >= self.limit:
                break
        if not entries:
            return []

        partial = raw_feed.total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=DOUYIN_THEME,
            work_title=work_title or "抖音作品",
            cover=self._normalize_url(cover),
            total_text=f"{self._count_text(raw_feed.total)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · 抖音评论区"
            ),
        )
        serialised = json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.CACHE_VERSION}|{serialised}".encode()
        ).hexdigest()[:12]
        out_path = self.cache_dir / f"douyin_comment_{aweme_id}_{digest}.jpg"
        if out_path.is_file() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def render() -> Path:
            await self.canvas.render(out_path, document)
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    render(),
                    name=f"douyin_comment_canvas_{aweme_id}",
                )
            )
        ]


__all__ = ["DouyinCommentFeed"]
