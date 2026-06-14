import hashlib
import re
import time
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from re import Match
from typing import ClassVar

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent, ImageContent
from ..download import Downloader
from ..text_renderer import TextCardRenderer
from ..utils import image_to_data_uri, normalize_image_url
from .base import BaseParser, handle, ParseException


class WeiboParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="weibo", display_name="微博")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({
            "Referer": "https://m.weibo.cn/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.162 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "MWeibo-Pwa": "1",
            "X-Requested-With": "XMLHttpRequest"
        })
        self.cache_dir = Path(config["cache_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.text_renderer = TextCardRenderer()

    @handle("weibo.com", r"weibo\.com/[0-9]+/([a-zA-Z0-9]+)")
    @handle("weibo.cn", r"weibo\.cn/(?:status|detail)/([a-zA-Z0-9]+)")
    async def _parse_weibo(self, searched: Match[str]):
        bid = searched.group(1)
        url = f"https://m.weibo.cn/statuses/show?id={bid}"
        
        logger.debug(f"[Weibo] 尝试 API 解析: {url}")
        
        try:
            resp = await self.client.get(url, headers=self.headers, timeout=8)
            if resp.status_code != 200:
                raise ParseException(f"微博 API 请求失败: HTTP {resp.status_code}")
            
            data = resp.json()
        except ParseException:
            raise
        except Exception as e:
            raise ParseException(f"连接微博 API 失败: {e}") from e

        if not isinstance(data, dict) or data.get("ok") != 1:
            raise ParseException(f"微博 API 返回错误: {data.get('msg') if isinstance(data, dict) else data}")

        data = data.get("data", {})
        if not data:
             raise ParseException("未获取到微博数据")
        
        user = data.get("user", {})
        author_name = user.get("screen_name", "微博用户")
        author_avatar = normalize_image_url(
            user.get("avatar_hd")
            or user.get("avatar_large")
            or user.get("profile_image_url", "")
        ) or ""
        
        text = data.get("text", "")
        if data.get("isLongText") and "longText" in data:
             text = data["longText"].get("longTextContent", text)
        
        text = self._html_to_plain_text(text)
        
        timestamp = None
        if created_at := data.get("created_at"):
            try:
                dt = parsedate_to_datetime(created_at)
                timestamp = int(dt.timestamp())
            except Exception:
                pass
        
        contents = []

        page_info = data.get("page_info") or {}
        if isinstance(page_info, dict) and page_info.get("type") == "video":
            media_info = page_info.get("media_info") or {}
            if not isinstance(media_info, dict):
                media_info = {}
            video_url = self._pick_video_url(page_info)
            if video_url:
                duration = media_info.get("duration", 0)
                
                video_task = self.downloader.download_video(
                    video_url, 
                    video_name=f"weibo_{bid}",
                    ext_headers=self.headers
                )
                # 提纯：不下载封面
                contents.append(VideoContent(video_task, None, duration=duration))

        for url in self._collect_static_pic_urls(data):
            img_task = self.downloader.download_img(
                url,
                ext_headers=self.headers,
            )
            contents.append(ImageContent(img_task))

        # 移除了评论区抓取逻辑

        extra = {}
        if text and not contents:
            text_card_avatar = await self._img_to_data_uri(author_avatar) or author_avatar
            if text_card_avatar:
                extra["text_card_avatar"] = text_card_avatar

            try:
                text_card = await self._render_text_card(
                    bid=bid,
                    author_name=author_name,
                    author_avatar=text_card_avatar,
                    text=text,
                    timestamp=timestamp,
                )
                contents.append(text_card)
            except Exception as e:
                logger.warning(f"[Weibo] 正文卡渲染失败: {e}")

        author = self.create_author(author_name, author_avatar, ext_headers=self.headers)
        original_url = f"https://weibo.com/{user.get('id')}/{bid}"

        return self.result(
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=original_url,
            extra=extra,
        )

    @staticmethod
    def _pick_video_url(page_info: dict) -> str | None:
        if not isinstance(page_info, dict):
            return None

        media_info = page_info.get("media_info") or {}
        urls = page_info.get("urls") or {}

        if not isinstance(media_info, dict):
            media_info = {}
        if not isinstance(urls, dict):
            urls = {}

        candidates = (
            urls.get("mp4_720p_mp4"),
            urls.get("mp4_hd_mp4"),
            urls.get("mp4_ld_mp4"),
            media_info.get("mp4_720p_mp4"),
            media_info.get("mp4_hd_url"),
            media_info.get("mp4_sd_url"),
            media_info.get("stream_url_hd"),
            media_info.get("stream_url"),
        )
        for url in candidates:
            if isinstance(url, str) and url:
                return url
        return None

    @classmethod
    def _collect_static_pic_urls(cls, data: dict) -> list[str]:
        if not isinstance(data, dict):
            return []

        pid_to_url: dict[str, str] = {}
        ordered_pids: list[str] = []
        raw_urls: list[str] = []

        def add_pid(pid: str | None) -> None:
            if isinstance(pid, str) and pid and pid not in ordered_pids:
                ordered_pids.append(pid)

        pics = data.get("pics")
        if isinstance(pics, list):
            for pic in pics:
                if not isinstance(pic, dict) or cls._is_video_pic(pic):
                    continue

                pid = pic.get("pid")
                add_pid(pid)

                url = normalize_image_url(cls._pick_static_pic_url(pic))
                if not url:
                    continue
                if isinstance(pid, str) and pid:
                    pid_to_url[pid] = url
                else:
                    raw_urls.append(url)

        pic_infos = data.get("pic_infos")
        if isinstance(pic_infos, dict):
            for pid, pic in pic_infos.items():
                if not isinstance(pic, dict) or cls._is_video_pic(pic):
                    continue

                pid_str = str(pid)
                add_pid(pid_str)

                url = normalize_image_url(cls._pick_static_pic_url(pic))
                if url:
                    pid_to_url[pid_str] = url

        pic_ids = data.get("pic_ids")
        if isinstance(pic_ids, list):
            for pid in pic_ids:
                if isinstance(pid, str):
                    add_pid(pid)

        urls: list[str] = []
        seen: set[str] = set()

        for pid in ordered_pids:
            url = pid_to_url.get(pid) or cls._build_pic_url_from_pid(pid)
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)

        for url in raw_urls:
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        return urls

    @staticmethod
    def _is_video_pic(pic: dict) -> bool:
        pic_type = str(pic.get("type") or "").lower()
        return pic_type == "video" or pic_type.endswith("_video")

    @staticmethod
    def _build_pic_url_from_pid(pid: str) -> str | None:
        if not re.fullmatch(r"[0-9a-zA-Z]+", pid or ""):
            return None
        return f"https://wx1.sinaimg.cn/mw2000/{pid}.jpg"

    @staticmethod
    def _pick_static_pic_url(pic: dict) -> str | None:
        if not isinstance(pic, dict):
            return None

        for key in ("large", "original", "bmiddle", "thumbnail"):
            item = pic.get(key)
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url

        url = pic.get("url")
        return url if isinstance(url, str) and url else None

    @staticmethod
    def _fmt_time(ts: int | None) -> str | None:
        if not ts:
            return None
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception:
            return None

    @staticmethod
    def _html_to_plain_text(text: str | None) -> str:
        if not text:
            return ""

        text = str(text)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

        def replace_img(match: re.Match[str]) -> str:
            tag = match.group(0)
            attr = re.search(
                r"\b(?:alt|title)=([\"'])(.*?)\1",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not attr:
                return ""
            return unescape(attr.group(2))

        text = re.sub(
            r"<img\b[^>]*>",
            replace_img,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<[^>]+>", "", text)
        return unescape(text).strip()

    async def _render_text_card(
        self,
        *,
        bid: str,
        author_name: str,
        author_avatar: str | None,
        text: str,
        timestamp: int | None,
    ) -> ImageContent:
        avatar_digest = (
            hashlib.md5(author_avatar.encode("utf-8")).hexdigest()
            if author_avatar
            else ""
        )
        digest = hashlib.md5(
            "\n".join(
                [
                    bid,
                    author_name,
                    avatar_digest,
                    self._fmt_time(timestamp) or "",
                    text,
                    "weibo_text_card_v3",
                ]
            ).encode("utf-8")
        ).hexdigest()[:12]

        out_path = self.cache_dir / f"weibo_text_card_{bid}_{digest}.png"
        if not out_path.exists():
            await self.text_renderer.render_text_card(
                out_path=out_path,
                platform_name=self.platform.display_name,
                author_name=author_name,
                author_avatar=author_avatar,
                text=text,
                timestamp_text=self._fmt_time(timestamp),
            )

        return ImageContent(out_path)

    async def _img_to_data_uri(
        self,
        url: str | None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> str | None:
        return await image_to_data_uri(
            self.http_get,
            url,
            headers=self.headers,
            referer="https://m.weibo.cn/",
            max_bytes=max_bytes,
            timeout=10,
            debug_label="[Weibo] image",
        )
