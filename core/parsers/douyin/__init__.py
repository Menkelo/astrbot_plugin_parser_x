import re
from typing import TYPE_CHECKING, ClassVar

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...download import Downloader
from ..base import BaseParser, ParseException, Platform, handle
from .composer import DouyinMediaComposer
from .extractor import (
    extract_bgm_url,
    extract_id_from_query,
    extract_mixed_image_dynamic_items,
    extract_router_data_json_str,
    pick_primary_aweme,
)

if TYPE_CHECKING:
    from ...data import ParseResult


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.composer = DouyinMediaComposer(downloader, config)

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
    def _extract_url_strings(value) -> list[str]:
        out: list[str] = []

        def walk(v):
            if isinstance(v, str):
                if v.startswith(("http://", "https://")):
                    out.append(v)
                return

            if isinstance(v, list):
                for item in v:
                    walk(item)
                return

            if isinstance(v, dict):
                for vv in v.values():
                    walk(vv)

        walk(value)
        return out

    @staticmethod
    def _is_video_like_url(url: str) -> bool:
        low = (url or "").lower()
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
    def _is_image_like_url(url: str) -> bool:
        low = (url or "").lower()

        if not isinstance(url, str):
            return False

        if not url.startswith(("http://", "https://")):
            return False

        if DouyinParser._is_video_like_url(url):
            return False

        return any(
            x in low
            for x in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".avif",
                ".heic",
                "douyinpic",
                "byteimg",
                "tos-cn",
                "p3-",
                "p6-",
                "p9-",
                "p11-",
                "p26-",
                "image",
                "img",
            )
        )

    @staticmethod
    def _is_bad_static_url(url: str) -> bool:
        low = (url or "").lower()
        return any(
            x in low
            for x in (
                "avatar",
                "music",
                "webcast",
                "user/profile",
                "profile/avatar",
            )
        )

    @staticmethod
    def _image_quality_score(url: str) -> int:
        low = (url or "").lower()
        score = 0

        if "origin" in low:
            score += 50
        if "large" in low:
            score += 40
        if "display" in low:
            score += 30
        if "douyinpic" in low:
            score += 20
        if "byteimg" in low:
            score += 15
        if "tos-cn" in low:
            score += 10

        if ".jpg" in low or ".jpeg" in low:
            score += 30
        if ".png" in low:
            score += 25
        if ".webp" in low:
            score += 10

        if "tplv-dy-lqen-new" in low:
            score += 10

        if "download_url_list" in low:
            score -= 50
        if "water" in low or "-water" in low:
            score -= 50
        if "thumb" in low or "thumbnail" in low:
            score -= 10

        return score

    def _repair_douyin_mixed_items(
        self,
        aweme: dict,
        mixed_items: list[tuple[str, str, str]] | None,
    ) -> list[tuple[str, str, str]]:
        """
        保守修复抖音静图 + 动图混合作品。

        重点：
        - 静图只从 images[i] 顶层 URL 字段取；
        - 动图只从 images[i]["video"] 里取；
        - 不从 cover / thumbnail / origin_cover 取静图，避免普通视频误判；
        - 不再尝试伪动态图 / BGM / fake video。
        """
        repaired: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        def add(t: str, k: str, u: str):
            if not isinstance(u, str):
                return

            if not u.startswith(("http://", "https://")):
                return

            if t == "video":
                u = u.replace("playwm", "play")

            mark = f"{t}:{u}"
            if mark in seen:
                return

            seen.add(mark)
            repaired.append((t, k, u))

        images = aweme.get("images") or []
        if not isinstance(images, list) or not images:
            return []

        for idx, img in enumerate(images):
            if not isinstance(img, dict):
                continue

            static_candidates: list[str] = []

            for key in (
                "url_list",
                "urlList",
                "download_url_list",
                "downloadUrlList",
                "origin_url_list",
                "originUrlList",
                "large_url_list",
                "largeUrlList",
                "display_url_list",
                "displayUrlList",
            ):
                if key in img:
                    static_candidates.extend(self._extract_url_strings(img.get(key)))

            clean_static: list[str] = []

            for u in static_candidates:
                if not isinstance(u, str):
                    continue

                if self._is_bad_static_url(u):
                    continue

                if not self._is_image_like_url(u):
                    continue

                low = u.lower()
                if "download_url_list" in low or "-water" in low or "water:" in low:
                    continue

                clean_static.append(u)

            clean_static = self._dedupe_urls(clean_static)

            if clean_static:
                clean_static.sort(key=self._image_quality_score, reverse=True)
                add("image", f"image:{idx}", clean_static[0])

            video = img.get("video")
            if not isinstance(video, dict):
                continue

            video_candidates: list[str] = []

            for addr_key in (
                "play_addr",
                "playAddr",
                "download_addr",
                "downloadAddr",
                "play_url",
                "playUrl",
                "bit_rate",
                "bitRate",
            ):
                if addr_key in video:
                    video_candidates.extend(self._extract_url_strings(video.get(addr_key)))

            clean_video: list[str] = []

            for u in video_candidates:
                if not isinstance(u, str):
                    continue

                if not u.startswith(("http://", "https://")):
                    continue

                low = u.lower()

                if any(
                    x in low
                    for x in (
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                        ".heic",
                        ".avif",
                    )
                ):
                    continue

                if ".mp3" in low or "ies-music" in low:
                    continue

                clean_video.append(u.replace("playwm", "play"))

            clean_video = self._dedupe_urls(clean_video)

            if clean_video:
                clean_video.sort(
                    key=lambda u: (
                        ("365yg.com" in u.lower()) * 30
                        + ("v26-" in u.lower()) * 10
                        + ("v5-" in u.lower()) * 8
                        + ("api-play.amemv.com" in u.lower()) * 5
                        - ("api.amemv.com" in u.lower()) * 5
                    ),
                    reverse=True,
                )

                add("video", f"video:{idx}", clean_video[0])

        return repaired

    # 直播直链硬拦截
    @handle("live.douyin.com", r"(?:https?://)?live\.douyin\.com/[A-Za-z0-9_/?.=&%-]+")
    @handle("webcast.douyin.com", r"(?:https?://)?webcast\.douyin\.com/[A-Za-z0-9_/?.=&%-]+")
    async def _parse_live_block(self, searched: re.Match[str]):
        raise ParseException("暂不支持抖音直播解析")

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        short_url = f"https://{searched.group(0)}"
        final_url = await self.get_final_url(short_url, headers=self.ios_headers)

        if self._is_live_url(final_url):
            raise ParseException("暂不支持抖音直播解析")

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

    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")

        if ty == "slides":
            return await self.parse_slides(vid)

        last_err: Exception | None = None

        for url in (
            f"https://www.douyin.com/{ty}/{vid}",
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        ):
            try:
                if self._is_live_url(url):
                    raise ParseException("暂不支持抖音直播解析")
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
                    if self._is_live_url(url):
                        raise ParseException("暂不支持抖音直播解析")
                    return await self.parse_video(url, vid)
                except Exception as e:
                    last_err = e

        logger.warning(f"[Douyin] _parse_by_id_fallback 失败，切换 ytdlp: {last_err}")
        return await self._parse_with_ytdlp(vid)

    async def parse_video(self, url: str, vid: str):
        if self._is_live_url(url):
            raise ParseException("暂不支持抖音直播解析")

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
            raise ParseException("暂不支持抖音直播解析")

        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=50)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)
        meta = msgspec.convert(aweme, VideoData)

        try:
            raw_mixed_items = extract_mixed_image_dynamic_items(aweme)
        except Exception as e:
            logger.warning(f"[Douyin] extract_mixed_image_dynamic_items failed: {e}")
            raw_mixed_items = []

        mixed_items = self._repair_douyin_mixed_items(aweme, raw_mixed_items)

        has_mixed_image = any(t == "image" for t, _, _ in mixed_items)
        has_mixed_video = any(t == "video" for t, _, _ in mixed_items)

        if mixed_items and (has_mixed_image or has_mixed_video):
            bgm_url = extract_bgm_url(aweme)
            contents = []
            dyn_index = 0

            sent_images: set[str] = set()
            sent_videos: set[str] = set()

            for item_type, key, item_url in mixed_items:
                if item_type == "image":
                    if item_url in sent_images:
                        continue

                    sent_images.add(item_url)
                    contents.extend(
                        self._create_image_contents_with_headers(
                            [item_url],
                            self.ios_headers,
                        )
                    )

                elif item_type == "video":
                    if key in sent_videos or item_url in sent_videos:
                        continue

                    sent_videos.add(key)
                    sent_videos.add(item_url)
                    dyn_index += 1

                    contents.extend(
                        self.composer.build_dynamic_contents_with_bgm(
                            entries=[(key, item_url)],
                            vid=f"{vid}_{dyn_index}",
                            bgm_url=bgm_url,
                            ext_headers=self.ios_headers,
                        )
                    )

            if contents:
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

        contents = []

        # 普通视频 / 普通图集兜底：
        # 优先视频，避免普通视频因为图片字段被误判成图集。
        if meta.video_url:
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

        elif meta.images and meta.image_urls:
            contents.extend(
                self._create_image_contents_with_headers(
                    meta.image_urls,
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

        def pick_best_video_url(urls: list[str]) -> str | None:
            valid = [
                u.replace("playwm", "play")
                for u in urls
                if isinstance(u, str)
                and u.startswith(("http://", "https://"))
            ]

            if not valid:
                return None

            valid.sort(
                key=lambda u: (
                    ("365yg.com" in u.lower()) * 30
                    + ("v26-" in u.lower()) * 10
                    + ("v5-" in u.lower()) * 8
                    + ("api-play.amemv.com" in u.lower()) * 5
                    - ("api.amemv.com" in u.lower()) * 5
                ),
                reverse=True,
            )

            return valid[0]

        sent_images: set[str] = set()
        sent_videos: set[str] = set()
        dyn_index = 0

        for idx, image in enumerate(slides.images or []):
            has_dynamic_video = (
                image.video
                and image.video.play_addr
                and image.video.play_addr.url_list
            )

            # 带 video 的节点不再发送 image.url_list，避免纯黑占位图。
            if not has_dynamic_video:
                image_url = pick_best_image_url(image.url_list or [])
                if image_url and image_url not in sent_images:
                    sent_images.add(image_url)
                    contents.extend(
                        self._create_image_contents_with_headers(
                            [image_url],
                            self.android_headers,
                        )
                    )

            if has_dynamic_video:
                video_url = pick_best_video_url(image.video.play_addr.url_list or [])

                if video_url and video_url not in sent_videos:
                    sent_videos.add(video_url)
                    dyn_index += 1

                    contents.extend(
                        self.composer.build_dynamic_contents_with_bgm(
                            entries=[(f"slides:{idx}:{video_url}", video_url)],
                            vid=f"{video_id}_slides_{dyn_index}",
                            bgm_url=None,
                            ext_headers=self.android_headers,
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

        if self._is_live_url(url):
            raise ParseException("暂不支持抖音直播解析")

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
