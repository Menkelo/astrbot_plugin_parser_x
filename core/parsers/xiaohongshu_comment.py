from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse
from dataclasses import dataclass
from html import unescape
from typing import Any

from astrbot.api import logger
from msgspec import json as msgjson

from ..comment_canvas import (
    XIAOHONGSHU_THEME,
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
class _RawXiaohongshuFeed:
    items: list[dict]
    total: int
    has_more: bool


class XiaohongshuCommentFeed(SocialCommentFeedBase):
    COMMENT_HOST = "https://edith.xiaohongshu.com"
    COMMENT_PATH = "/api/sns/web/v2/comment/page"
    CACHE_VERSION = "xiaohongshu_comment_v2_emotes"
    PLATFORM_SLUG = "xiaohongshu"
    AVATAR_REFERER = "https://www.xiaohongshu.com/"

    def __init__(
        self,
        parser,
        canvas: SocialCommentCanvas,
        *,
        limit: int = 10,
    ):
        super().__init__(parser, canvas, limit=limit)
        self._signer: Any | None = None
        self._feed_cache: dict[
            str,
            tuple[float, _RawXiaohongshuFeed],
        ] = {}
        try:
            cache_ttl = int(getattr(parser, "_page_cache_ttl", 120))
        except (TypeError, ValueError):
            cache_ttl = 120
        self._feed_cache_ttl = max(30, min(600, cache_ttl))

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or item.get("comment_id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            output.append(item)
        return output

    @staticmethod
    def _build_query(params: dict[str, object]) -> str:
        parts = []
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                value_text = ",".join(str(item) for item in value)
            elif value is None:
                value_text = ""
            else:
                value_text = str(value)
            parts.append(f"{key}={urllib.parse.quote(value_text, safe=',')}")
        return "&".join(parts)

    def _sign_headers(
        self,
        path: str,
        params: dict[str, object],
        *,
        sign_format: str,
    ) -> dict[str, str]:
        try:
            from xhshow import Xhshow
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "小红书评论签名依赖 xhshow 未安装，请重新安装插件依赖"
            ) from exc

        if self._signer is None:
            self._signer = Xhshow()
        try:
            return self._signer.sign_headers_get(
                uri=path,
                cookies=self.parser.comment_cookie,
                params=params,
                sign_format=sign_format,
            )
        except ValueError as exc:
            message = str(exc)
            if "a1" in message.lower():
                raise RuntimeError(
                    "小红书 Cookie 缺少 a1，无法生成评论接口签名"
                ) from exc
            raise RuntimeError("小红书评论接口签名生成失败") from exc
        except Exception as exc:
            raise RuntimeError("小红书评论接口签名生成失败") from exc

    def _request_headers(
        self,
        path: str,
        params: dict[str, object],
        *,
        sign_format: str,
        referer: str,
    ) -> dict[str, str]:
        headers = dict(self.parser.headers)
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://www.xiaohongshu.com",
                "Referer": referer,
                "Cookie": self.parser.comment_cookie,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }
        )
        headers.update(
            self._sign_headers(
                path,
                params,
                sign_format=sign_format,
            )
        )
        return headers

    @staticmethod
    def _payload_message(payload: dict) -> str:
        message = str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error_msg")
            or ""
        ).strip()
        return message[:160]

    @classmethod
    def _sign_rejected(cls, status_code: int, payload: dict) -> bool:
        if status_code == 406:
            return True
        message = cls._payload_message(payload).lower()
        return any(
            marker in message
            for marker in (
                "sign",
                "signature",
                "x-s",
                "签名",
            )
        )

    @classmethod
    def _public_request_error(cls, status_code: int, payload: dict) -> RuntimeError:
        if status_code in {461, 471}:
            return RuntimeError("小红书评论请求触发访问验证，请更新 Cookie 后重试")
        if status_code in {401, 403}:
            return RuntimeError("小红书评论登录态已失效或无权访问该笔记")
        if status_code == 406:
            return RuntimeError("小红书拒绝了评论接口签名，请更新插件依赖")

        code = payload.get("code") if isinstance(payload, dict) else None
        message = cls._payload_message(payload) if isinstance(payload, dict) else ""
        detail = message or (f"错误码 {code}" if code not in (None, "") else "未知错误")
        return RuntimeError(f"小红书评论接口请求失败：{detail}")

    async def _request_page(
        self,
        params: dict[str, object],
        *,
        referer: str,
    ) -> dict:
        query = self._build_query(params)
        url = f"{self.COMMENT_HOST}{self.COMMENT_PATH}?{query}"
        last_error: RuntimeError | None = None

        for sign_format in ("xys", "xyw"):
            headers = self._request_headers(
                self.COMMENT_PATH,
                params,
                sign_format=sign_format,
                referer=referer,
            )
            response = await self.parser.http_get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=10,
                retries=1,
            )
            try:
                payload = msgjson.decode(response.content) if response.content else {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            success = payload.get("success")
            code = payload.get("code")
            if (
                response.status_code == 200
                and success is not False
                and code in (None, 0, "0")
            ):
                data = payload.get("data", payload)
                if isinstance(data, dict):
                    return data
                raise RuntimeError("小红书评论接口返回了无效数据")

            last_error = self._public_request_error(response.status_code, payload)
            if sign_format == "xys" and self._sign_rejected(
                response.status_code,
                payload,
            ):
                continue
            raise last_error

        raise last_error or RuntimeError("小红书评论接口请求失败")

    @staticmethod
    def _cache_key(note_id: str, xsec_token: str) -> str:
        token_digest = hashlib.sha256(xsec_token.encode()).hexdigest()[:12]
        return f"{note_id}:{token_digest}"

    async def fetch(
        self,
        note_id: str,
        xsec_token: str,
    ) -> _RawXiaohongshuFeed:
        if not self.parser.comment_cookie:
            return _RawXiaohongshuFeed([], 0, False)

        cache_key = self._cache_key(note_id, xsec_token)
        cached = self._feed_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        if cached:
            self._feed_cache.pop(cache_key, None)

        items: list[dict] = []
        cursor = ""
        total = 0
        has_more = False
        referer = (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={urllib.parse.quote(xsec_token, safe='')}"
            "&xsec_source=pc_feed"
        )

        for _ in range(3):
            params: dict[str, object] = {
                "note_id": note_id,
                "cursor": cursor,
                "top_comment_id": "",
                "image_formats": "jpg,webp,avif",
                "xsec_token": xsec_token,
            }
            data = await self._request_page(params, referer=referer)
            page_items = data.get("comments") or []
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))
            items = self._dedupe(items)
            try:
                total = int(
                    data.get("total")
                    or data.get("comment_count")
                    or total
                    or len(items)
                )
            except (TypeError, ValueError):
                total = max(total, len(items))

            has_more = bool(data.get("has_more", False))
            next_cursor = str(data.get("cursor") or "")
            if len(items) >= max(30, min(60, self.limit * 3)) or not has_more:
                break
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        result = _RawXiaohongshuFeed(items, max(total, len(items)), has_more)
        self._feed_cache[cache_key] = (
            now + self._feed_cache_ttl,
            result,
        )
        while len(self._feed_cache) > 64:
            self._feed_cache.pop(next(iter(self._feed_cache)), None)
        return result

    @staticmethod
    def _tag_names(item: dict) -> set[str]:
        output: set[str] = set()
        tags = item.get("show_tags") or item.get("showTags") or []
        if isinstance(tags, (str, dict)):
            tags = [tags]
        if not isinstance(tags, list):
            return output
        for tag in tags:
            if isinstance(tag, str):
                value = tag
            elif isinstance(tag, dict):
                value = tag.get("name") or tag.get("type") or tag.get("tag") or ""
            else:
                continue
            normalized = str(value).strip().lower()
            if normalized:
                output.add(normalized)
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

    @classmethod
    def _rich_text(
        cls,
        value: object,
        emote_map: dict[str, str] | None = None,
    ) -> list[CommentRichPart]:
        text = unescape(str(value or ""))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return []

        parts: list[CommentRichPart] = []
        catalog = emote_map or fallback_emote_map("xiaohongshu")
        cursor = 0
        for start, end, token, url in iter_emote_matches(
            text,
            "xiaohongshu",
            catalog,
        ):
            if start > cursor:
                cls._append_plain(parts, text[cursor:start])
            parts.append(
                CommentRichPart(
                    "emote" if url else "emoji-text",
                    text=token,
                    url=url,
                )
            )
            cursor = end
        if cursor < len(text):
            cls._append_plain(parts, text[cursor:])
        return parts

    @classmethod
    def _local_emote_map(
        cls,
        item: dict,
        global_map: dict[str, str],
    ) -> dict[str, str]:
        """Merge emoji objects embedded in a comment with the public catalog.

        Some Xiaohongshu responses include the image URL next to the comment
        instead of only returning a ``[name]`` token in ``content``.  The web
        API has used several names for that field, so inspect only known emoji
        containers and keep the traversal deliberately shallow.
        """
        output = dict(global_map)

        def register(name: object, url: object) -> None:
            name_text = str(name or "").strip()
            url_text = normalize_image_url(str(url or "")) or ""
            if not name_text or not url_text:
                return
            token = (
                name_text
                if name_text.startswith("[") and name_text.endswith("]")
                else f"[{name_text}]"
            )
            output[token] = url_text
            output.setdefault(name_text, url_text)

        def walk(value: object, depth: int = 0) -> None:
            if depth > 2:
                return
            if isinstance(value, list):
                for child in value:
                    walk(child, depth + 1)
                return
            if not isinstance(value, dict):
                return

            name = next(
                (
                    value.get(key)
                    for key in (
                        "image_name",
                        "imageName",
                        "display_name",
                        "displayName",
                        "emoji_name",
                        "emojiName",
                        "emoticon_name",
                        "name",
                        "text",
                    )
                    if value.get(key)
                ),
                "",
            )
            url = next(
                (
                    value.get(key)
                    for key in (
                        "image",
                        "url",
                        "src",
                        "image_url",
                        "imageUrl",
                        "url_default",
                        "urlDefault",
                    )
                    if value.get(key)
                ),
                "",
            )
            if isinstance(url, dict):
                url = cls._image_from_object(url)
            register(name, url)
            for key in (
                "emoji",
                "emojis",
                "emoticon",
                "emoticons",
                "redmoji",
                "emoji_info",
                "emojiInfo",
                "content_emojis",
                "contentEmojis",
            ):
                if key in value:
                    walk(value.get(key), depth + 1)

        for key in (
            "emoji",
            "emojis",
            "emoticon",
            "emoticons",
            "redmoji",
            "emoji_info",
            "emojiInfo",
            "content_emojis",
            "contentEmojis",
        ):
            if key in item:
                walk(item.get(key))
        return output

    @staticmethod
    def _image_from_object(value: object) -> str:
        if isinstance(value, str):
            return normalize_image_url(value) or ""
        if not isinstance(value, dict):
            return ""
        for key in (
            "url_default",
            "urlDefault",
            "url_pre",
            "urlPre",
            "url",
            "src",
        ):
            if url := normalize_image_url(value.get(key)):
                return url
        for item in value.get("info_list") or value.get("infoList") or []:
            if isinstance(item, dict) and (url := normalize_image_url(item.get("url"))):
                return url
        return ""

    @classmethod
    def _images(cls, item: dict) -> list[str]:
        output = []
        values = item.get("pictures") or item.get("images") or []
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            return []
        for value in values:
            if url := cls._image_from_object(value):
                output.append(url)
        return list(dict.fromkeys(output))

    @classmethod
    def _author(cls, item: dict, owner_id: str) -> CommentAuthor:
        user = item.get("user_info") or item.get("userInfo") or item.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        user_id = str(user.get("user_id") or user.get("userId") or "")
        tags = cls._tag_names(item)
        is_owner = "is_author" in tags or bool(owner_id and user_id == owner_id)
        badges = [CommentBadge("作者")] if is_owner else []
        return CommentAuthor(
            nickname=str(user.get("nickname") or user.get("nick_name") or "小红书用户"),
            avatar=cls._image_from_object(
                user.get("image") or user.get("avatar") or user
            ),
            nickname_color=XIAOHONGSHU_THEME.accent if is_owner else "",
            badges=badges,
        )

    @staticmethod
    def _timestamp_seconds(value: object) -> int | None:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return None
        if timestamp <= 0:
            return None
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp

    @classmethod
    def _is_pinned(cls, item: dict) -> bool:
        tags = cls._tag_names(item)
        return bool(
            tags & {"user_top", "is_top", "top", "pinned"}
            or item.get("is_top")
            or item.get("is_pinned")
        )

    def adapt_comment(
        self,
        item: dict,
        owner_id: str,
        *,
        nested: bool = False,
        emote_map: dict[str, str] | None = None,
    ) -> CommentEntry | None:
        local_emotes = self._local_emote_map(item, emote_map or {})
        content = self._rich_text(
            item.get("content") or item.get("text"),
            local_emotes,
        )
        images = self._images(item)
        if not content and not images:
            return None

        if nested:
            target = item.get("target_comment") or item.get("targetComment") or {}
            target_user = target.get("user_info") if isinstance(target, dict) else {}
            target_name = (
                str((target_user or {}).get("nickname") or "").strip()
                if isinstance(target_user, dict)
                else ""
            )
            if target_name:
                content = [
                    CommentRichPart("highlight", f"回复 @{target_name}："),
                    *content,
                ]

        try:
            reply_count = int(item.get("sub_comment_count") or 0)
        except (TypeError, ValueError):
            reply_count = 0

        first_reply = None
        if not nested:
            replies = item.get("sub_comments") or item.get("subComments") or []
            if isinstance(replies, dict):
                nested_items = replies.get("comments") or replies.get("items")
                replies = nested_items if isinstance(nested_items, list) else [replies]
            if isinstance(replies, list):
                for reply in replies:
                    if not isinstance(reply, dict):
                        continue
                    first_reply = self.adapt_comment(
                        reply,
                        owner_id,
                        nested=True,
                        emote_map=local_emotes,
                    )
                    if first_reply is not None:
                        break

        timestamp = self._timestamp_seconds(item.get("create_time"))
        location = re.sub(
            r"^IP属地\s*[:：]?\s*",
            "",
            str(item.get("ip_location") or "").strip(),
        )
        return CommentEntry(
            author=self._author(item, owner_id),
            content=content,
            images=images,
            time_text=self.time_text(timestamp) if timestamp else "",
            location=location,
            like_text=self.count_text(item.get("like_count")),
            reply_text=(
                f"回复 {self.count_text(reply_count)}" if reply_count else "回复"
            ),
            pinned=not nested and self._is_pinned(item),
            first_reply=first_reply,
        )

    async def build_document(
        self,
        note_id: str,
        xsec_token: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
        total_hint: int | None = None,
    ) -> CommentDocument | None:
        if not self.parser.comment_cookie:
            logger.debug(
                "[评论区][小红书] stage=fetch result=skipped reason=missing_cookie"
            )
            return None

        raw_feed, emote_map = await asyncio.gather(
            self.fetch(str(note_id), xsec_token),
            load_platform_emotes(self.parser, "xiaohongshu"),
        )
        if not raw_feed.items:
            return None

        ordered = sorted(raw_feed.items, key=self._is_pinned, reverse=True)
        owner_text = str(owner_id or "")
        candidates = []
        for item in ordered:
            entry = self.adapt_comment(item, owner_text, emote_map=emote_map)
            if entry is not None:
                candidates.append(entry)
        entries = await self.comment_filter.apply(candidates, limit=self.limit)
        if not entries:
            return None

        try:
            total = int(total_hint or 0)
        except (TypeError, ValueError):
            total = 0
        total = max(total, raw_feed.total, len(entries))
        partial = total > len(entries) or raw_feed.has_more
        document = CommentDocument(
            theme=XIAOHONGSHU_THEME,
            work_title=work_title or "小红书视频",
            cover=normalize_image_url(cover) or "",
            total_text=f"{self.count_text(total)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · 小红书评论区"
            ),
        )
        await self._embed_avatars(document.entries)
        return document

    async def build_images(
        self,
        note_id: str,
        xsec_token: str,
        *,
        work_title: str,
        cover: str | None,
        owner_id: str | int | None,
        total_hint: int | None = None,
    ) -> list[ImageContent]:
        document = await self.build_document(
            note_id,
            xsec_token,
            work_title=work_title,
            cover=cover,
            owner_id=owner_id,
            total_hint=total_hint,
        )
        if document is None:
            return []
        return self.render_document(note_id, document)


__all__ = ["XiaohongshuCommentFeed"]
