from __future__ import annotations

import hashlib
import json
import random
import re
import time
from html import unescape
from re import Match
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlparse

from astrbot.api import logger

from ..data import ImageContent, Platform, VideoContent
from ..exception import ParseException
from ..utils import normalize_image_url
from .base import BaseParser, handle
from .metadata import parse_open_graph


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
            ("bbs", r"/community/[^/]+/list/([A-Za-z0-9]+)"),
            ("pc", r"/games/detail/([A-Za-z0-9]+)"),
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
    def _extract_html_content(cls, value: str) -> tuple[str, list[str]]:
        images: list[str] = []
        for tag in re.findall(r"<img\b[^>]*>", value, flags=re.IGNORECASE):
            matched = re.search(
                r"\b(?:data-original|data-src|src)=([\"'])(.*?)\1",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if matched and cls._is_image_url(unescape(matched.group(2))):
                images.append(unescape(matched.group(2)))

        def replace_anchor(match: re.Match[str]) -> str:
            attrs, body = match.group(1), match.group(2)
            label = re.sub(r"<[^>]+>", "", body).strip()
            href_match = re.search(
                r"\bhref=([\"'])(.*?)\1",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            href = unescape(href_match.group(2)).replace("\\", "") if href_match else ""
            if label and href.startswith(("http://", "https://")):
                return f"{label} ({href})"
            return label

        text = re.sub(
            r"<a\b([^>]*)>(.*?)</a>",
            replace_anchor,
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"</(?:p|div|h[1-6]|blockquote|li)>|<br\s*/?>",
            "\n",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        lines = [
            re.sub(r"[ \t]+", " ", line).strip() for line in unescape(text).splitlines()
        ]
        return "\n".join(line for line in lines if line), list(dict.fromkeys(images))

    @classmethod
    def extract_rich_content(cls, value: Any) -> tuple[str, list[str]]:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return cls.extract_rich_content(json.loads(stripped))
                except (TypeError, ValueError):
                    pass
            if "<" in stripped and ">" in stripped:
                return cls._extract_html_content(stripped)
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
                    "html",
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
                    "cdn_src",
                    "original",
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

    @staticmethod
    def _canonical_share_url(kind: str, item_id: str) -> str:
        item_id = quote(str(item_id), safe="")
        if kind == "bbs":
            return (
                f"https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id={item_id}"
            )
        return (
            "https://api.xiaoheihe.cn/v3/game/app/api/web/share_game_detail"
            f"?appid={item_id}&game_type={quote(kind, safe='')}"
        )

    @handle(
        "xiaoheihe.cn",
        r"https?://(?:[A-Za-z0-9-]+\.)*xiaoheihe\.cn/"
        r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    async def parse_xiaoheihe(self, searched: Match[str]):
        url = searched.group(0).rstrip(").,;!?，。；！？）]")
        kind, item_id = self.extract_identity(url)
        if not kind or not item_id:
            raise ParseException("小黑盒链接类型或编号无法识别")

        if self.cookie:
            try:
                return await self._parse_api(url, kind, item_id)
            except Exception as exc:
                logger.debug(f"[Xiaoheihe] API 解析失败，回退页面: {exc}")
        if kind != "bbs":
            try:
                return await self._parse_public_game_api(url, kind, item_id)
            except Exception as exc:
                logger.debug(f"[Xiaoheihe] 公开游戏接口失败，回退页面: {exc}")
        return await self._parse_page_fallback(url, kind, item_id)

    async def _parse_public_game_api(self, url: str, kind: str, item_id: str):
        params = {"appid": item_id}
        if kind != "pc":
            params["game_type"] = kind
        response = await self.http_get(
            "https://api.xiaoheihe.cn/game/web/get_game_detail/",
            params=params,
            headers=self.headers,
            timeout=15,
            retries=2,
        )
        if response.status_code >= 400:
            raise ParseException(
                f"小黑盒公开游戏接口请求失败: HTTP {response.status_code}"
            )
        payload = response.json()
        result = payload.get("result")
        if payload.get("status") not in ("ok", None) or not isinstance(result, dict):
            raise ParseException("小黑盒公开游戏接口未返回有效内容")
        if not result:
            raise ParseException("小黑盒公开游戏接口没有该游戏")
        return self._build_game_result(url, kind, item_id, result)

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
            status = str(payload.get("status") or "").strip()
            if status == "show_captcha":
                raise ParseException("小黑盒接口触发验证码，请更新 Cookie 后重试")
            raise ParseException("小黑盒接口未返回有效内容")
        if kind == "bbs":
            link = result.get("link") or {}
            if not isinstance(link, dict) or not link:
                raise ParseException("小黑盒接口没有返回帖子内容")
            user = link.get("user") or {}
            if not isinstance(user, dict):
                user = {}
            text_parts: list[str] = []
            images: list[str] = []
            for source in (link.get("text"), link.get("content")):
                source_text, source_images = self.extract_rich_content(source)
                if source_text and source_text not in text_parts:
                    text_parts.append(source_text)
                images.extend(source_images)
            description = str(link.get("description") or "").strip()
            if description and not any(description in item for item in text_parts):
                text_parts.insert(0, description)

            tags = []
            for item in link.get("hashtags") or link.get("content_tags") or []:
                if not isinstance(item, dict):
                    continue
                tag = str(item.get("name") or item.get("text") or "").strip()
                if tag:
                    tags.append(f"#{tag}")
            if tags:
                text_parts.append(" ".join(dict.fromkeys(tags[:10])))

            text = "\n\n".join(text_parts).strip()
            if len(text) > 6000:
                text = f"{text[:5997]}..."
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
            if link.get("has_video") in (1, "1", True) and link.get("video_url"):
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
            avatar = normalize_image_url(
                user.get("avatar") or user.get("avatar_url") or user.get("head_url")
            )
            extra = {"render_text_card": True}
            if avatar:
                extra["text_card_avatar"] = avatar
            return self.result(
                title=link.get("title") or "小黑盒帖子",
                author=self.create_author(
                    user.get("username") or user.get("nickname") or "小黑盒用户",
                    avatar,
                ),
                text=text or None,
                contents=contents,
                timestamp=timestamp,
                url=url,
                extra=extra,
            )

        return self._build_game_result(url, kind, item_id, result)

    def _build_game_result(
        self,
        url: str,
        kind: str,
        item_id: str,
        result: dict[str, Any],
    ):
        title = (
            result.get("name")
            or result.get("game_name")
            or result.get("steam_name")
            or "小黑盒游戏"
        )
        text_parts = []
        description = (
            result.get("about_the_game")
            or result.get("description")
            or result.get("desc")
        )
        if description:
            clean_description, description_images = self.extract_rich_content(
                description
            )
            if clean_description:
                text_parts.append(f"简介：{clean_description}")
        else:
            description_images = []
        if result.get("score"):
            comment_stats = result.get("comment_stats") or {}
            comment_count = (
                comment_stats.get("score_comment")
                if isinstance(comment_stats, dict)
                else None
            )
            suffix = f"（{comment_count} 人评价）" if comment_count else ""
            text_parts.append(f"评分：{result['score']}{suffix}")

        menu = {
            str(item.get("type")): item.get("value")
            for item in result.get("menu_v2") or []
            if isinstance(item, dict) and item.get("value")
        }
        for key, label in (("release_date", "发行日期"), ("developer", "开发商")):
            value = menu.get(key) or result.get(key)
            if value:
                text_parts.append(f"{label}：{value}")
        developers = []
        for item in result.get("developers") or []:
            if isinstance(item, dict):
                item = item.get("value") or item.get("name")
            if value := str(item or "").strip():
                developers.append(value)
        if developers and not menu.get("developer") and not result.get("developer"):
            text_parts.append(
                f"开发商：{', '.join(item for item in developers if item)}"
            )
        publisher_values = []
        for item in result.get("publishers") or []:
            if isinstance(item, dict):
                item = item.get("value") or item.get("name")
            if value := str(item or "").strip():
                publisher_values.append(value)
        publishers = menu.get("publisher") or ", ".join(publisher_values)
        if publishers:
            text_parts.append(f"发行商：{publishers}")
        if result.get("platforms"):
            text_parts.append(f"支持平台：{' / '.join(map(str, result['platforms']))}")

        price = result.get("price") or {}
        if isinstance(price, dict) and price.get("current") is not None:
            price_text = f"当前价格：¥{price['current']}"
            if price.get("initial") not in (None, price.get("current")):
                price_text += f"（原价 ¥{price['initial']}）"
            text_parts.append(price_text)
        cover = result.get("image") or result.get("logo") or result.get("game_img")
        contents = []
        if normalized := normalize_image_url(cover):
            contents.append(
                ImageContent(
                    self.downloader.download_img(normalized, ext_headers=self.headers)
                )
            )
        screenshots = list(description_images)
        video_url = None
        for media in result.get("screenshots") or []:
            if not isinstance(media, dict):
                continue
            media_url = normalize_image_url(media.get("url") or media.get("thumbnail"))
            if media.get("type") == "movie":
                video_url = media.get("url") or video_url
            elif media_url:
                screenshots.append(media_url)
        for media_url in list(dict.fromkeys(screenshots))[:8]:
            contents.append(
                ImageContent(
                    self.downloader.download_img(media_url, ext_headers=self.headers)
                )
            )
        if video_url:
            contents.append(
                VideoContent(
                    self.downloader.download_video(
                        video_url,
                        video_name=f"xiaoheihe_{kind}_{item_id}.mp4",
                        ext_headers=self.headers,
                        max_size_mb=int(
                            self.config.get("performance", {}).get(
                                "source_max_size", 90
                            )
                        ),
                    )
                )
            )
        text = "\n".join(text_parts)
        if len(text) > 6000:
            text = f"{text[:5997]}..."
        return self.result(
            title=title,
            text=text or None,
            contents=contents,
            url=url,
            extra={"render_text_card": True},
        )

    async def _parse_page_fallback(self, url: str, kind: str, item_id: str):
        canonical_url = self._canonical_share_url(kind, item_id)
        candidates = [canonical_url]
        if url != canonical_url:
            candidates.append(url)

        page_title = ""
        page_description = ""
        page_image = ""
        last_status: int | None = None
        for candidate in candidates:
            try:
                response = await self.http_get(
                    candidate,
                    headers=self.headers,
                    timeout=15,
                    retries=2,
                )
            except Exception as exc:
                logger.debug(f"[Xiaoheihe] 页面候选请求失败 {candidate}: {exc}")
                continue
            last_status = response.status_code
            if response.status_code >= 400:
                continue

            metadata = parse_open_graph(response.text)
            redirect_metadata = {
                **self._parse_redirect_metadata(candidate),
                **self._parse_redirect_metadata(str(response.url)),
            }
            page_title = redirect_metadata.get("title") or metadata["title"]
            page_description = (
                redirect_metadata.get("description") or metadata["description"]
            )
            page_image = metadata["image"]
            if (
                page_title in {"小黑盒 - 玩家高能聚集地", "小黑盒 - 高能玩家聚集地"}
                and not page_description
                and not page_image
            ):
                page_title = ""
                continue
            if page_title or page_description or page_image:
                break

        if not page_title and not page_description and not page_image:
            if last_status and last_status >= 400:
                raise ParseException(f"小黑盒页面请求失败: HTTP {last_status}")
            if not self.cookie:
                raise ParseException("小黑盒公开分享信息不可用，请配置有效 Cookie")
            raise ParseException("小黑盒内容不可见或 Cookie 已失效")
        contents = []
        if normalized_image := normalize_image_url(page_image):
            contents.append(
                ImageContent(
                    self.downloader.download_img(
                        normalized_image, ext_headers=self.headers
                    )
                )
            )
        return self.result(
            title=page_title,
            text=page_description,
            contents=contents,
            url=url,
            extra={
                "render_text_card": True,
                "info": (
                    "小黑盒接口不可用，当前展示官方分享信息。"
                    if self.cookie
                    else "未配置有效小黑盒 Cookie，当前展示官方分享信息。"
                ),
            },
        )

    @staticmethod
    def _parse_redirect_metadata(url: str) -> dict[str, str]:
        raw = (parse_qs(urlparse(url).query).get("redirect_data") or [""])[0]
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        link = payload.get("link") if isinstance(payload, dict) else None
        if not isinstance(link, dict):
            return {}
        return {
            key: text
            for key in ("title", "description")
            if (text := str(link.get(key) or "").strip())
        }


__all__ = ["XiaoheiheParser"]
