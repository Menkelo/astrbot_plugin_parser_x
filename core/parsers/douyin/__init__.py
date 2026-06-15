import re
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...download import Downloader
from ..base import BaseParser, ParseException, Platform, SkipParseException, handle
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
    DAILY_UNSUPPORTED_TEXT: ClassVar[str] = "无法解析抖音限时日常内容"

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

        self.cookies = ""
        ck_conf = config.get("cookies", {})
        if isinstance(ck_conf, dict):
            self.cookies = ck_conf.get("douyin_ck", "")

        if self.cookies:
            self._set_cookies(self.cookies)

    def _set_cookies(self, cookies: str):
        cleaned = cookies.replace("\n", "").replace("\r", "").strip()
        if cleaned:
            self.ios_headers["Cookie"] = cleaned
            self.android_headers["Cookie"] = cleaned

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

    @classmethod
    def _unsupported_daily_result(cls) -> "ParseResult":
        return cls.result(
            text=cls.DAILY_UNSUPPORTED_TEXT,
            extra={"plain_text_only": True},
        )

    @staticmethod
    def _looks_like_daily_share_url(text: str | None) -> bool:
        if not text:
            return False

        candidates = re.findall(r"https?://[^\s\"'<>]+", text)
        if not candidates:
            candidates = [text]

        for candidate in candidates:
            try:
                query = parse_qs(urlparse(candidate).query, keep_blank_values=True)
            except Exception:
                continue

            activity_text = " ".join(query.get("activity_info") or []).lower()
            extra_text = " ".join(query.get("share_extra_params") or []).lower()
            compact_extra = re.sub(r"\s+", "", extra_text)

            has_social_activity = any(
                key in activity_text
                for key in (
                    "social_author_id",
                    "social_share_id",
                    "social_share_time",
                    "social_share_user_id",
                )
            )
            has_daily_schema = (
                '"schema_type":"1"' in compact_extra
                or "'schema_type':'1'" in compact_extra
            )
            has_empty_title_type = any(
                value == ""
                for value in query.get("titleType") or []
            )

            if has_social_activity and has_daily_schema and has_empty_title_type:
                return True

        return False

    @staticmethod
    def _is_fresh_cookies_error(error: Exception) -> bool:
        text = str(error).lower()
        return "fresh cookies" in text and "needed" in text

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
                if self._looks_like_daily_share_url(final_url) and self._is_fresh_cookies_error(e):
                    return self._unsupported_daily_result()
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

        # 移动端分享域名(m / iesdouyin)更稳定，优先；www.douyin.com 反爬较重，
        # 降为最后兜底。串行尝试：m 通常一次即成功，避免并发请求挤占共享 session。
        # 提速点仅保留单域名超时 20s → 8s（见 _fetch_router_resp 默认 timeout）。
        for url in (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
            f"https://www.douyin.com/{ty}/{vid}",
        ):
            try:
                return await self.parse_video(url, vid)
            except SkipParseException:
                raise
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
                except SkipParseException:
                    raise
                except Exception as e:
                    last_err = e

        logger.warning(f"[Douyin] _parse_by_id_fallback 失败，切换 ytdlp: {last_err}")
        return await self._parse_with_ytdlp(vid)

    async def _fetch_router_resp(self, url: str, timeout: int = 8):
        """
        仅负责抓取抖音分享页并做直播/状态码校验，返回响应对象。
        与解析处理拆开，便于多域名竞速。
        """
        if self._is_live_url(url):
            raise SkipParseException()

        resp = await self.http_get(
            url,
            headers=self.ios_headers,
            allow_redirects=True,
            timeout=timeout,
        )

        if resp.status_code != 200:
            raise ParseException(f"页面请求失败 Status: {resp.status_code}")

        final_resp_url = str(getattr(resp, "url", "") or "")
        if self._is_live_url(final_resp_url):
            raise SkipParseException()

        return resp

    async def parse_video(self, url: str, vid: str):
        resp = await self._fetch_router_resp(url)
        return self._process_router_resp(resp, vid)

    def _process_router_resp(self, resp, vid: str):
        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=50)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)
        meta = msgspec.convert(aweme, VideoData)

        contents = []
        image_urls = meta.image_urls or extract_static_image_urls_deep(aweme)

        # 图文作品优先发送静态图片，避免把无声或不可直连的图文视频误当普通视频下载。
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
        return self.result(
            title=meta.desc,
            author=author,
            contents=contents,
            timestamp=meta.create_time,
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

        for image in slides.images or []:
            image_url = pick_best_image_url(image.url_list or [])
            if not image_url and image.video and image.video.cover:
                image_url = pick_best_image_url(image.video.cover.url_list or [])

            if image_url and image_url not in sent_images:
                sent_images.add(image_url)
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

        return self.result(
            title=slides.desc,
            author=author,
            contents=contents,
            timestamp=slides.create_time,
        )

    async def _parse_with_ytdlp(self, vid: str):
        url = f"https://www.douyin.com/video/{vid}"

        try:
            info = await self.downloader.ytdlp_extract_info(url)
        except Exception as e:
            if self._looks_like_daily_share_url(self.source_text) and self._is_fresh_cookies_error(e):
                return self._unsupported_daily_result()
            raise

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
        return self.result(
            title=title,
            text=info.description or "",
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
        )
