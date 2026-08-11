from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time
from re import Match
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..data import ImageContent, Platform, VideoContent
from ..exception import ParseException
from ..utils import normalize_image_url
from .base import BaseParser, handle


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
                return re.sub(r"\s+", " ", value).strip()
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
        cover = normalize_image_url(post.get("cover"))
        if cover:
            urls.insert(0, cover)
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
        text = self._plain_content(post.get("content"))
        if len(text) > 4000:
            text = text[:3997] + "..."

        contents = [
            ImageContent(self.downloader.download_img(item, ext_headers=self.headers))
            for item in self._image_urls(post)
        ]
        video_url, duration = self._video_url(post_container.get("vod_list"))
        if video_url:
            contents.append(
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
        timestamp = post.get("created_at") or post.get("publish_time")
        try:
            timestamp = int(timestamp) if timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        return self.result(
            title=post.get("subject") or post.get("title") or "米游社文章",
            author=self.create_author(author_name, author_avatar),
            text=text or None,
            contents=contents,
            timestamp=timestamp,
            url=url,
            extra={"send_text": True},
        )


__all__ = ["MiyousheParser"]
