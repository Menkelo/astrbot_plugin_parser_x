from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time
from html import unescape
from re import Match
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..comment_canvas import SocialCommentCanvas
from ..comment_settings import CommentSettings
from ..data import (
    DeliveryBatch,
    DeliveryPlan,
    ImageContent,
    Platform,
    VideoContent,
)
from ..exception import ParseException
from ..html_renderer import HtmlRenderService
from ..platform_emotes import (
    contains_platform_emotes,
    load_platform_emotes,
    select_text_emotes,
)
from ..utils import normalize_image_url
from .base import BaseParser, handle
from .miyoushe_comment import MiyousheCommentFeed


class MiyousheParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="miyoushe", display_name="米游社")
    api_url = "https://bbs-api.miyoushe.com/post/wapi/getPostFull"
    ds_salt = "ZSHlXeQUBis52qD1kEgKt5lUYed4b7Bb"

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.miyoushe.com/",
                "x-rpc-app_version": "2.87.0",
                "x-rpc-client_type": "4",
            }
        )
        self.render_service = HtmlRenderService.from_config(config)
        comment_settings = CommentSettings.from_config(config, "miyoushe")
        self.enable_comment_card = comment_settings.enabled
        self.comment_limit = comment_settings.display_count
        self.comment_timeout = comment_settings.timeout
        self.comment_canvas = SocialCommentCanvas(self.render_service)
        self.comment_feed = MiyousheCommentFeed(
            self,
            self.comment_canvas,
            limit=self.comment_limit,
        )

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service
        self.comment_canvas.render_service = render_service

    def _comment_extra(
        self,
        post_id: str,
        *,
        title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> dict:
        if not self.enable_comment_card or not post_id:
            return {}

        async def build_comment_images():
            return await self.comment_feed.build_images(
                post_id,
                work_title=title,
                cover=cover,
                owner_id=owner_id,
            )

        return {
            "comment_image_task_factory": build_comment_images,
            "comment_timeout": self.comment_timeout,
        }

    @classmethod
    def build_ds(cls, timestamp: int | None = None, nonce: str | None = None) -> str:
        timestamp = timestamp or int(time.time())
        nonce = nonce or "".join(
            random.choice(string.ascii_letters + string.digits) for _ in range(6)
        )
        digest = hashlib.md5(
            f"salt={cls.ds_salt}&t={timestamp}&r={nonce}".encode()
        ).hexdigest()
        return f"{timestamp},{nonce},{digest}"

    @staticmethod
    def extract_post_id(url: str) -> str | None:
        path = urlparse(url).path
        if matched := re.search(r"/article/(\d+)(?:/|$)", path):
            return matched.group(1)
        numeric = re.findall(r"/(\d+)(?=/|$)", path)
        return numeric[-1] if numeric else None

    @staticmethod
    def _plain_content(value: Any) -> str:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                if "<" in value and ">" in value:
                    value = re.sub(
                        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
                        "",
                        value,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    value = re.sub(
                        r"</(?:p|div|h[1-6]|blockquote|li)>|<br\s*/?>",
                        "\n",
                        value,
                        flags=re.IGNORECASE,
                    )
                    value = re.sub(r"<[^>]+>", "", value)
                lines = [
                    re.sub(r"[ \t\u00a0]+", " ", line).strip()
                    for line in unescape(value).splitlines()
                ]
                return "\n".join(line for line in lines if line)
            return MiyousheParser._plain_content(decoded)
        if isinstance(value, dict):
            for key in ("describe", "content", "text"):
                if key in value and (text := MiyousheParser._plain_content(value[key])):
                    return text
            parts = [MiyousheParser._plain_content(item) for item in value.values()]
            return "\n".join(item for item in parts if item)
        if isinstance(value, list):
            parts = [MiyousheParser._plain_content(item) for item in value]
            return "\n".join(item for item in parts if item)
        return ""

    @classmethod
    def _structured_content_flow(cls, value: Any) -> list[dict[str, str]]:
        """Extract ordered text and image blocks from MiHoYo's Quill delta."""

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return []
        if not isinstance(value, list):
            return []

        flow: list[dict[str, str]] = []
        text_parts: list[str] = []
        seen_images: set[str] = set()

        def flush_text() -> None:
            if not text_parts:
                return
            text = cls._plain_content("".join(text_parts))
            text_parts.clear()
            if text:
                flow.append({"type": "text", "text": text})

        def append_embedded_text(text: str) -> None:
            text = cls._plain_content(text)
            if not text:
                return
            if text_parts and not text_parts[-1].endswith("\n"):
                text_parts.append("\n")
            text_parts.append(text)
            if not text.endswith("\n"):
                text_parts.append("\n")

        for row in value:
            if not isinstance(row, dict):
                continue
            insert = row.get("insert")
            if isinstance(insert, str):
                text_parts.append(insert)
                continue
            if not isinstance(insert, dict):
                continue

            image_value = (
                insert.get("image")
                or insert.get("image_url")
                or insert.get("img_url")
            )
            if isinstance(image_value, dict):
                image_value = (
                    image_value.get("url")
                    or image_value.get("image_url")
                    or image_value.get("src")
                )
            image_url = normalize_image_url(image_value)
            if image_url:
                flush_text()
                if image_url not in seen_images:
                    seen_images.add(image_url)
                    flow.append({"type": "image", "url": image_url})
                continue

            embedded_text = insert.get("backup_text")
            if not isinstance(embedded_text, str) or not embedded_text.strip():
                embedded_text = next(
                    (
                        insert.get(key)
                        for key in ("text", "content", "describe")
                        if isinstance(insert.get(key), str)
                        and str(insert.get(key)).strip()
                    ),
                    "",
                )
            if not embedded_text and isinstance(insert.get("fold"), dict):
                fold = insert["fold"]
                embedded_text = "\n".join(
                    item
                    for item in (
                        cls._plain_content(fold.get("title")),
                        cls._plain_content(fold.get("content")),
                    )
                    if item
                )
            if embedded_text:
                append_embedded_text(str(embedded_text))

        flush_text()
        return flow

    @classmethod
    def _ordered_content_flow(cls, post: dict[str, Any]) -> list[dict[str, str]]:
        flow = cls._structured_content_flow(post.get("structured_content"))
        fallback_text = cls._plain_content(post.get("content"))
        if fallback_text and not any(block.get("type") == "text" for block in flow):
            flow.insert(0, {"type": "text", "text": fallback_text})

        seen_images = {
            block.get("url", "")
            for block in flow
            if block.get("type") == "image"
        }
        for image_url in cls._image_urls(post):
            if image_url in seen_images:
                continue
            seen_images.add(image_url)
            flow.append({"type": "image", "url": image_url})
        return flow

    @staticmethod
    def _image_urls(post: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for item in post.get("images") or []:
            if isinstance(item, str):
                url = item
            elif isinstance(item, dict):
                url = item.get("url") or item.get("image_url")
            else:
                continue
            if normalized := normalize_image_url(url):
                urls.append(normalized)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _video_url(vod_list: Any) -> tuple[str | None, float]:
        candidates: list[tuple[int, str, float]] = []
        if not isinstance(vod_list, list):
            return None, 0
        for vod in vod_list:
            if not isinstance(vod, dict):
                continue
            duration = float(vod.get("duration") or 0)
            for resolution in vod.get("resolutions") or []:
                if not isinstance(resolution, dict) or not resolution.get("url"):
                    continue
                width = int(resolution.get("width") or 0)
                height = int(resolution.get("height") or 0)
                score = width * height if width and height else 10**12
                candidates.append((score, resolution["url"], duration))
        if not candidates:
            return None, 0
        _, url, duration = min(candidates, key=lambda item: item[0])
        return url, duration

    @handle(
        "miyoushe.com",
        r"https?://(?:m|www)\.miyoushe\.com/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    async def parse_miyoushe(self, searched: Match[str]):
        url = searched.group(0).rstrip(").,;!?，。；！？）]")
        post_id = self.extract_post_id(url)
        if not post_id:
            raise ParseException("米游社链接中没有文章编号")
        headers = dict(self.headers)
        headers["DS"] = self.build_ds()
        response = await self.http_get(
            self.api_url,
            params={"post_id": post_id},
            headers=headers,
            timeout=15,
            retries=2,
        )
        if response.status_code >= 400:
            raise ParseException(f"米游社接口请求失败: HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:
            raise ParseException("米游社返回了无效数据") from exc
        if payload.get("retcode") not in (0, None):
            raise ParseException(payload.get("message") or "米游社接口返回错误")

        post_container = (payload.get("data") or {}).get("post") or {}
        post = post_container.get("post") or {}
        if not post:
            raise ParseException("米游社文章不存在或不可见")
        user = post_container.get("user") or post.get("user") or {}
        author_name = (
            user.get("nickname")
            or post.get("author_name")
            or post.get("user_name")
            or "米游社用户"
        )
        author_avatar = user.get("avatar_url") or user.get("avatar")
        ordered_flow = self._ordered_content_flow(post)
        full_text = "\n".join(
            block.get("text", "")
            for block in ordered_flow
            if block.get("type") == "text"
        ).strip()
        if not full_text:
            full_text = self._plain_content(post.get("content"))
        text = full_text
        if len(text) > 4000:
            text = text[:3997] + "..."

        cover_url = normalize_image_url(post.get("cover"))
        cover_content = None
        if cover_url:
            cover_content = ImageContent(
                self.downloader.download_img(
                    cover_url,
                    ext_headers=self.headers,
                )
            )
        card_flow: list[dict[str, str]] = []
        body_image_urls: list[str] = []
        for block in ordered_flow:
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                card_flow.append({"type": "text", "text": block["text"]})
                continue
            if block_type != "image":
                continue
            image_url = block.get("url", "")
            if not image_url or image_url == cover_url or image_url in body_image_urls:
                continue
            body_image_urls.append(image_url)
            card_flow.append({"type": "image", "url": image_url})
        if full_text and not any(block.get("type") == "text" for block in card_flow):
            card_flow.insert(0, {"type": "text", "text": full_text})

        image_content_by_url = {
            image_url: ImageContent(
                self.downloader.download_img(image_url, ext_headers=self.headers)
            )
            for image_url in body_image_urls
        }
        image_contents = list(image_content_by_url.values())
        video_contents = []
        video_url, duration = self._video_url(post_container.get("vod_list"))
        if video_url:
            video_contents.append(
                VideoContent(
                    self.downloader.download_video(
                        video_url,
                        video_name=f"miyoushe_{post_id}.mp4",
                        ext_headers=self.headers,
                        max_size_mb=int(
                            self.config.get("performance", {}).get(
                                "source_max_size", 90
                            )
                        ),
                    ),
                    duration=duration,
                )
            )

        title = str(post.get("subject") or post.get("title") or "米游社文章")
        native_parts: list[str | ImageContent] = []
        if cover_content is not None:
            native_parts.append(cover_content)
        if title:
            native_parts.append(title)
        for block in card_flow:
            if block.get("type") == "text" and block.get("text"):
                native_parts.append(block["text"])
                continue
            image_content = image_content_by_url.get(block.get("url", ""))
            if image_content is not None:
                native_parts.append(image_content)
        batches = [DeliveryBatch(native_parts, mode="forward")] if native_parts else []
        batches.extend(DeliveryBatch([video]) for video in video_contents)

        contents = [
            *([cover_content] if cover_content is not None else []),
            *image_contents,
            *video_contents,
        ]
        timestamp = post.get("created_at") or post.get("publish_time")
        try:
            timestamp = int(timestamp) if timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        owner_id = user.get("uid") or user.get("user_id") or user.get("id")
        extra = self._comment_extra(
            post_id,
            title=title,
            cover=cover_url,
            owner_id=owner_id,
        )
        card_emotes = {}
        if contains_platform_emotes(full_text, "miyoushe"):
            emote_catalog = await load_platform_emotes(
                self,
                "miyoushe",
                gids=post.get("game_id") or post.get("gids") or 2,
            )
            card_emotes = select_text_emotes(full_text, "miyoushe", emote_catalog)
        extra.update(
            {
                "render_text_card": True,
                "text_card_avatar": str(author_avatar or ""),
                "text_card_media": str(cover_url or ""),
                "text_card_flow": card_flow,
                "delivery_text_card_consume_non_video": True,
                "native_delivery": True,
                "card_emotes": card_emotes,
                "card_kind": (
                    "文章 · 视频"
                    if video_contents
                    else "文章 · 图文"
                    if contents
                    else "文章"
                ),
                "card_author_badge": "作者",
                "card_metrics": [
                    (
                        "浏览",
                        (post_container.get("stat") or post.get("stat") or {}).get(
                            "view_num"
                        ),
                    ),
                    (
                        "评论",
                        (post_container.get("stat") or post.get("stat") or {}).get(
                            "reply_num"
                        ),
                    ),
                    (
                        "点赞",
                        (post_container.get("stat") or post.get("stat") or {}).get(
                            "like_num"
                        ),
                    ),
                    (
                        "收藏",
                        (post_container.get("stat") or post.get("stat") or {}).get(
                            "bookmark_num"
                        ),
                    ),
                ],
                "card_info": [
                    "正文完整保留",
                    *([f"媒体 {len(contents)} 项"] if contents else []),
                ],
            }
        )
        return self.result(
            title=title,
            author=self.create_author(author_name, author_avatar),
            text=text or None,
            contents=contents,
            delivery=DeliveryPlan(batches),
            timestamp=timestamp,
            url=url,
            extra=extra,
        )


__all__ = ["MiyousheParser"]
