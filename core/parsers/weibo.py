import json
import re
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from re import Match
from typing import ClassVar

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..comment_canvas import SocialCommentCanvas
from ..comment_settings import CommentSettings
from ..data import (
    DeliveryBatch,
    DeliveryPlan,
    DynamicContent,
    ImageContent,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..html_renderer import HtmlRenderService
from ..platform_emotes import select_text_emotes
from ..utils import normalize_image_url
from .base import BaseParser, ParseException, handle
from .gallery import first_media_url, live_photo, media_url, ordered_media_plan
from .weibo_comment import WeiboCommentFeed


class WeiboParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="weibo", display_name="微博")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Referer": "https://m.weibo.cn/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.162 Mobile Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "MWeibo-Pwa": "1",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.cache_dir = Path(config["cache_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.render_service = HtmlRenderService.from_config(config)
        cookies = config.get("cookies", {})
        self.cookie = (
            str(cookies.get("weibo_cookie", "")) if isinstance(cookies, dict) else ""
        )
        comment_settings = CommentSettings.from_config(config, "weibo")
        self.enable_comment_card = comment_settings.enabled
        self.comment_limit = comment_settings.display_count
        self.comment_timeout = comment_settings.timeout
        self.comment_canvas = SocialCommentCanvas(self.render_service)
        self.comment_feed = WeiboCommentFeed(
            self,
            self.comment_canvas,
            limit=self.comment_limit,
        )

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service
        self.comment_canvas.render_service = render_service

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _comment_extra(
        self,
        mid: str,
        *,
        title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> dict:
        if not self.enable_comment_card or not mid:
            return {}

        async def build_comment_images():
            return await self.comment_feed.build_images(
                mid,
                work_title=title,
                cover=cover,
                owner_id=owner_id,
            )

        return {
            "comment_image_task_factory": build_comment_images,
            "comment_timeout": self.comment_timeout,
        }

    @staticmethod
    def _mid_to_bid(mid: str) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

        def base62_encode(number: int) -> str:
            if number == 0:
                return "0"
            output = ""
            while number > 0:
                number, remainder = divmod(number, 62)
                output = alphabet[remainder] + output
            return output

        reversed_mid = str(mid)[::-1]
        groups = []
        for index in range(0, len(reversed_mid), 7):
            encoded = base62_encode(int(reversed_mid[index : index + 7][::-1]))
            if index + 7 < len(reversed_mid):
                encoded = encoded.zfill(4)
            groups.append(encoded)
        return "".join(reversed(groups))

    @classmethod
    def _delivery_summary(cls, data: dict, text: str) -> str:
        lines = ["识别：微博"]
        if text:
            lines.append(text)
        if status_title := cls._html_to_plain_text(data.get("status_title")):
            title_prefix = re.sub(r"[.。…]+$", "", status_title).strip()
            if not title_prefix or not text.startswith(title_prefix):
                lines.append(status_title)
        source = cls._html_to_plain_text(data.get("source"))
        region = re.sub(r"^发布于", "", str(data.get("region_name") or "")).strip()
        if source or region:
            lines.append("\t".join(item for item in (source, region) if item))
        return "\n".join(lines)

    @classmethod
    def _extract_body_text(cls, data: dict) -> str:
        long_text = data.get("longText")
        candidates = []
        if isinstance(long_text, dict):
            candidates.extend(
                [
                    long_text.get("longTextContent"),
                    long_text.get("content"),
                    long_text.get("text"),
                ]
            )
        elif isinstance(long_text, str):
            candidates.append(long_text)
        candidates.extend(
            [
                data.get("longTextContent"),
                data.get("text"),
                data.get("text_raw"),
            ]
        )
        texts = [
            text
            for candidate in candidates
            if (text := cls._html_to_plain_text(candidate))
        ]
        return max(texts, key=len, default="")

    @staticmethod
    def _extract_body_emotes(data: object) -> dict[str, str]:
        output: dict[str, str] = {}

        def scan_html(value: str) -> None:
            for tag in re.findall(r"<img\b[^>]*>", value, flags=re.I | re.S):
                attrs = {
                    key.lower(): unescape(attr_value)
                    for key, _quote, attr_value in re.findall(
                        r"\b(src|alt|title)\s*=\s*([\"'])(.*?)\2",
                        tag,
                        flags=re.I | re.S,
                    )
                }
                token = str(attrs.get("alt") or attrs.get("title") or "").strip()
                url = normalize_image_url(attrs.get("src")) or ""
                if token and url:
                    output[token] = url

        def walk(value: object) -> None:
            if isinstance(value, str):
                if "<img" in value.lower():
                    scan_html(value)
                return
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
                return
            if isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        return output

    @staticmethod
    def _looks_like_status_data(value: object) -> bool:
        return isinstance(value, dict) and any(
            key in value
            for key in (
                "id",
                "mid",
                "text",
                "text_raw",
                "longText",
                "status_title",
                "pics",
                "user",
            )
        )

    @classmethod
    def _find_status_data(cls, value: object) -> dict | None:
        if not isinstance(value, dict):
            return None

        status = value.get("status")
        if cls._looks_like_status_data(status):
            return status
        if cls._looks_like_status_data(value):
            return value

        for key in ("data", "mblog"):
            nested = value.get(key)
            if found := cls._find_status_data(nested):
                return found
        return None

    @classmethod
    def _extract_detail_status(cls, html: str) -> dict | None:
        if not html:
            return None

        decoder = json.JSONDecoder()
        sources = (html, unescape(html))
        for source in sources:
            for marker in re.finditer(r'"status"\s*:\s*', source):
                try:
                    value, _ = decoder.raw_decode(source[marker.end() :])
                except (json.JSONDecodeError, TypeError):
                    continue
                if found := cls._find_status_data({"status": value}):
                    return found

            for marker in re.finditer(r"\$render_data\s*=\s*", source):
                try:
                    value, _ = decoder.raw_decode(source[marker.end() :])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, list):
                    for item in value:
                        if found := cls._find_status_data(item):
                            return found
                elif found := cls._find_status_data(value):
                    return found
        return None

    async def _fetch_detail_status(self, bid: str) -> dict | None:
        try:
            response = await self.client.get(
                f"https://m.weibo.cn/detail/{bid}",
                headers=self._request_headers(),
                timeout=8,
            )
            if response.status_code != 200:
                return None
            return self._extract_detail_status(response.text)
        except Exception as exc:
            logger.debug(f"[Weibo] 获取详情页正文失败: {exc}")
            return None

    @staticmethod
    def _merge_status_data(primary: dict, fallback: dict) -> dict:
        merged = dict(primary)
        for key, value in fallback.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return merged

    async def _fetch_web_status(self, bid: str) -> dict | None:
        try:
            headers = self._request_headers()
            headers["Referer"] = "https://weibo.com/"
            response = await self.client.get(
                "https://weibo.com/ajax/statuses/show",
                params={"id": bid},
                headers=headers,
                timeout=8,
            )
            if response.status_code == 200:
                data = response.json()
                if self._looks_like_status_data(data):
                    return data
        except Exception as exc:
            logger.debug(
                f"[Weibo] 网页作品数据不可用，尝试移动端: {type(exc).__name__}"
            )
        return None

    async def _fetch_status_data(self, bid: str) -> dict:
        # Logged-in web responses preserve live-photo and mixed-media metadata
        # that the mobile view can flatten into still images.
        if self.cookie and (web_data := await self._fetch_web_status(bid)):
            return web_data
        api_error = ""
        try:
            response = await self.client.get(
                f"https://m.weibo.cn/statuses/show?id={bid}",
                headers=self._request_headers(),
                timeout=8,
            )
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("ok") == 1:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and data:
                        if self._extract_body_text(data):
                            return data
                        if detail := await self._fetch_detail_status(bid):
                            return self._merge_status_data(data, detail)
                        return data
                api_error = str(
                    payload.get("msg") if isinstance(payload, dict) else payload
                )
            else:
                api_error = f"HTTP {response.status_code}"
        except Exception as exc:
            api_error = str(exc)

        if detail := await self._fetch_detail_status(bid):
            return detail
        raise ParseException(f"微博 API 请求失败: {api_error or '未获取到微博数据'}")

    async def _resolve_single_body_text(self, data: dict, bid: str) -> str:
        inline_long_text = self._extract_body_text(
            {
                "longText": data.get("longText"),
                "longTextContent": data.get("longTextContent"),
            }
        )
        fallback = self._extract_body_text(data)
        if inline_long_text or not data.get("isLongText"):
            return inline_long_text or fallback

        status_id = str(data.get("id") or data.get("mid") or bid).strip()
        if not status_id:
            return fallback

        try:
            response = await self.client.get(
                f"https://m.weibo.cn/statuses/extend?id={status_id}",
                headers=self._request_headers(),
                timeout=8,
            )
            if response.status_code != 200:
                return fallback
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") != 1:
                return fallback
            extended_data = payload.get("data") or {}
            if isinstance(extended_data, dict):
                extended_text = self._extract_body_text(extended_data)
                if extended_text:
                    data["_parser_x_extended_body"] = extended_data
                    return extended_text
        except Exception as exc:
            logger.debug(f"[Weibo] 获取长微博正文失败，使用摘要正文: {exc}")
        return fallback

    async def _resolve_body_text(self, data: dict, bid: str) -> str:
        text = await self._resolve_single_body_text(data, bid)
        retweeted = data.get("retweeted_status")
        if not isinstance(retweeted, dict):
            return text or self._html_to_plain_text(data.get("status_title"))

        retweeted_bid = str(
            retweeted.get("id") or retweeted.get("mid") or retweeted.get("bid") or ""
        ).strip()
        retweeted_text = await self._resolve_single_body_text(
            retweeted,
            retweeted_bid,
        )
        if not retweeted_text:
            return text or self._html_to_plain_text(data.get("status_title"))

        retweeted_user = retweeted.get("user") or {}
        retweeted_name = (
            str(retweeted_user.get("screen_name") or "").strip()
            if isinstance(retweeted_user, dict)
            else ""
        )
        original = (
            f"转发自 @{retweeted_name}：{retweeted_text}"
            if retweeted_name
            else f"转发微博：{retweeted_text}"
        )
        if text and text not in {"转发微博", "转发"}:
            return f"{text}\n\n{original}"
        return original

    @staticmethod
    def _delivery_plan(
        summary: str,
        images: list[ImageContent],
        videos: list[VideoContent],
    ) -> DeliveryPlan:
        if images and videos:
            parts = [*([summary] if summary else []), *images, *videos]
            return DeliveryPlan([DeliveryBatch(parts, mode="forward")])

        batches = []
        if summary:
            batches.append(DeliveryBatch([summary]))
        if images:
            batches.append(
                DeliveryBatch(
                    list(images),
                    mode="direct" if len(images) <= 9 else "forward",
                    reply_original=len(images) == 1,
                )
            )
        batches.extend(DeliveryBatch([video]) for video in videos)
        return DeliveryPlan(batches)

    @handle("weibo.com/tv", r"weibo\.com/tv/show/(?P<fid>\d+:\d+)(?:[?][^\s<>]*)?")
    @handle(
        "video.weibo.com",
        r"(?:h5\.)?video\.weibo\.com/(?:show\?(?:[^\s<>]*?&)?fid=|show/)(?P<fid>\d+:\d+)(?:[&?][^\s<>]*)?",
    )
    async def _parse_video_fid(self, searched: Match[str]):
        fid = searched.group("fid")
        headers = self._request_headers()
        headers["Referer"] = f"https://h5.video.weibo.com/show/{fid}"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = await self.client.post(
            f"https://h5.video.weibo.com/api/component?page=/show/{fid}",
            data={"data": json.dumps({"Component_Play_Playinfo": {"oid": fid}})},
            headers=headers,
            timeout=10,
        )
        if response.status_code >= 400:
            raise ParseException(f"微博视频接口请求失败: HTTP {response.status_code}")
        try:
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            info = (
                data.get("Component_Play_Playinfo") if isinstance(data, dict) else None
            )
        except (TypeError, ValueError):
            info = None
        if not isinstance(info, dict):
            raise ParseException("微博视频不存在、不可见或接口未返回播放信息")
        url = self._pick_video_url({"urls": info.get("urls"), "media_info": info})
        if not url:
            raise ParseException("微博视频未返回可用播放地址")
        reward = info.get("reward") or {}
        user = reward.get("user") or {} if isinstance(reward, dict) else {}
        content = self.create_video_content(
            url, duration=self._duration(info.get("duration_time"))
        )
        return self.result(
            title=self._html_to_plain_text(info.get("title")),
            text=self._html_to_plain_text(info.get("text")),
            author=self.create_author(
                str(user.get("name") or "微博用户"), user.get("profile_image_url")
            ),
            timestamp=info.get("real_date"),
            contents=[content],
            url=f"https://video.weibo.com/show?fid={fid}",
        )

    @handle("mapp.api.weibo.cn", r"https?://mapp\.api\.weibo\.cn/fx/[A-Za-z0-9]+\.html")
    async def _parse_app_share(self, searched: Match[str]):
        return await self.parse_with_redirect(
            searched.group(0), headers=self._request_headers()
        )

    @staticmethod
    def _duration(value) -> float:
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _media_specs(cls, data: dict):
        # (kind, URL, duration, optional still image), in source order.
        specs = []
        seen = set()

        def add(kind, url, duration=0, still=None):
            url = media_url(url)
            if url and (kind, url) not in seen:
                seen.add((kind, url))
                specs.append((kind, url, cls._duration(duration), still))

        def picture(pic):
            if not isinstance(pic, dict):
                return
            still = media_url(cls._pick_static_pic_url(pic))
            kind = str(pic.get("type") or "").lower()
            if kind == "livephoto":
                if url := first_media_url(pic.get("video")):
                    add("live", url, still=still)
                    return
            if cls._is_video_pic(pic):
                url = media_url(
                    pic.get("videoSrc") or pic.get("video_src")
                ) or cls._pick_video_url(pic)
                if url:
                    add("video", url)
                    return
            if still:
                add("image", still)

        mixed = data.get("mix_media_info")
        if isinstance(mixed, dict) and isinstance(mixed.get("items"), list):
            for entry in mixed["items"]:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("data"), dict
                ):
                    continue
                item = entry["data"]
                if entry.get("type") == "video":
                    info = item.get("media_info") or {}
                    add(
                        "video",
                        cls._pick_video_url(item),
                        info.get("duration") if isinstance(info, dict) else 0,
                    )
                elif entry.get("type") in {"pic", "livephoto"}:
                    picture({**item, "type": item.get("type") or entry["type"]})
            if specs:
                return specs

        pids = set()
        for pic in data.get("pics") or []:
            if isinstance(pic, dict):
                picture(pic)
                if pid := pic.get("pid") or pic.get("pic_id"):
                    pids.add(str(pid))
        infos = data.get("pic_infos")
        if isinstance(infos, dict):
            ids = list(dict.fromkeys([*(data.get("pic_ids") or []), *infos]))
            for pid in ids:
                if str(pid) not in pids:
                    picture(infos.get(pid))
        # Legacy status layouts may only expose pic_ids or URL objects.
        if not specs:
            for url in cls._collect_static_pic_urls(data):
                add("image", url)
        for url, duration in cls._collect_video_items(data):
            add("video", url, duration)
        return specs

    @handle(
        "weibo.com/tv/show",
        r"weibo\.com/tv/show/[^\s?]+[^\s]*?[?&]mid=([0-9]+)",
    )
    @handle("weibo.com", r"weibo\.com/(?:u/)?[0-9]+/([a-zA-Z0-9]+)")
    @handle("weibo.cn", r"weibo\.cn/(?:status|detail)/([a-zA-Z0-9]+)")
    @handle("weibo.cn", r"m\.weibo\.cn/[0-9]+/([a-zA-Z0-9]+)")
    async def _parse_weibo(self, searched: Match[str]):
        bid = searched.group(1)
        if "/tv/show/" in searched.group(0):
            bid = self._mid_to_bid(bid)
        logger.debug(f"[Weibo] 尝试解析微博: {bid}")
        data = await self._fetch_status_data(bid)

        user = data.get("user", {})
        author_name = user.get("screen_name", "微博用户")
        author_avatar = (
            normalize_image_url(
                user.get("avatar_hd")
                or user.get("avatar_large")
                or user.get("profile_image_url", "")
            )
            or ""
        )

        text = await self._resolve_body_text(data, bid)
        card_emotes = select_text_emotes(
            "\n".join(
                value
                for value in (
                    self._html_to_plain_text(data.get("status_title")),
                    text,
                )
                if value
            ),
            "weibo",
            self._extract_body_emotes(data),
        )

        timestamp = None
        if created_at := data.get("created_at"):
            try:
                dt = parsedate_to_datetime(created_at)
                timestamp = int(dt.timestamp())
            except Exception:
                pass

        media_sources = [data]
        if isinstance(data.get("retweeted_status"), dict):
            media_sources.append(data["retweeted_status"])

        contents = []
        static_pic_urls = []
        seen_media = set()
        for media_source in media_sources:
            for kind, url, duration, still in self._media_specs(media_source):
                if (kind, url) in seen_media:
                    continue
                seen_media.add((kind, url))
                if kind == "image":
                    static_pic_urls.append(url)
                    contents.extend(self.create_image_contents([url]))
                elif kind == "live":
                    if still:
                        static_pic_urls.append(still)
                    contents.append(live_photo(self, url, still, headers=self.headers))
                else:
                    task = self.downloader.download_video(
                        url,
                        video_name=f"weibo_{bid}_{len(contents)}.mp4",
                        ext_headers=self.headers,
                    )
                    contents.append(VideoContent(task, None, duration=duration))

        image_contents = [item for item in contents if isinstance(item, ImageContent)]
        video_contents = [item for item in contents if isinstance(item, VideoContent)]
        summary = "" if contents else self._delivery_summary(data, text)
        ordered = (
            any(isinstance(item, DynamicContent) for item in contents)
            or bool(image_contents and video_contents)
            or any(source.get("mix_media_info") for source in media_sources)
        )
        delivery = (
            ordered_media_plan(contents)
            if ordered
            else self._delivery_plan(summary, image_contents, video_contents)
        )

        comment_title = re.sub(r"\s+", " ", text).strip()
        if len(comment_title) > 64:
            comment_title = f"{comment_title[:61]}..."
        extra = self._comment_extra(
            str(data.get("id") or data.get("mid") or bid),
            title=comment_title or f"{author_name}的微博",
            cover=(static_pic_urls[0] if static_pic_urls else author_avatar),
            owner_id=user.get("id"),
        )
        if contents:
            extra["native_delivery"] = True
        if not contents:
            extra.update(
                {
                    "render_text_card": True,
                    "text_card_avatar": author_avatar,
                    "card_kind": "微博",
                    "card_author_badge": "认证" if user.get("verified") else "博主",
                    "card_metrics": [
                        ("评论", data.get("comments_count")),
                        ("点赞", data.get("attitudes_count")),
                        ("转发", data.get("reposts_count")),
                    ],
                    "card_info": [
                        "正文完整保留",
                        *(
                            ["含转发原文"]
                            if isinstance(data.get("retweeted_status"), dict)
                            else []
                        ),
                    ],
                    "card_emotes": card_emotes,
                }
            )

        author = self.create_author(
            author_name, author_avatar, ext_headers=self.headers
        )
        original_url = (
            f"https://weibo.com/{user.get('id')}/{bid}"
            if user.get("id")
            else f"https://m.weibo.cn/detail/{bid}"
        )

        return self.result(
            title=self._html_to_plain_text(data.get("status_title")) or None,
            text=text,
            author=author,
            contents=contents,
            delivery=delivery,
            timestamp=timestamp,
            url=original_url,
            extra=extra,
        )

    @staticmethod
    def _pick_video_url(page_info: dict) -> str | None:
        if not isinstance(page_info, dict):
            return None
        info = page_info.get("media_info") or {}
        urls = page_info.get("urls") or {}
        info = info if isinstance(info, dict) else {}
        urls = urls if isinstance(urls, dict) else {}
        for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4"):
            if url := media_url(urls.get(key)):
                return url
        for key in (
            "mp4_720p_mp4",
            "mp4_hd_url",
            "mp4_sd_url",
            "stream_url_hd",
            "stream_url",
            "replay_hd",
        ):
            if url := media_url(info.get(key)):
                return url
        for value in urls.values():
            if url := media_url(value):
                return url
        for playback in info.get("playback_list") or []:
            if isinstance(playback, dict) and isinstance(
                playback.get("play_info"), dict
            ):
                if url := media_url(playback["play_info"].get("url")):
                    return url
        return None

    @classmethod
    def _collect_video_items(cls, data: dict) -> list[tuple[str, float | int]]:
        if not isinstance(data, dict):
            return []

        items: list[tuple[str, float | int, str]] = []

        page_info = data.get("page_info") or {}
        if isinstance(page_info, dict) and page_info.get("type") == "video":
            video_url = cls._pick_video_url(page_info)
            if video_url:
                media_info = page_info.get("media_info") or {}
                if not isinstance(media_info, dict):
                    media_info = {}
                items.append((video_url, media_info.get("duration", 0), "page_info"))

        pics = data.get("pics")
        if isinstance(pics, list):
            for pic in pics:
                if not isinstance(pic, dict) or not cls._is_video_pic(pic):
                    continue

                video_url = pic.get("videoSrc") or pic.get("video_src")
                if isinstance(video_url, str) and video_url:
                    items.append((video_url, 0, f"pic:{pic.get('pid') or video_url}"))

        # url_objects 和 page_info 往往指向同一个视频；只有前面没拿到视频时才兜底。
        if not items:
            for video_obj in cls._iter_url_object_videos(data):
                video_url = cls._pick_video_url(video_obj)
                if not video_url:
                    continue
                media_info = video_obj.get("media_info") or {}
                if not isinstance(media_info, dict):
                    media_info = {}
                duration = media_info.get("duration", video_obj.get("duration", 0))
                items.append(
                    (video_url, duration, cls._video_key(video_obj, video_url))
                )

        out: list[tuple[str, float | int]] = []
        seen: set[str] = set()
        for video_url, duration, _key in items:
            dedupe_key = cls._normalize_video_url_key(video_url)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append((video_url, duration))

        return out

    @classmethod
    def _iter_url_object_videos(cls, data: dict):
        containers = []
        for key in ("url_objects", "url_struct"):
            value = data.get(key)
            if isinstance(value, list):
                containers.extend(value)

        long_text = data.get("longText")
        if isinstance(long_text, dict):
            value = long_text.get("url_objects")
            if isinstance(value, list):
                containers.extend(value)

        seen_ids: set[int] = set()
        for item in containers:
            if not isinstance(item, dict):
                continue

            video_obj = cls._extract_video_object(item)
            if not video_obj:
                continue

            marker = id(video_obj)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            yield video_obj

    @classmethod
    def _extract_video_object(cls, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        if item.get("type") == "video" or item.get("object_type") == "video":
            return item

        obj = item.get("object")
        if isinstance(obj, dict):
            if obj.get("object_type") == "video":
                nested = obj.get("object")
                return nested if isinstance(nested, dict) else obj

            nested = obj.get("object")
            if isinstance(nested, dict) and nested.get("object_type") == "video":
                return nested

        return None

    @staticmethod
    def _video_key(video_obj: dict, video_url: str) -> str:
        for key in ("object_id", "id", "fid", "mid"):
            value = video_obj.get(key)
            if value:
                return str(value)
        return WeiboParser._normalize_video_url_key(video_url)

    @staticmethod
    def _normalize_video_url_key(url: str) -> str:
        return re.sub(
            r"^https?://", "", (url or "").split("?", 1)[0], flags=re.IGNORECASE
        )

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

        for key in ("largest", "large", "original", "bmiddle", "thumbnail"):
            item = pic.get(key)
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url

        url = pic.get("url")
        return url if isinstance(url, str) and url else None

    @staticmethod
    def _html_to_plain_text(text: str | None) -> str:
        if not text:
            return ""

        text = str(text)
        text = re.sub(
            r"</(?:p|div|h[1-6]|blockquote|li)>|<br\s*/?>",
            "\n",
            text,
            flags=re.IGNORECASE,
        )

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
        text = unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
