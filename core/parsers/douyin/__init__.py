import hashlib
import re
from html import unescape
from typing import TYPE_CHECKING, ClassVar

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...data import ImageContent
from ...download import Downloader
from ...live_renderer import LiveCardRenderer
from ...utils import image_to_data_uri, normalize_image_url
from ..base import BaseParser, ParseException, Platform, handle
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

    @staticmethod
    def _clean_html_text(value: str | None) -> str | None:
        if not value:
            return None
        text = re.sub(r"<[^>]+>", "", value)
        text = unescape(text).strip()
        return text or None

    @classmethod
    def _clean_live_url(cls, value: str | None) -> str | None:
        text = cls._clean_html_text(value)
        if not text:
            return None
        text = text.strip("'\"")
        if text.startswith("//"):
            text = "https:" + text
        return normalize_image_url(text)

    @staticmethod
    def _extract_input_value(html: str, name: str) -> str | None:
        for matched in re.finditer(r"<input\b[^>]*>", html, flags=re.IGNORECASE | re.DOTALL):
            tag = matched.group(0)
            name_attr = re.search(
                r"\bname=([\"'])(.*?)\1",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not name_attr or name_attr.group(2) != name:
                continue
            value_attr = re.search(
                r"\bvalue=([\"'])(.*?)\1",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if value_attr:
                return unescape(value_attr.group(2)).strip()
        return None

    @classmethod
    def _extract_text(cls, html: str, pattern: str) -> str | None:
        matched = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if not matched:
            return None
        return cls._clean_html_text(matched.group(1))

    @staticmethod
    def _normalize_live_html(html: str) -> str:
        return (
            html.replace("\\u002F", "/")
            .replace("\\/", "/")
            .replace("\\u0026", "&")
            .replace("\\u003D", "=")
            .replace("\\u003F", "?")
            .replace("&amp;", "&")
        )

    @classmethod
    def _extract_json_text(cls, html: str, *keys: str) -> str | None:
        normalized = cls._normalize_live_html(html)
        for key in keys:
            matched = re.search(
                rf'"{re.escape(key)}"\s*:\s*"([^"]+)"',
                normalized,
                flags=re.IGNORECASE,
            )
            if matched:
                return cls._clean_html_text(matched.group(1))
        return None

    @classmethod
    def _extract_live_image_urls(cls, html: str) -> list[str]:
        normalized = cls._normalize_live_html(html)
        urls: list[str] = []
        seen: set[str] = set()

        for matched in re.finditer(r'https?://[^"\'<>\s\\]+', normalized):
            url = cls._clean_live_url(matched.group(0))
            if not url:
                continue

            low = url.lower()
            if "douyinpic.com" not in low and "bytednsdoc.com" not in low:
                continue
            if any(skip in low for skip in ("logo", "launcher", "button_call", "medal", "grade_level")):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)

        return urls

    @staticmethod
    def _pick_live_cover_url(urls: list[str]) -> str | None:
        def score(url: str) -> int:
            low = url.lower()
            value = 0
            if "webcast_cover" in low:
                value += 100
            if "room.pack" in low or "reflow_room_info" in low:
                value += 60
            if "cover" in low:
                value += 40
            if "tplv-resize:0:0.image" in low:
                value += 30
            if "100x100" in low or "/avatar/" in low:
                value -= 60
            return value

        ranked = [(score(url), url) for url in urls]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(reverse=True)
        return ranked[0][1] if ranked else None

    @staticmethod
    def _pick_live_avatar_url(urls: list[str]) -> str | None:
        def score(url: str) -> int:
            low = url.lower()
            value = 0
            if "aweme-avatar" in low or "/avatar/" in low:
                value += 80
            if "100x100" in low:
                value += 40
            if "webcast_cover" in low or "room.pack" in low:
                value -= 100
            return value

        ranked = [(score(url), url) for url in urls]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(reverse=True)
        return ranked[0][1] if ranked else None

    async def _live_image_to_data_uri(self, url: str | None) -> str | None:
        return await image_to_data_uri(
            self.http_get,
            url,
            headers=self.ios_headers,
            referer="https://www.douyin.com/",
            max_bytes=4 * 1024 * 1024,
            timeout=8,
            debug_label="[Douyin-live] image",
        )

    def _extract_live_name_from_source(self) -> str | None:
        text = self.source_text or ""
        for pattern in (
            r"【([^】]{1,30})】正在直播",
            r"([^\s，,。！!【】#]{1,30})正在直播",
            r"([^\s，,。！!【】#]{1,30})的直播",
        ):
            if matched := re.search(pattern, text):
                name = matched.group(1).strip()
                if name and "抖音" not in name:
                    return name
        return None

    async def _parse_live_url(self, url: str):
        html = ""
        final_url = url

        try:
            resp = await self.http_get(
                url,
                headers=self.ios_headers,
                allow_redirects=True,
                timeout=10,
            )
            html = resp.text or ""
            final_url = str(getattr(resp, "url", "") or url)
        except Exception as e:
            logger.debug(f"[Douyin-live] 页面获取失败，使用分享文案兜底: {e}")

        normalized_html = self._normalize_live_html(html)
        image_urls = self._extract_live_image_urls(html)
        source_name = self._extract_live_name_from_source()
        share_title = self._extract_input_value(html, "shareTitle")
        title = share_title or (f"{source_name}的直播" if source_name else "抖音直播")
        streamer_name = (
            self._extract_text(normalized_html, r'<h2[^>]*class="[^"]*anchor-name[^"]*"[^>]*>(.*?)</h2>')
            or self._extract_json_text(html, "nickname", "webcastNick")
            or (share_title[:-3] if share_title and share_title.endswith("的直播") else None)
            or source_name
            or "抖音主播"
        )

        cover = self._clean_live_url(self._extract_input_value(html, "shareImage"))
        if not cover:
            cover = self._clean_live_url(
                self._extract_text(
                    normalized_html,
                    r'<div[^>]*class="[^"]*cover[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"',
                )
            )
        if not cover:
            cover = self._clean_live_url(
                self._extract_text(normalized_html, r"background-image:url\(([^)]+)\)")
            )
        if not cover:
            cover = self._pick_live_cover_url(image_urls)

        avatar = self._clean_live_url(
            self._extract_text(
                normalized_html,
                r'<div[^>]*class="[^"]*avatar[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"',
            )
        )
        if not avatar:
            avatar = self._pick_live_avatar_url(image_urls)

        cover_data_uri = await self._live_image_to_data_uri(cover)
        avatar_data_uri = await self._live_image_to_data_uri(avatar)

        reason = self._extract_text(normalized_html, r'<div[^>]*class="[^"]*reason[^"]*"[^>]*>(.*?)</div>')
        if reason and "结束" in reason:
            status_text = "已结束"
        elif "正在直播" in (self.source_text or "") or "直播中" in html:
            status_text = "直播中"
        else:
            status_text = reason or "直播"

        return await self._render_live_card_result(
            title=title,
            streamer_name=streamer_name,
            cover=cover_data_uri or cover,
            avatar=avatar_data_uri or avatar,
            status_text=status_text,
            cache_key=final_url,
            url=final_url,
        )

    async def _render_live_card_result(
        self,
        *,
        title: str,
        streamer_name: str,
        cover: str | None,
        avatar: str | None,
        status_text: str,
        cache_key: str,
        url: str,
    ):
        digest = hashlib.md5(
            "\n".join(
                [
                    cache_key,
                    title,
                    streamer_name,
                    cover or "",
                    avatar or "",
                    status_text,
                    "douyin_live_card_v2",
                ]
            ).encode("utf-8")
        ).hexdigest()[:12]
        out_path = self.cache_dir / f"douyin_live_{digest}.png"

        if not out_path.exists():
            await LiveCardRenderer().render_live_card(
                out_path=out_path,
                platform_name="Douyin",
                title=title,
                streamer_name=streamer_name,
                cover=cover,
                avatar=avatar,
                status_text=status_text,
                area_text=None,
            )

        return self.result(
            title=title,
            contents=[ImageContent(out_path)],
            url=url,
            extra={"force_direct_media": True},
        )

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

    # 直播链接走通用直播卡，不下载直播流。
    @handle("live.douyin.com", r"(?:https?://)?live\.douyin\.com/[A-Za-z0-9_/?.=&%-]+")
    @handle("douyin.com/live", r"(?:https?://)?(?:www\.)?douyin\.com/live/[A-Za-z0-9_/?.=&%-]+")
    @handle("webcast.douyin.com", r"(?:https?://)?webcast\.douyin\.com/[A-Za-z0-9_/?.=&%-]+")
    @handle("webcast.amemv.com", r"(?:https?://)?webcast\.amemv\.com/[A-Za-z0-9_/?.=&%-]+")
    async def _parse_live_link(self, searched: re.Match[str]):
        raw = searched.group(0)
        url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
        return await self._parse_live_url(url)

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        short_url = f"https://{searched.group(0)}"
        final_url = await self.get_final_url(short_url, headers=self.ios_headers)

        if self._is_live_url(final_url):
            return await self._parse_live_url(final_url)

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
            return await self._parse_live_url(url)

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
            return await self._parse_live_url(final_resp_url)

        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=50)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)
        meta = msgspec.convert(aweme, VideoData)

        contents = []

        # 普通视频 / 普通图集兜底：
        # 优先视频，避免普通视频因为图片字段被误判成图集。
        if meta.video_url and self._is_video_like_url(meta.video_url):
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

        else:
            image_urls = meta.image_urls or extract_static_image_urls_deep(aweme)
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

        author = self.create_author(info.uploader or "抖音用户")
        return self.result(
            title=info.title or "抖音视频",
            text=info.description or "",
            author=author,
            contents=contents,
            timestamp=info.timestamp,
            url=url,
        )
