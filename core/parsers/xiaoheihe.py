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
from ..utils import normalize_image_url
from .base import BaseParser, handle
from .metadata import parse_open_graph
from .xiaoheihe_comment import XiaoheiheCommentFeed


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
        self.render_service = HtmlRenderService.from_config(config)
        comment_settings = CommentSettings.from_config(config, "xiaoheihe")
        self.enable_comment_card = comment_settings.enabled
        self.comment_limit = comment_settings.display_count
        self.comment_timeout = comment_settings.timeout
        self.comment_canvas = SocialCommentCanvas(self.render_service)
        self.comment_feed = XiaoheiheCommentFeed(
            self,
            self.comment_canvas,
            limit=self.comment_limit,
        )

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service
        self.comment_canvas.render_service = render_service

    def _comment_extra(
        self,
        link_id: str,
        threads: list[dict],
        *,
        title: str,
        cover: str | None,
        owner_id: str | int | None,
        total: int | None,
    ) -> dict:
        if not self.enable_comment_card or not link_id or not threads:
            return {}

        async def build_comments():
            return self.comment_feed.build_images(
                link_id,
                threads,
                work_title=title,
                cover=cover,
                owner_id=owner_id,
                total=total,
            )

        return {
            "comment_task_factory": build_comments,
            "comment_timeout": self.comment_timeout,
        }

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

    @classmethod
    def _extract_html_blocks(cls, value: str) -> list[tuple[str, str]]:
        blocks: list[tuple[str, str]] = []
        parts = re.split(
            r"(<img\b[^>]*>)",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for part in parts:
            if not part:
                continue
            if re.match(r"<img\b", part, flags=re.IGNORECASE):
                matched = re.search(
                    r"\b(?:data-original|data-src|src)=([\"'])(.*?)\1",
                    part,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if matched:
                    image_url = unescape(matched.group(2)).replace("\\", "")
                    if cls._is_image_url(image_url):
                        blocks.append(("image", image_url))
                continue

            text, _ = cls._extract_html_content(part)
            if text:
                blocks.append(("text", text))
        return blocks

    @classmethod
    def extract_rich_blocks(cls, value: Any) -> list[tuple[str, str]]:
        """Return ordered text/image blocks from Xiaoheihe rich content."""

        blocks: list[tuple[str, str]] = []

        def append(kind: str, item: str) -> None:
            item = str(item or "").strip()
            if not item:
                return
            if kind == "image":
                if any(old_kind == "image" and old == item for old_kind, old in blocks):
                    return
                blocks.append((kind, item))
                return
            if blocks and blocks[-1][0] == "text":
                previous = blocks[-1][1]
                if item == previous or item in previous:
                    return
                blocks[-1] = ("text", f"{previous}\n\n{item}")
                return
            blocks.append((kind, item))

        def walk(item: Any) -> None:
            if isinstance(item, str):
                stripped = item.strip()
                if not stripped:
                    return
                if stripped.startswith(("{", "[")):
                    try:
                        walk(json.loads(stripped))
                        return
                    except (TypeError, ValueError):
                        pass
                if "<" in stripped and ">" in stripped:
                    for kind, value_part in cls._extract_html_blocks(stripped):
                        append(kind, value_part)
                    return
                if cls._is_image_url(stripped):
                    append("image", stripped)
                else:
                    append("text", stripped)
                return

            if isinstance(item, list):
                for child in item:
                    walk(child)
                return

            if not isinstance(item, dict):
                return

            for key, child in item.items():
                low_key = str(key).lower()
                if isinstance(child, str) and low_key in {
                    "url",
                    "src",
                    "image",
                    "image_url",
                    "img",
                    "cdn_src",
                    "original",
                }:
                    if cls._is_image_url(child):
                        append("image", child)
                    continue
                if low_key in {
                    "type",
                    "id",
                    "width",
                    "height",
                    "size",
                }:
                    continue
                if isinstance(child, (str, dict, list)):
                    walk(child)

        walk(value)
        return blocks

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

        try:
            return await self._parse_api(url, kind, item_id)
        except Exception as exc:
            logger.debug(f"[Xiaoheihe] 签名 API 解析失败，尝试公开回退: {exc}")
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

            rich_blocks: list[tuple[str, str]] = []
            for source in (link.get("text"), link.get("content")):
                for block in self.extract_rich_blocks(source):
                    if block not in rich_blocks:
                        rich_blocks.append(block)

            description = str(link.get("description") or "").strip()
            tags = []
            for item in link.get("hashtags") or link.get("content_tags") or []:
                if not isinstance(item, dict):
                    continue
                tag = str(item.get("name") or item.get("text") or "").strip()
                if tag:
                    tags.append(f"#{tag}")

            title = str(link.get("title") or "小黑盒帖子").strip()
            author_name = str(
                user.get("username") or user.get("nickname") or "小黑盒用户"
            ).strip()
            overview_lines = ["识别：小黑盒帖子", f"👤作者：{author_name}"]
            if title:
                overview_lines.append(f"📝标题：{title}")
            if description:
                overview_lines.append(f"📄简介：{description}")
            if tags:
                overview_lines.append(f"🏷️标签：{' '.join(dict.fromkeys(tags[:10]))}")
            overview = "\n".join(overview_lines)

            text_parts = [description] if description else []
            text_parts.extend(
                value for block_type, value in rich_blocks if block_type == "text"
            )
            text = "\n\n".join(dict.fromkeys(text_parts)).strip()
            if len(text) > 6000:
                text = f"{text[:5997]}..."

            cover_url = normalize_image_url(
                link.get("thumb") or link.get("video_thumb")
            )
            cover_content = None
            if cover_url:
                cover_content = ImageContent(
                    self.downloader.download_img(
                        cover_url,
                        ext_headers=self.headers,
                    )
                )

            body_parts: list[str | ImageContent] = []
            image_contents: list[ImageContent] = []
            image_count = 0
            for block_type, value in rich_blocks:
                if block_type == "text":
                    body_parts.append(value)
                    continue
                normalized = normalize_image_url(value)
                if not normalized or normalized == cover_url or image_count >= 20:
                    continue
                content = ImageContent(
                    self.downloader.download_img(
                        normalized,
                        ext_headers=self.headers,
                    )
                )
                image_contents.append(content)
                body_parts.append(content)
                image_count += 1

            video_contents: list[VideoContent] = []
            if link.get("has_video") in (1, "1", True) and link.get("video_url"):
                video_contents.append(
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

            overview_parts: list[str | ImageContent] = []
            if cover_content is not None:
                overview_parts.append(cover_content)
            overview_parts.append(overview)
            batches = [DeliveryBatch(overview_parts)]
            if body_parts:
                batches.append(DeliveryBatch(body_parts, mode="forward"))
            batches.extend(DeliveryBatch([video]) for video in video_contents)

            contents = [
                *([cover_content] if cover_content is not None else []),
                *image_contents,
                *video_contents,
            ]
            timestamp = link.get("create_at")
            try:
                timestamp = int(timestamp) if timestamp else None
            except (TypeError, ValueError):
                timestamp = None
            avatar = normalize_image_url(
                user.get("avatar") or user.get("avatar_url") or user.get("head_url")
            )
            threads = [
                item for item in result.get("comments") or [] if isinstance(item, dict)
            ]
            total_value = (
                link.get("comment_num")
                or link.get("comment_count")
                or result.get("comment_num")
                or result.get("comment_count")
            )
            try:
                total = int(total_value) if total_value is not None else None
            except (TypeError, ValueError):
                total = None
            owner_id = user.get("userid") or user.get("user_id") or user.get("id")
            extra = self._comment_extra(
                item_id,
                threads,
                title=title,
                cover=cover_url,
                owner_id=owner_id,
                total=total,
            )
            return self.result(
                title=title,
                author=self.create_author(
                    author_name,
                    avatar,
                ),
                text=text or None,
                contents=contents,
                delivery=DeliveryPlan(batches),
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
        title = str(
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
        cover_url = normalize_image_url(
            result.get("image") or result.get("logo") or result.get("game_img")
        )
        cover_content = None
        if cover_url:
            cover_content = ImageContent(
                self.downloader.download_img(
                    cover_url,
                    ext_headers=self.headers,
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
        screenshot_contents = []
        for media_url in list(dict.fromkeys(screenshots))[:8]:
            if media_url == cover_url:
                continue
            screenshot_contents.append(
                ImageContent(
                    self.downloader.download_img(media_url, ext_headers=self.headers)
                )
            )
        video_contents = []
        if video_url:
            video_contents.append(
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

        overview_lines = ["识别：小黑盒游戏", f"🕹️ {title}"]
        if result.get("score"):
            comment_stats = result.get("comment_stats") or {}
            comment_count = (
                comment_stats.get("score_comment")
                if isinstance(comment_stats, dict)
                else None
            )
            score_text = f"🌟 小黑盒评分：{result['score']}"
            if comment_count:
                score_text += f"（{comment_count} 人评价）"
            overview_lines.append(score_text)
        if isinstance(price, dict) and price.get("current") is not None:
            overview_lines.append(f"💰 当前价格：¥{price['current']}")

        overview_parts: list[str | ImageContent] = []
        if cover_content is not None:
            overview_parts.append(cover_content)
        overview_parts.append("\n".join(overview_lines))
        batches = [DeliveryBatch(overview_parts)]
        detail_parts: list[str | ImageContent] = []
        if text:
            detail_parts.append(text)
        if screenshot_contents:
            detail_parts.extend(["🖼️ 游戏截图", *screenshot_contents])
        if detail_parts:
            batches.append(DeliveryBatch(detail_parts, mode="forward"))
        batches.extend(DeliveryBatch([video]) for video in video_contents)

        contents = [
            *([cover_content] if cover_content is not None else []),
            *screenshot_contents,
            *video_contents,
        ]
        return self.result(
            title=title,
            text=text or None,
            contents=contents,
            delivery=DeliveryPlan(batches),
            url=url,
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
            candidate_redirect = self._parse_redirect_metadata(candidate)
            if candidate_redirect:
                page_title = candidate_redirect.get("title", page_title)
                page_description = candidate_redirect.get(
                    "description", page_description
                )
            try:
                response = await self.http_get(
                    candidate,
                    headers=self.headers,
                    timeout=15,
                    retries=2,
                )
            except Exception as exc:
                logger.debug(f"[Xiaoheihe] 页面候选请求失败 {candidate}: {exc}")
                if page_title or page_description:
                    break
                continue
            last_status = response.status_code
            if response.status_code >= 400:
                if page_title or page_description:
                    break
                continue

            metadata = parse_open_graph(response.text)
            redirect_metadata = {
                **candidate_redirect,
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
        fallback_info = (
            "小黑盒接口不可用，当前展示官方分享信息。"
            if self.cookie
            else "签名接口不可用，当前展示官方分享信息。"
        )
        summary_lines = [f"识别：{'小黑盒帖子' if kind == 'bbs' else '小黑盒游戏'}"]
        if page_title:
            summary_lines.append(f"📝标题：{page_title}")
        if page_description:
            summary_lines.append(f"📄简介：{page_description}")
        summary_lines.append(f"提示：{fallback_info}")
        delivery_parts: list[str | ImageContent] = [*contents, "\n".join(summary_lines)]
        return self.result(
            title=page_title,
            text=page_description,
            contents=contents,
            delivery=DeliveryPlan([DeliveryBatch(delivery_parts)]),
            url=url,
            extra={"info": fallback_info},
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
