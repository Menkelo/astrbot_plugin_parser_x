import base64
import hashlib
import mimetypes
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
            resp = await self.client.get(url, headers=self.headers)
            if resp.status_code != 200:
                logger.debug(f"微博 API 请求失败: {resp.status_code}，尝试 fallback")
                return await self._parse_with_ytdlp(searched.group(0))
            
            data = resp.json()
        except Exception as e:
            logger.debug(f"连接微博 API 失败: {e}，尝试 fallback")
            return await self._parse_with_ytdlp(searched.group(0))

        if not data or data.get("ok") != 1:
            logger.debug(f"微博 API 返回错误 ({data.get('msg')})，尝试 fallback")
            return await self._parse_with_ytdlp(searched.group(0))

        data = data.get("data", {})
        if not data:
             raise ParseException("未获取到微博数据")
        
        user = data.get("user", {})
        author_name = user.get("screen_name", "微博用户")
        author_avatar = self._norm_img(
            user.get("avatar_hd")
            or user.get("avatar_large")
            or user.get("profile_image_url", "")
        )
        
        text = data.get("text", "")
        if data.get("isLongText") and "longText" in data:
             text = data["longText"].get("longTextContent", text)
        
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text).strip()
        
        timestamp = None
        if created_at := data.get("created_at"):
            try:
                dt = parsedate_to_datetime(created_at)
                timestamp = int(dt.timestamp())
            except Exception:
                pass
        
        contents = []
        image_urls: list[str] = []
        has_video = False

        page_info = data.get("page_info", {})
        if page_info and page_info.get("type") == "video":
            media_info = page_info.get("media_info", {})
            video_url = (
                media_info.get("mp4_720p_mp4") or 
                media_info.get("mp4_hd_url") or 
                media_info.get("mp4_sd_url") or
                media_info.get("stream_url")
            )
            if video_url:
                duration = media_info.get("duration", 0)
                
                video_task = self.downloader.download_video(
                    video_url, 
                    video_name=f"weibo_{bid}",
                    ext_headers=self.headers
                )
                # 提纯：不下载封面
                contents.append(VideoContent(video_task, None, duration=duration))
                has_video = True

        if "pics" in data:
            for pic in data["pics"]:
                large = pic.get("large", {})
                url = large.get("url") or pic.get("url")
                if url:
                    image_urls.append(url)
                    img_task = self.downloader.download_img(
                        url, 
                        ext_headers=self.headers
                    )
                    contents.append(ImageContent(img_task))

        # 移除了评论区抓取逻辑

        extra = {}
        if text:
            text_card_avatar = await self._img_to_data_uri(author_avatar) or author_avatar
            if text_card_avatar:
                extra["text_card_avatar"] = text_card_avatar

            if not has_video:
                try:
                    text_card = await self._render_text_card(
                        bid=bid,
                        author_name=author_name,
                        author_avatar=text_card_avatar,
                        text=text,
                        timestamp=timestamp,
                        image_urls=image_urls,
                    )
                    contents.insert(0, text_card)
                except Exception as e:
                    logger.warning(f"[Weibo] 正文卡渲染失败: {e}")

        author = self.create_author(author_name, author_avatar, ext_headers=self.headers)
        original_url = f"https://weibo.com/{user.get('id')}/{bid}"

        return self.result(
            title="微博正文",
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=original_url,
            extra=extra,
        )

    @staticmethod
    def _fmt_time(ts: int | None) -> str | None:
        if not ts:
            return None
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception:
            return None

    async def _render_text_card(
        self,
        *,
        bid: str,
        author_name: str,
        author_avatar: str | None,
        text: str,
        timestamp: int | None,
        image_urls: list[str],
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
                    "|".join(image_urls),
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

    @staticmethod
    def _norm_img(url: str | None) -> str:
        if not url:
            return ""

        url = str(url).strip()
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http://"):
            return "https://" + url[len("http://") :]
        return url

    async def _img_to_data_uri(
        self,
        url: str | None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> str | None:
        url = self._norm_img(url)
        if not url:
            return None

        headers = self.headers.copy()
        headers.update(
            {
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": "https://m.weibo.cn/",
            }
        )

        try:
            resp = await self.http_get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=10,
            )
            if resp.status_code >= 400:
                return None

            content = resp.content or b""
            if not content or len(content) > max_bytes:
                return None

            content_type = (
                (resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
            )
            if not content_type.lower().startswith("image/"):
                guessed, _ = mimetypes.guess_type(url)
                content_type = guessed or "image/jpeg"

            encoded = base64.b64encode(content).decode("ascii")
            return f"data:{content_type};base64,{encoded}"

        except Exception as e:
            logger.debug(f"[Weibo] 头像转 data URI 失败: {url} | {e}")
            return None

    async def _parse_with_ytdlp(self, url: str):
        if not url.startswith("http"):
            url = f"https://{url}"
            
        logger.debug(f"[Weibo] 使用 yt-dlp 兜底解析: {url}")
        
        info = await self.downloader.ytdlp_extract_info(url)
        contents = []
        
        if info.duration:
            video_task = self.downloader.download_video(
                url, 
                use_ytdlp=True, 
                video_name=info.title
            )
            # 提纯：不下载封面
            contents.append(VideoContent(video_task, None, duration=info.duration))
        
        if not contents and info.thumbnail:
            img_task = self.downloader.download_img(info.thumbnail)
            contents.append(ImageContent(img_task))

        author = self.create_author(info.uploader or "微博用户")

        return self.result(
            title=info.title or "微博正文",
            text=info.description or "",
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
        )
