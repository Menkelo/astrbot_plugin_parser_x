from __future__ import annotations

import hashlib
import json
import random
import re
import time
from re import Match
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from ..data import ImageContent, Platform, VideoContent
from ..exception import ParseException
from ..utils import normalize_image_url
from .base import BaseParser, handle
from .music import parse_open_graph


class XiaoheiheParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaoheihe", display_name="小黑盒")
    salt = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
    api_paths = {
        "bbs": "bbs/app/link/tree",
        "pc": "game/get_game_detail",
        "console": "game/console/get_game_detail",
        "mobile": "game/mobile/get_game_detail",
    }

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.cookie = config.get("cookies", {}).get("xiaoheihe_cookie", "")
        self.headers.update(
            {
                "Referer": "https://www.xiaoheihe.cn/",
                "Origin": "https://www.xiaoheihe.cn",
                **({"Cookie": self.cookie} if self.cookie else {}),
            }
        )

    @staticmethod
    def _da(value: int) -> int:
        return ((value << 1) ^ 27) & 255 if value & 128 else value << 1

    @classmethod
    def _ba(cls, value: int) -> int:
        return cls._da(value) ^ value

    @classmethod
    def _na(cls, value: int) -> int:
        return cls._ba(cls._da(value))

    @classmethod
    def _fa(cls, value: int) -> int:
        return cls._na(cls._ba(cls._da(value)))

    @classmethod
    def _ua(cls, value: int) -> int:
        return cls._fa(value) ^ cls._na(value) ^ cls._ba(value)

    @classmethod
    def _map_chars(cls, value: str, trim: int) -> str:
        alphabet = cls.salt[:trim]
        return "".join(alphabet[ord(char) % len(alphabet)] for char in value)

    @classmethod
    def build_hkey(cls, path: str, timestamp: int, nonce: str) -> str:
        normalized_path = f"/{path.strip('/')}/"
        sources = [
            cls._map_chars(str(timestamp), -2),
            cls._map_chars(normalized_path, len(cls.salt)),
            cls._map_chars(nonce, len(cls.salt)),
        ]
        interleaved = "".join(
            source[index]
            for index in range(max(map(len, sources)))
            for source in sources
            if index < len(source)
        )[:20]
        digest = hashlib.md5(interleaved.encode()).hexdigest()
        values = [ord(char) for char in digest[-6:]]
        original = values[:4]
        values[0] = (
            cls._ua(original[0])
            ^ cls._fa(original[1])
            ^ cls._na(original[2])
            ^ cls._ba(original[3])
        )
        values[1] = (
            cls._ba(original[0])
            ^ cls._ua(original[1])
            ^ cls._fa(original[2])
            ^ cls._na(original[3])
        )
        values[2] = (
            cls._na(original[0])
            ^ cls._ba(original[1])
            ^ cls._ua(original[2])
            ^ cls._fa(original[3])
        )
        values[3] = (
            cls._fa(original[0])
            ^ cls._na(original[1])
            ^ cls._ba(original[2])
            ^ cls._ua(original[3])
        )
        suffix = str(sum(values) % 100).zfill(2)
        return f"{cls._map_chars(digest[:5], -4)}{suffix}"

    @classmethod
    def build_api_params(
        cls,
        kind: str,
        item_id: str,
        *,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        timestamp = timestamp or int(time.time())
        nonce = (
            nonce
            or hashlib.md5(f"{timestamp}{random.random()}".encode()).hexdigest().upper()
        )
        path = cls.api_paths[kind]
        params: dict[str, Any] = {
            "os_type": "web",
            "version": "999.0.4",
            "hkey": cls.build_hkey(path, timestamp + 1, nonce),
            "_time": timestamp,
            "nonce": nonce,
        }
        if kind == "bbs":
            params.update(
                {
                    "link_id": item_id,
                    "limit": 20,
                    "web_version": "2.5",
                    "x_client_type": "web",
                    "x_app": "heybox_website",
                    "x_os_type": "Android",
                }
            )
        elif kind == "pc":
            params["steam_appid"] = item_id
        else:
            params["appid"] = item_id
        return params

    @staticmethod
    def extract_identity(url: str) -> tuple[str | None, str | None]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        patterns = (
            ("bbs", r"/bbs/link/([A-Za-z0-9]+)"),
            ("pc", r"/game/pc/([A-Za-z0-9]+)"),
            ("console", r"/game/console/([A-Za-z0-9]+)"),
            ("mobile", r"/game/mobile/([A-Za-z0-9]+)"),
        )
        for kind, pattern in patterns:
            if matched := re.search(pattern, parsed.path):
                return kind, matched.group(1)
        if query.get("link_id"):
            return "bbs", query["link_id"][0]
        game_type = (query.get("game_type") or [""])[0]
        app_id = (query.get("appid") or [""])[0]
        if game_type in {"pc", "console", "mobile"} and app_id:
            return game_type, app_id
        return None, None

    @staticmethod
    def _is_image_url(value: str) -> bool:
        low = value.lower().split("?", 1)[0]
        return value.startswith(("http://", "https://")) and (
            low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
            or any(host in low for host in ("heybox", "xiaoheihe", "img."))
        )

    @classmethod
    def extract_rich_content(cls, value: Any) -> tuple[str, list[str]]:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return cls.extract_rich_content(json.loads(stripped))
                except (TypeError, ValueError):
                    pass
            return stripped, []
        texts: list[str] = []
        images: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                low_key = key.lower()
                if isinstance(item, str) and low_key in {
                    "text",
                    "content",
                    "description",
                    "desc",
                }:
                    text, nested_images = cls.extract_rich_content(item)
                    if text:
                        texts.append(text)
                    images.extend(nested_images)
                elif isinstance(item, str) and low_key in {
                    "url",
                    "src",
                    "image",
                    "image_url",
                    "img",
                }:
                    if cls._is_image_url(item):
                        images.append(item)
                elif isinstance(item, (dict, list)):
                    text, nested_images = cls.extract_rich_content(item)
                    if text:
                        texts.append(text)
                    images.extend(nested_images)
        elif isinstance(value, list):
            for item in value:
                text, nested_images = cls.extract_rich_content(item)
                if text:
                    texts.append(text)
                images.extend(nested_images)
        return "\n".join(dict.fromkeys(texts)), list(dict.fromkeys(images))

    @handle(
        "xiaoheihe.cn",
        r"https?://(?:www\.)?xiaoheihe\.cn/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    async def parse_xiaoheihe(self, searched: Match[str]):
        url = searched.group(0).rstrip(").,;!?，。；！？）]")
        kind, item_id = self.extract_identity(url)
        if not kind or not item_id:
            raise ParseException("小黑盒链接类型或编号无法识别")

        if self.cookie:
            try:
                return await self._parse_api(url, kind, item_id)
            except Exception:
                pass
        return await self._parse_page_fallback(url)

    async def _parse_api(self, url: str, kind: str, item_id: str):
        path = self.api_paths[kind]
        response = await self.http_get(
            f"https://api.xiaoheihe.cn/{path}",
            params=self.build_api_params(kind, item_id),
            headers=self.headers,
            timeout=15,
            retries=2,
        )
        if response.status_code >= 400:
            raise ParseException(f"小黑盒接口请求失败: HTTP {response.status_code}")
        payload = response.json()
        result = payload.get("result")
        if payload.get("status") not in ("ok", None) or not isinstance(result, dict):
            raise ParseException("小黑盒接口未返回有效内容")
        if kind == "bbs":
            link = result.get("link") or {}
            user = link.get("user") or {}
            text, images = self.extract_rich_content(link.get("content"))
            description = str(link.get("description") or "").strip()
            if description and description not in text:
                text = "\n".join(item for item in (description, text) if item)
            cover = link.get("thumb") or link.get("video_thumb")
            if cover:
                images.insert(0, cover)
            images = [
                normalized
                for item in images
                if (normalized := normalize_image_url(item))
            ]
            contents = [
                ImageContent(
                    self.downloader.download_img(item, ext_headers=self.headers)
                )
                for item in list(dict.fromkeys(images))[:12]
            ]
            if link.get("has_video") == 1 and link.get("video_url"):
                contents.append(
                    VideoContent(
                        self.downloader.download_video(
                            link["video_url"],
                            video_name=f"xiaoheihe_{item_id}.mp4",
                            ext_headers=self.headers,
                            max_size_mb=int(
                                self.config.get("performance", {}).get(
                                    "source_max_size", 90
                                )
                            ),
                        )
                    )
                )
            timestamp = link.get("create_at")
            try:
                timestamp = int(timestamp) if timestamp else None
            except (TypeError, ValueError):
                timestamp = None
            return self.result(
                title=link.get("title") or "小黑盒帖子",
                author=self.create_author(user.get("username") or "小黑盒用户"),
                text=text or None,
                contents=contents,
                timestamp=timestamp,
                url=url,
                extra={"send_text": True},
            )

        title = (
            result.get("name")
            or result.get("game_name")
            or result.get("steam_name")
            or "小黑盒游戏"
        )
        text_parts = []
        for key, label in (
            ("description", "简介"),
            ("desc", "简介"),
            ("score", "评分"),
            ("release_date", "发行日期"),
            ("developer", "开发商"),
        ):
            if result.get(key):
                text_parts.append(f"{label}：{result[key]}")
        cover = result.get("image") or result.get("logo") or result.get("game_img")
        contents = []
        if normalized := normalize_image_url(cover):
            contents.append(
                ImageContent(
                    self.downloader.download_img(normalized, ext_headers=self.headers)
                )
            )
        return self.result(
            title=title,
            text="\n".join(text_parts) or None,
            contents=contents,
            url=url,
            extra={"send_text": True},
        )

    async def _parse_page_fallback(self, url: str):
        response = await self.http_get(
            url,
            headers=self.headers,
            timeout=15,
            retries=2,
        )
        if response.status_code >= 400:
            raise ParseException(f"小黑盒页面请求失败: HTTP {response.status_code}")
        metadata = parse_open_graph(response.text)
        if not metadata["title"] and not metadata["description"]:
            if not self.cookie:
                raise ParseException("小黑盒需要有效 Cookie，且页面元数据不可用")
            raise ParseException("小黑盒内容不可见或 Cookie 已失效")
        contents = []
        if metadata["image"]:
            contents.append(
                ImageContent(
                    self.downloader.download_img(
                        metadata["image"], ext_headers=self.headers
                    )
                )
            )
        return self.result(
            title=metadata["title"],
            text=metadata["description"],
            contents=contents,
            url=url,
            extra={
                "send_text": True,
                "info": "未配置有效小黑盒 Cookie，当前仅展示官方页面元数据。",
            },
        )


__all__ = ["XiaoheiheParser"]
