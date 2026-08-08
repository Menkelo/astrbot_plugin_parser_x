import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...download import Downloader
from ...utils import cookies_str_to_netscape
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
        self._cookiefile: Path | None = None
        ck_conf = config.get("cookies", {})
        if isinstance(ck_conf, dict):
            self.cookies = ck_conf.get("douyin_ck", "")

        if self.cookies:
            self._set_cookies(self.cookies)
            self._cookiefile = self._write_cookiefile(self.cookies)

    def _set_cookies(self, cookies: str):
        cleaned = cookies.replace("\n", "").replace("\r", "").strip()
        if cleaned:
            self.ios_headers["Cookie"] = cleaned
            self.android_headers["Cookie"] = cleaned

    def _write_cookiefile(self, cookies: str) -> Path | None:
        """
        将用户配置的 douyin_ck 写成 yt-dlp 可读的 Netscape cookiefile，
        供 _parse_with_ytdlp 兜底时传给 ytdlp_extract_info / download_video。
        """
        if not cookies:
            return None
        try:
            path = self.cache_dir / "douyin_cookies.txt"
            path.write_text(
                cookies_str_to_netscape(cookies),
                encoding="utf-8",
            )
            return path
        except Exception as e:
            logger.warning(f"[Douyin] cookie 文件写入失败: {e}")
            return None

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

    async def _resolve_final_url_by_head(self, url: str) -> str | None:
        """
        抖音短链只需要最终落点。优先用 HEAD 跟随跳转，避免像 get_final_url
        那样把整页 HTML 正文全部下载下来再丢弃（短链解析的主要耗时点）。
        HEAD 被拒或拿不到可用 URL 时返回 None，调用方回退到 GET 方案。
        """
        try:
            resp = await self.client.head(
                url,
                headers=self.ios_headers,
                allow_redirects=True,
                timeout=5,
                verify=False,
            )
        except Exception as e:
            logger.debug(f"[Douyin] HEAD 短链解析失败: {url} | {e}")
            return None

        if getattr(resp, "status_code", 0) >= 400:
            return None

        final_url = str(getattr(resp, "url", "") or "").strip()
        if not final_url or final_url == url:
            return None

        return final_url

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        short_url = f"https://{searched.group(0)}"

        # HEAD 优先（不下载正文），失败再退回完整 GET 跟随重定向。
        _t0 = time.monotonic()
        final_url = await self._resolve_final_url_by_head(short_url)
        if not final_url:
            final_url = await self.get_final_url(
                short_url,
                headers=self.ios_headers,
                timeout=8,
                retries=1,
            )
            logger.debug(
                f"[Douyin][计时] 短链解析(HEAD失败→GET兜底) "
                f"{time.monotonic() - _t0:.2f}s → {final_url}"
            )
        else:
            logger.debug(
                f"[Douyin][计时] 短链解析(HEAD) "
                f"{time.monotonic() - _t0:.2f}s → {final_url}"
            )

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

        # 移动端分享域名(m / iesdouyin)更稳定，www.douyin.com 反爬较重。
        # 以前是串行逐个尝试，m 慢或失败就要等满单域名超时(8s)才试下一个，
        # 最坏可累计 ~24s。现改为三域名并发竞速：谁先成功用谁、取消其余，
        # 总耗时≈最快可用域名的单次耗时。
        try:
            return await self._race_parse_video(
                [
                    self._build_m_douyin_url(ty, vid),
                    self._build_iesdouyin_url(ty, vid),
                    f"https://www.douyin.com/{ty}/{vid}",
                ],
                vid,
            )
        except SkipParseException:
            raise
        except Exception as last_err:
            logger.warning(f"[Douyin] 直连解析失败，切换 ytdlp 兜底: {last_err}")
            return await self._parse_with_ytdlp(vid)

    async def _parse_by_id_fallback(self, vid: str):
        # video / note 两种类型 × m / iesdouyin 两个域名，共 4 个候选。
        # 旧实现 4 个串行 await，最坏累计 ~32s；改为全部并发竞速。
        candidates = [
            self._build_m_douyin_url(ty, vid)
            for ty in ("video", "note")
        ] + [
            self._build_iesdouyin_url(ty, vid)
            for ty in ("video", "note")
        ]

        try:
            return await self._race_parse_video(candidates, vid)
        except SkipParseException:
            raise
        except Exception as last_err:
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
            retries=1,
        )

        if resp.status_code != 200:
            raise ParseException(f"页面请求失败 Status: {resp.status_code}")

        final_resp_url = str(getattr(resp, "url", "") or "")
        if self._is_live_url(final_resp_url):
            raise SkipParseException()

        return resp

    async def _race_parse_video(self, urls: list[str], vid: str):
        """
        多域名并发竞速解析：同时向所有候选分享域名发起抓取，
        谁先成功就用谁，立即取消其余在途请求。

        这是抖音解析提速的关键点：旧实现是串行 for 循环逐个 await，
        m.douyin 一旦慢/失败就要等满单域名超时(8s)才试下一个，最坏
        3 个域名串行可累计 ~24s。改为并发后总耗时≈最快可用域名的单次耗时。

        语义保持与原串行版本一致：
        - 任一域名抛 SkipParseException(直播等) → 立即上抛，不再等其它；
        - 全部失败 → 抛出最后一个非 Skip 异常，交由上层走 ytdlp 兜底。
        """
        urls = [u for u in urls if u]
        if not urls:
            raise ParseException("没有可用的抖音解析域名")

        _start = time.monotonic()

        tasks = [
            asyncio.create_task(self.parse_video(url, vid), name=f"douyin_parse:{url}")
            for url in urls
        ]
        pending = set(tasks)
        last_err: Exception | None = None
        result = None

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        result = task.result()
                    except SkipParseException:
                        # 直播等场景：语义上应直接跳过，不再尝试其它域名。
                        raise
                    except Exception as e:
                        last_err = e
                        continue

                    if result is not None:
                        logger.debug(
                            f"[Douyin][计时] 并发解析成功 {task.get_name()} "
                            f"/ {time.monotonic() - _start:.2f}s"
                        )
                        return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 回收已取消任务，避免 "Task was destroyed but it is pending" 噪声。
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.debug(
            f"[Douyin][计时] 并发解析全部失败 / {time.monotonic() - _start:.2f}s"
        )
        if last_err:
            raise last_err
        raise ParseException("抖音多域名解析均失败")

    async def parse_video(self, url: str, vid: str):
        resp = await self._fetch_router_resp(url)
        try:
            return self._process_router_resp(resp, vid)
        except ParseException as e:
            if "未找到 aweme 数据" in str(e):
                logger.debug(f"[Douyin] SSR 无数据，回退 detail API: {vid}")
                return await self._parse_via_detail_api(vid)
            raise

    async def _parse_via_detail_api(self, vid: str):
        """
        抖音分享页 SSR 改版后不再内嵌 aweme 数据，回退到 detail API。
        需要 PC UA + Referer + Cookie。

        图文作品(notes)在默认参数下会被 filter_reason="images_base" 过滤，
        加 aid=6383 可绕过该过滤，返回完整图文数据。
        """
        if not self.cookies:
            raise ParseException("detail API 需要 cookie，但未配置 douyin_ck")

        base_headers = {
            "User-Agent": self.headers.get("User-Agent", ""),
            "Referer": "https://www.douyin.com/",
            "Cookie": self.cookies,
        }

        # 先用默认参数；图文被过滤时带 aid=6383 重试
        for params in (
            {"aweme_id": vid},
            {"aweme_id": vid, "aid": "6383"},
        ):
            resp = await self.http_get(
                "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                params=params,
                headers=base_headers,
                allow_redirects=True,
                timeout=10,
                retries=1,
            )

            if resp.status_code != 200:
                continue

            try:
                data = msgspec.json.decode(resp.text)
            except Exception as e:
                raise ParseException(f"detail API JSON 解析失败: {e}")

            aweme = data.get("aweme_detail") if isinstance(data, dict) else None
            if aweme and isinstance(aweme, dict):
                return self._build_result_from_aweme(aweme, vid)

            filter_reason = ""
            fd = data.get("filter_detail") if isinstance(data, dict) else None
            if isinstance(fd, dict):
                filter_reason = fd.get("filter_reason") or ""

            if "images" not in filter_reason:
                # 不是图文过滤导致的空，没必要再试 aid=6383
                raise ParseException("detail API 未返回 aweme_detail")

        raise ParseException("detail API 未返回 aweme_detail (图文过滤已绕过仍为空)")

    def _process_router_resp(self, resp, vid: str):
        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=50)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)
        return self._build_result_from_aweme(aweme, vid)

    def _build_result_from_aweme(self, aweme: dict, vid: str):
        """从单个 aweme dict 构建 ParseResult，SSR 与 detail API 共用。"""
        from .video import VideoData

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
            timeout=10,
            retries=1,
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
