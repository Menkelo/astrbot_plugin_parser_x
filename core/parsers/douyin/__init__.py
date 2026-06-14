import asyncio
import re
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...download import Downloader
from ..base import BaseParser, ParseException, Platform, SkipParseException, handle
from ..bilibili.comment_renderer import BiliCommentRenderer
from .comment_service import DouyinCommentService
from .extractor import (
    extract_id_from_query,
    extract_router_data_json_str,
    extract_static_image_urls_deep,
    pick_primary_aweme,
)

if TYPE_CHECKING:
    from ...data import ParseResult


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

        self.cookies = ""
        ck_conf = config.get("cookies", {})
        if isinstance(ck_conf, dict):
            self.cookies = ck_conf.get("douyin_ck", "")

        if self.cookies:
            self._set_cookies(self.cookies)

        self.enable_comment_card = bool(config.get("douyin_comment", True))
        comment_conf = config.get("comment_filter", {})
        if not isinstance(comment_conf, dict):
            comment_conf = {}

        self.comment_renderer = BiliCommentRenderer()
        self.comment_service = DouyinCommentService(
            self,
            renderer=self.comment_renderer,
            comment_limit=9,
            enable_text_ad_filter=bool(comment_conf.get("enable_text_ad_filter", True)),
        )

    def _set_cookies(self, cookies: str):
        cleaned = cookies.replace("\n", "").replace("\r", "").strip()
        if cleaned:
            self.ios_headers["Cookie"] = cleaned
            self.android_headers["Cookie"] = cleaned

    def _create_comment_task(
        self,
        aweme_id: str,
        *,
        title: str,
        cover: str | None,
        author: str | None,
        timestamp: int | None,
        referer: str,
    ) -> asyncio.Task | None:
        if not self.enable_comment_card or not aweme_id:
            return None

        return asyncio.create_task(
            self.comment_service.build_comment_image_content(
                aweme_id,
                video_title=title,
                video_cover=cover,
                video_author=author,
                video_timestamp=timestamp,
                referer=referer,
            ),
            name=f"douyin_comments_{aweme_id}",
        )

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    @staticmethod
    def _is_live_url(url: str) -> bool:
        u = (url or "").lower()
        live_keys = [
            "live.douyin.com",
            "webcast.douyin.com",
            "webcast.amemv.com",
            "/live/",
            "reflow/",
            "live_room",
            "enterfrom=live",
        ]
        return any(k in u for k in live_keys)

    def _create_image_contents_with_headers(self, urls: list[str], headers: dict[str, str]):
        """
        兼容不同版本 BaseParser.create_image_contents：
        - 支持 ext_headers 就带 headers；
        - 不支持就退回旧调用。
        """
        if not urls:
            return []

        try:
            return self.create_image_contents(urls, ext_headers=headers)
        except TypeError:
            return self.create_image_contents(urls)

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        for u in urls:
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)

        return out

    @staticmethod
    def _is_audio_like_url(url: str) -> bool:
        low = (url or "").lower()
        return any(
            x in low
            for x in (
                ".mp3",
                ".m4a",
                ".aac",
                ".wav",
                "mime_type=audio",
                "ies-music",
            )
        )

    @staticmethod
    def _is_video_like_url(url: str) -> bool:
        low = (url or "").lower()
        if DouyinParser._is_audio_like_url(url):
            return False
        if DouyinParser._has_url_as_video_id(url):
            return False
        return any(
            x in low
            for x in (
                ".mp4",
                ".m3u8",
                "video_id=",
                "mime_type=video",
                "/video/",
                "playwm",
                "aweme/v1/play",
                "api-play",
                "is_play_url=1",
            )
        )

    @staticmethod
    def _has_url_as_video_id(url: str) -> bool:
        try:
            query = parse_qs(urlparse(url).query)
        except Exception:
            return False

        for key in ("video_id", "vid"):
            for value in query.get(key) or []:
                if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
                    return True

        return False

    @staticmethod
    def _has_image_album(aweme: dict) -> bool:
        images = aweme.get("images")
        return isinstance(images, list) and len(images) > 0

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        short_url = f"https://{searched.group(0)}"
        final_url = await self.get_final_url(short_url, headers=self.ios_headers)

        if self._is_live_url(final_url):
            raise SkipParseException()

        try:
            keyword, m = self.search_url(final_url)
        except Exception:
            keyword, m = None, None

        if keyword and m:
            try:
                return await self.parse(keyword, m)
            except Exception as e:
                logger.warning(f"[Douyin] 短链路由后解析失败，尝试ID兜底: {e}")

        vid = extract_id_from_query(final_url)
        if vid and not self._is_live_url(final_url):
            try:
                return await self._parse_by_id_fallback(vid)
            except Exception as e:
                raise ParseException(f"短链解析失败，ID兜底失败: {e} | 最终链接: {final_url}")

        raise ParseException(f"短链解析失败，无法识别最终链接: {final_url}")

    @handle(
        "modal_id",
        r"(?:https?://)?(?:www\.)?douyin\.com/\S*[?&]modal_id=(?P<vid>\d+)",
    )
    async def _parse_modal(self, searched: re.Match[str]):
        # www.douyin.com/jingxuan?modal_id=xxx 这类"精选/弹窗"链接：
        # 视频 ID 在 query 的 modal_id 里，路径不是 /video/ 或 /note/，
        # 此前所有 handler 都匹配不上 → 整条消息被静默忽略、无任何响应。
        # 直接取出 ID，走稳定的 m / iesdouyin 解析路径。
        vid = searched.group("vid")
        if not vid:
            raise ParseException("未能从链接中提取抖音视频 ID(modal_id)")
        return await self._parse_by_id_fallback(vid)

    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")

        if ty == "slides":
            return await self.parse_slides(vid)

        last_err: Exception | None = None

        # www.douyin.com 反爬较重、经常拿不到 _ROUTER_DATA（甚至超时），
        # 之前被排在第一个尝试，导致每次解析都先在它身上空耗一轮。
        # 这里优先更稳定的移动端分享域名(m / iesdouyin)，www 降为最后兜底。
        for url in (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
            f"https://www.douyin.com/{ty}/{vid}",
        ):
            try:
                return await self.parse_video(url, vid)
            except Exception as e:
                last_err = e

        logger.warning(f"[Douyin] 直连解析失败，切换 ytdlp 兜底: {last_err}")
        return await self._parse_with_ytdlp(vid)

    async def _parse_by_id_fallback(self, vid: str):
        last_err: Exception | None = None

        for ty in ("video", "note"):
            for url in (
                self._build_m_douyin_url(ty, vid),
                self._build_iesdouyin_url(ty, vid),
            ):
                try:
                    return await self.parse_video(url, vid)
                except Exception as e:
                    last_err = e

        logger.warning(f"[Douyin] _parse_by_id_fallback 失败，切换 ytdlp: {last_err}")
        return await self._parse_with_ytdlp(vid)

    async def parse_video(self, url: str, vid: str):
        if self._is_live_url(url):
            raise SkipParseException()

        resp = await self.http_get(
            url,
            headers=self.ios_headers,
            allow_redirects=True,
            timeout=20,
        )

        if resp.status_code != 200:
            raise ParseException(f"页面请求失败 Status: {resp.status_code}")

        final_resp_url = str(getattr(resp, "url", "") or "")
        if self._is_live_url(final_resp_url):
            raise SkipParseException()

        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=50)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)
        meta = msgspec.convert(aweme, VideoData)

        contents = []
        image_urls = meta.image_urls or extract_static_image_urls_deep(aweme)

        # 图文 / live photo 作品会带 images，同时可能把动图播放地址塞进顶层 video。
        # 当前策略回退为静态图，避免把无声/不可直连的动图视频当普通视频下载。
        if self._has_image_album(aweme) and image_urls:
            contents.extend(
                self._create_image_contents_with_headers(
                    image_urls,
                    self.ios_headers,
                )
            )

        elif meta.video_url and self._is_video_like_url(meta.video_url):
            task = self.downloader.download_video(
                meta.video_url,
                video_name=f"douyin_{meta.id or vid}.mp4",
                ext_headers=self.ios_headers,
            )
            contents.append(
                self.create_video_content(
                    task,
                    None,
                    meta.video.duration if meta.video else 0,
                )
            )

        elif image_urls:
            contents.extend(
                self._create_image_contents_with_headers(
                    image_urls,
                    self.ios_headers,
                )
            )

        author = self.create_author(
            meta.author.nickname,
            meta.avatar_url,
            ext_headers=self.ios_headers,
        )
        aweme_id = meta.id or vid
        comment_task = self._create_comment_task(
            aweme_id,
            title=meta.desc,
            cover=meta.cover_url or (image_urls[0] if image_urls else None),
            author=meta.author.nickname,
            timestamp=meta.create_time,
            referer=f"https://www.douyin.com/{'note' if self._has_image_album(aweme) else 'video'}/{aweme_id}",
        )
        return self.result(
            title=meta.desc,
            author=author,
            contents=contents,
            timestamp=meta.create_time,
            extra={"comment_task": comment_task} if comment_task else {},
        )

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}

        resp = await self.http_get(
            url,
            params=params,
            headers=self.android_headers,
            allow_redirects=True,
            timeout=20,
        )

        if resp.status_code >= 400:
            raise ParseException(f"API Error: {resp.status_code}")

        from .slides import SlidesInfo

        info = msgspec.json.decode(resp.content, type=SlidesInfo)
        if not info.aweme_details:
            raise ParseException("图集数据为空")

        slides = info.aweme_details[0]
        contents = []

        def pick_best_image_url(urls: list[str]) -> str | None:
            valid = [
                u for u in urls
                if isinstance(u, str)
                and u.startswith(("http://", "https://"))
            ]

            if not valid:
                return None

            valid.sort(
                key=lambda u: (
                    (".jpeg" in u.lower()) * 30
                    + (".jpg" in u.lower()) * 30
                    + (".png" in u.lower()) * 25
                    + (".webp" in u.lower()) * 20
                    + ("p96-" in u.lower()) * 10
                    + ("p26-" in u.lower()) * 8
                    + ("p11-" in u.lower()) * 6
                    + ("p9-" in u.lower()) * 5
                    + ("p5-" in u.lower()) * 4
                ),
                reverse=True,
            )

            return valid[0]

        sent_images: set[str] = set()
        first_image_url: str | None = None

        for image in slides.images or []:
            image_url = pick_best_image_url(image.url_list or [])
            if not image_url and image.video and image.video.cover:
                image_url = pick_best_image_url(image.video.cover.url_list or [])

            if image_url and image_url not in sent_images:
                sent_images.add(image_url)
                if not first_image_url:
                    first_image_url = image_url
                contents.extend(
                    self._create_image_contents_with_headers(
                        [image_url],
                        self.android_headers,
                    )
                )

        author = self.create_author(
            slides.name,
            slides.avatar_url,
            ext_headers=self.android_headers,
        )
        comment_task = self._create_comment_task(
            video_id,
            title=slides.desc,
            cover=first_image_url,
            author=slides.name,
            timestamp=slides.create_time,
            referer=f"https://www.douyin.com/note/{video_id}",
        )

        return self.result(
            title=slides.desc,
            author=author,
            contents=contents,
            timestamp=slides.create_time,
            extra={"comment_task": comment_task} if comment_task else {},
        )

    async def _parse_with_ytdlp(self, vid: str):
        url = f"https://www.douyin.com/video/{vid}"

        info = await self.downloader.ytdlp_extract_info(url)
        contents = []

        if info.duration:
            task = self.downloader.download_video(
                url,
                use_ytdlp=True,
                video_name=f"douyin_{vid}.mp4",
            )
            contents.append(
                self.create_video_content(
                    task,
                    None,
                    info.duration,
                )
            )

        title = info.title or "抖音视频"
        author_name = info.uploader or "抖音用户"
        author = self.create_author(author_name)
        comment_task = self._create_comment_task(
            vid,
            title=title,
            cover=None,
            author=author_name,
            timestamp=info.timestamp,
            referer=url,
        )
        return self.result(
            title=title,
            text=info.description or "",
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
            extra={"comment_task": comment_task} if comment_task else {},
        )
