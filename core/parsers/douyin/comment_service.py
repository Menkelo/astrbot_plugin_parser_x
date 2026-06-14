import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from msgspec import json as msgjson

from ...data import ImageContent
from ..bilibili.comment_renderer import BiliCommentRenderer


class DouyinCommentService:
    def __init__(
        self,
        parser,
        renderer: BiliCommentRenderer,
        *,
        comment_limit: int = 9,
        enable_text_ad_filter: bool = True,
    ):
        self.parser = parser
        self.renderer = renderer
        self.comment_limit = comment_limit
        self.enable_text_ad_filter = enable_text_ad_filter

        self._ad_kw_re = re.compile(
            r"(微信|v信|vx|加微|私信|进群|福利|代理|兼职|看片|资源|加我|联系我|返利|推广|引流|合作)",
            re.IGNORECASE,
        )
        self._contact_re = re.compile(
            r"(wx[:：]?\s*[a-zA-Z][-_a-zA-Z0-9]{4,}|qq[:：]?\s*\d{5,}|tg[:：]?\s*[a-zA-Z0-9_]{4,})",
            re.IGNORECASE,
        )
        self._shortlink_re = re.compile(
            r"(https?://)?([a-zA-Z0-9-]+\.)?(t\.cn|u\.jd\.com|dwz\.cn|v\.douyin\.com|b23\.tv)/",
            re.IGNORECASE,
        )

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    @property
    def client(self):
        return self.parser.client

    @staticmethod
    def _format_ts(ts: int | None) -> str | None:
        if not ts:
            return None
        try:
            ts = int(ts)
            if ts > 10_000_000_000:
                ts //= 1000
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except Exception:
            return None

    @staticmethod
    def _clean_text(text: str | None) -> str:
        if not text:
            return ""
        text = re.sub(r"\[.*?\]", "", str(text)).strip()
        return text.replace("@", "@\u200b")

    @staticmethod
    def _pick_url(obj: Any) -> str | None:
        if isinstance(obj, str):
            return obj if obj.startswith(("http://", "https://")) else None

        if isinstance(obj, list):
            for item in obj:
                url = DouyinCommentService._pick_url(item)
                if url:
                    return url
            return None

        if not isinstance(obj, dict):
            return None

        for key in (
            "url_list",
            "urlList",
            "url",
            "uri",
            "origin_url",
            "originUrl",
            "display_url",
            "displayUrl",
            "thumb_url",
            "thumbUrl",
        ):
            if key not in obj:
                continue
            url = DouyinCommentService._pick_url(obj.get(key))
            if url:
                return url

        return None

    @staticmethod
    def _extract_comment_pic(item: dict) -> str | None:
        for key in ("image_list", "imageList", "images", "pictures"):
            values = item.get(key)
            if not isinstance(values, list):
                continue
            for image in values:
                url = DouyinCommentService._pick_url(image)
                if url:
                    return url
        return None

    @staticmethod
    def _extract_comments(data: dict) -> list[dict]:
        for path in (
            ("comments",),
            ("comment_list",),
            ("data", "comments"),
            ("data", "comment_list"),
        ):
            node: Any = data
            for key in path:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if isinstance(node, list):
                return node
        return []

    @staticmethod
    def _extract_cursor(data: dict, default: int) -> int:
        for path in (
            ("cursor",),
            ("data", "cursor"),
        ):
            node: Any = data
            for key in path:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if isinstance(node, (int, float, str)):
                try:
                    return int(node)
                except Exception:
                    pass
        return default

    @staticmethod
    def _extract_has_more(data: dict) -> bool:
        for path in (
            ("has_more",),
            ("hasMore",),
            ("data", "has_more"),
            ("data", "hasMore"),
        ):
            node: Any = data
            for key in path:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if isinstance(node, bool):
                return node
            if isinstance(node, (int, float, str)):
                return str(node) not in {"0", "False", "false", ""}
        return False

    def _is_ad_like_text(self, text: str) -> bool:
        if not text:
            return False
        return bool(
            self._ad_kw_re.search(text)
            or self._contact_re.search(text)
            or self._shortlink_re.search(text)
        )

    def _build_headers(self, referer: str) -> dict[str, str]:
        headers = self.parser.android_headers.copy()
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": referer,
            }
        )
        return headers

    async def _fetch_page(
        self,
        *,
        aweme_id: str,
        cursor: int,
        count: int,
        referer: str,
    ) -> dict:
        headers = self._build_headers(referer)
        endpoints = (
            (
                "https://aweme.snssdk.com/aweme/v1/comment/list/",
                {
                    "aweme_id": aweme_id,
                    "cursor": str(cursor),
                    "count": str(count),
                },
            ),
            (
                "https://www.douyin.com/aweme/v1/web/comment/list/",
                {
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "aweme_id": aweme_id,
                    "cursor": str(cursor),
                    "count": str(count),
                    "item_type": "0",
                },
            ),
        )

        first_ok: dict | None = None
        for url, params in endpoints:
            try:
                resp = await self.client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=6,
                )
                if resp.status_code != 200 or not resp.content:
                    continue

                data = msgjson.decode(resp.content)
                if not isinstance(data, dict):
                    continue

                status = data.get("status_code", data.get("code", 0))
                if status not in (0, "0", None):
                    continue

                if first_ok is None:
                    first_ok = data

                if self._extract_comments(data):
                    return data

            except Exception as e:
                logger.debug(f"[Douyin] comment api failed aweme={aweme_id} url={url}: {e}")
                continue

        return first_ok or {}

    async def build_comment_image_content(
        self,
        aweme_id: str,
        *,
        video_title: str,
        video_cover: str | None,
        video_author: str | None = None,
        video_timestamp: int | None = None,
        referer: str | None = None,
    ) -> list[ImageContent]:
        aweme_id = str(aweme_id or "").strip()
        if not aweme_id:
            return []

        referer = referer or f"https://www.douyin.com/video/{aweme_id}"
        comments_data: list[dict] = []
        seen: set[str] = set()
        cursor = 0
        max_pages = 3
        count = max(12, min(20, self.comment_limit * 2))

        for _ in range(max_pages):
            if len(comments_data) >= self.comment_limit:
                break

            data = await self._fetch_page(
                aweme_id=aweme_id,
                cursor=cursor,
                count=count,
                referer=referer,
            )
            comments = self._extract_comments(data)
            if not comments:
                break

            for item in comments:
                if not isinstance(item, dict):
                    continue

                cid = str(item.get("cid") or item.get("comment_id") or item.get("id") or "")
                if cid and cid in seen:
                    continue
                if cid:
                    seen.add(cid)

                message = self._clean_text(item.get("text") or item.get("content"))
                pic_url = self._extract_comment_pic(item)
                if not message and not pic_url:
                    continue
                if self.enable_text_ad_filter and self._is_ad_like_text(message):
                    continue

                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                avatar = self._pick_url(
                    user.get("avatar_thumb")
                    or user.get("avatar_medium")
                    or user.get("avatar_larger")
                    or user.get("avatar_url")
                    or user.get("avatar")
                )

                comments_data.append(
                    {
                        "avatar": avatar,
                        "uname": self._clean_text(user.get("nickname") or user.get("unique_id") or ""),
                        "message": message,
                        "pic": pic_url,
                        "comment_time": self._format_ts(item.get("create_time") or item.get("createTime")),
                    }
                )

                if len(comments_data) >= self.comment_limit:
                    break

            if not self._extract_has_more(data):
                break

            next_cursor = self._extract_cursor(data, cursor)
            if next_cursor <= cursor:
                cursor += count
            else:
                cursor = next_cursor

        comments_data = comments_data[: self.comment_limit]
        if not comments_data:
            return []

        c_hash = hashlib.md5(
            (
                f"{comments_data[0]}"
                f"{self.comment_limit}"
                f"{video_title}"
                f"{video_cover}"
                f"{video_author}"
                f"{video_timestamp}"
                "_douyin_comment_v1"
            ).encode()
        ).hexdigest()[:8]

        out_path = self.cache_dir / f"douyin_comments_merged_{aweme_id}_{c_hash}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def _render_then_return():
            await self.renderer.render_merged_comments(
                out_path=out_path,
                comments=comments_data,
                video_title=video_title,
                video_cover=video_cover,
                video_author=video_author,
                video_time=self._format_ts(video_timestamp),
                author_label="作者",
            )
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    _render_then_return(),
                    name=f"douyin_comment_render_{aweme_id}",
                )
            )
        ]
