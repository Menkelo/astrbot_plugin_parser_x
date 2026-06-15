import asyncio
import hashlib
import re
import time
from pathlib import Path

from msgspec import json as msgjson

from astrbot.api import logger

from ...data import ImageContent
from .comment_renderer import BiliCommentRenderer


class BiliCommentService:
    def __init__(
        self,
        parser,
        renderer: BiliCommentRenderer,
        *,
        comment_limit: int = 9,
        enable_text_ad_filter: bool = True,
        enable_qr_filter: bool = True,
        qr_check_max: int = 4,
        qr_check_timeout: float = 6.0,
    ):
        self.parser = parser
        self.renderer = renderer
        self.comment_limit = comment_limit
        self.enable_text_ad_filter = enable_text_ad_filter
        self.enable_qr_filter = enable_qr_filter
        self.qr_check_max = qr_check_max
        self.qr_check_timeout = qr_check_timeout

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

        self._qr_detect_cache: dict[str, bool] = {}

    @property
    def headers(self) -> dict[str, str]:
        return self.parser.headers

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    @property
    def client(self):
        return self.parser.client

    @staticmethod
    def _neutralize_at_text(text: str) -> str:
        if not text:
            return text
        return text.replace("@", "＠")

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
    def _is_bili_default_avatar(url: str | None) -> bool:
        if not url:
            return True

        u = str(url).lower()

        markers = [
            "noface",
            "no_face",
            "member/noface",
            "bili_default",
            "default",
            "akari.jpg",
        ]

        return any(m in u for m in markers)

    def _is_ad_like_text(self, text: str) -> bool:
        if not text:
            return False
        return bool(
            self._ad_kw_re.search(text)
            or self._contact_re.search(text)
            or self._shortlink_re.search(text)
        )

    async def _has_qr_in_image(self, img_url: str) -> bool:
        if not img_url:
            return False

        if img_url in self._qr_detect_cache:
            return self._qr_detect_cache[img_url]

        if len(self._qr_detect_cache) > 512:
            self._qr_detect_cache.clear()

        try:
            resp = await self.client.get(
                img_url,
                headers=self.headers,
                timeout=self.qr_check_timeout,
            )
            if resp.status_code != 200 or not resp.content:
                self._qr_detect_cache[img_url] = False
                return False

            try:
                import cv2
                import numpy as np

                arr = np.frombuffer(resp.content, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    self._qr_detect_cache[img_url] = False
                    return False

                detector = cv2.QRCodeDetector()
                decoded, points, _ = detector.detectAndDecode(img)
                has_qr = bool(decoded) or points is not None
                self._qr_detect_cache[img_url] = has_qr
                return has_qr
            except Exception:
                self._qr_detect_cache[img_url] = False
                return False

        except Exception:
            self._qr_detect_cache[img_url] = False
            return False

    async def _should_skip_comment(
        self,
        message: str,
        pic_url: str | None,
        qr_check_counter: list[int],
    ) -> bool:
        if self.enable_text_ad_filter and self._is_ad_like_text(message):
            return True

        if (
            self.enable_qr_filter
            and pic_url
            and qr_check_counter[0] < self.qr_check_max
            and (not message or len(message.strip()) <= 8)
        ):
            qr_check_counter[0] += 1
            if await self._has_qr_in_image(pic_url):
                return True

        return False

    async def build_comment_image_content(
        self,
        oid: int,
        type_: int,
        *,
        video_title: str,
        video_cover: str | None,
        video_author: str | None = None,
        video_timestamp: int | None = None,
    ) -> list[ImageContent]:
        url = "https://api.bilibili.com/x/v2/reply/main"
        strict_list = []
        relaxed_list = []
        fallback_list = []
        seen = set()

        next_cursor = 0
        is_end = False

        max_pages = 3

        qr_check_counter = [0]

        for _ in range(max_pages):
            candidate_count = len(strict_list) + len(relaxed_list) + len(fallback_list)
            if is_end or len(strict_list) >= self.comment_limit or candidate_count >= self.comment_limit:
                break

            params = {
                "oid": oid,
                "type": type_,
                "mode": 3,
                "next": next_cursor,
                "ps": 20,
            }

            try:
                resp = await self.client.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=5,
                )
                if resp.status_code != 200:
                    break

                data = msgjson.decode(resp.content)
                if data.get("code") != 0:
                    break

                block = data.get("data") or {}
                replies = block.get("replies") or []
                cursor = block.get("cursor") or {}
                is_end = bool(cursor.get("is_end"))
                next_cursor = cursor.get("next", next_cursor + 1)

                for item in replies:
                    rpid = item.get("rpid")
                    if rpid in seen:
                        continue
                    seen.add(rpid)

                    content = item.get("content", {})
                    member = item.get("member", {})

                    raw_msg = content.get("message") or ""
                    message = re.sub(r"\[.*?\]", "", raw_msg).strip()
                    message = self._neutralize_at_text(message)

                    pics = content.get("pictures") or []
                    pic_url = pics[0].get("img_src") if pics else None

                    if not message and not pic_url:
                        continue

                    if await self._should_skip_comment(message, pic_url, qr_check_counter):
                        continue

                    avatar = member.get("avatar", "")

                    data_obj = {
                        "avatar": "" if self._is_bili_default_avatar(avatar) else avatar,
                        "uname": self._neutralize_at_text(member.get("uname", "")),
                        "message": message,
                        "pic": pic_url,
                        "comment_time": self._format_ts(item.get("ctime")),
                    }

                    if self._is_bili_default_avatar(avatar):
                        fallback_list.append(data_obj)
                        continue

                    if "＠" in message:
                        relaxed_list.append(data_obj)
                    else:
                        strict_list.append(data_obj)

                    candidate_count = len(strict_list) + len(relaxed_list) + len(fallback_list)
                    if len(strict_list) >= self.comment_limit or candidate_count >= self.comment_limit:
                        break

            except Exception as e:
                logger.debug(f"[Bilibili] 评论抓取错误 oid={oid}: {e}")
                break

        if len(strict_list) < self.comment_limit:
            need = self.comment_limit - len(strict_list)
            strict_list.extend(relaxed_list[:need])

        if len(strict_list) < self.comment_limit:
            need = self.comment_limit - len(strict_list)
            strict_list.extend(fallback_list[:need])

        comments_data = strict_list[: self.comment_limit]
        if not comments_data:
            return []

        video_time_text = self._format_ts(video_timestamp)

        comments_digest = "|".join(
            (
                f"{item.get('uname', '')}\0"
                f"{item.get('message', '')}\0"
                f"{item.get('pic', '')}\0"
                f"{item.get('comment_time', '')}"
            )
            for item in comments_data
        )
        c_hash = hashlib.md5(
            (
                f"{comments_digest}"
                f"{len(comments_data)}"
                f"{self.comment_limit}"
                f"{video_title}"
                f"{video_cover}"
                f"{video_author}"
                f"{video_timestamp}"
                f"_full_comment_digest_v1"
            ).encode()
        ).hexdigest()[:8]

        out_path = self.cache_dir / f"bili_comments_merged_{oid}_{c_hash}.png"

        if out_path.exists() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def _render_then_return():
            await self.renderer.render_merged_comments(
                out_path=out_path,
                comments=comments_data,
                video_title=video_title,
                video_cover=video_cover,
                video_author=video_author,
                video_time=video_time_text,
            )
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    _render_then_return(),
                    name=f"bili_comment_render_{oid}",
                )
            )
        ]
