import asyncio
import hashlib
import re
import time
import urllib.parse
from pathlib import Path

from msgspec import json as msgjson

from astrbot.api import logger

from ...data import ImageContent
from .comment_renderer import BiliCommentRenderer


MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32,
    15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19,
    29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]


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
        self._wbi_mixin_key: str | None = None
        self._wbi_mixin_key_expire = 0.0

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

    def _comment_headers(self, referer: str | None = None) -> dict[str, str]:
        headers = self.headers.copy()
        headers["Referer"] = referer or "https://www.bilibili.com/"
        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck
        return headers

    @staticmethod
    def _wbi_key_part(url: str | None) -> str | None:
        if not url:
            return None
        name = str(url).rsplit("/", 1)[-1].split(".", 1)[0].strip()
        return name or None

    async def _get_wbi_mixin_key(self) -> str | None:
        now = time.time()
        if self._wbi_mixin_key and now < self._wbi_mixin_key_expire:
            return self._wbi_mixin_key

        try:
            resp = await self.parser.http_get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=self._comment_headers(),
                allow_redirects=True,
                timeout=8,
            )
            data = msgjson.decode(resp.content)
            wbi_img = ((data.get("data") or {}).get("wbi_img") or {})
            img_key = self._wbi_key_part(wbi_img.get("img_url"))
            sub_key = self._wbi_key_part(wbi_img.get("sub_url"))
            raw_key = f"{img_key or ''}{sub_key or ''}"
            if len(raw_key) < 64:
                return None

            mixin_key = "".join(raw_key[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._wbi_mixin_key = mixin_key
            self._wbi_mixin_key_expire = now + 12 * 60 * 60
            return mixin_key
        except Exception as e:
            logger.debug(f"[Bilibili] WBI key 获取失败: {e}")
            return None

    @staticmethod
    def _sign_wbi_params(params: dict, mixin_key: str) -> dict:
        signed = dict(params)
        signed["wts"] = int(time.time())

        filtered = {}
        for key, value in signed.items():
            if isinstance(value, str):
                value = "".join(ch for ch in value if ch not in "!'()*")
            filtered[key] = value

        query = urllib.parse.urlencode(sorted(filtered.items()))
        filtered["w_rid"] = hashlib.md5(f"{query}{mixin_key}".encode()).hexdigest()
        return filtered

    async def _get_comment_json(
        self,
        url: str,
        params: dict,
        *,
        referer: str,
    ) -> dict:
        resp = await self.parser.http_get(
            url,
            params=params,
            headers=self._comment_headers(referer),
            allow_redirects=True,
            timeout=8,
        )
        if resp.status_code != 200 or not resp.content:
            return {}
        try:
            data = msgjson.decode(resp.content)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _normalize_legacy_reply_page(block: dict, page_num: int) -> dict:
        page = block.get("page") or {}
        try:
            total = int(page.get("count") or 0)
            size = int(page.get("size") or 20)
            current = int(page.get("num") or page_num)
        except Exception:
            total = 0
            size = 20
            current = page_num

        replies = block.get("replies") or []
        block["cursor"] = {
            "is_end": (not replies) or (total > 0 and current * size >= total),
            "next": current + 1,
        }
        return block

    async def _fetch_comment_page(
        self,
        *,
        oid: int,
        type_: int,
        next_cursor: int,
        referer: str,
    ) -> dict:
        base_params = {
            "oid": oid,
            "type": type_,
            "mode": 3,
            "next": next_cursor,
            "ps": 20,
        }

        mixin_key = await self._get_wbi_mixin_key()
        if mixin_key:
            try:
                data = await self._get_comment_json(
                    "https://api.bilibili.com/x/v2/reply/wbi/main",
                    self._sign_wbi_params(base_params, mixin_key),
                    referer=referer,
                )
                if data.get("code") == 0:
                    return data.get("data") or {}
                logger.debug(
                    "[Bilibili] WBI 评论接口失败 "
                    f"oid={oid} code={data.get('code')} msg={data.get('message') or data.get('msg')}"
                )
            except Exception as e:
                logger.debug(f"[Bilibili] WBI 评论接口异常 oid={oid}: {e}")

        try:
            data = await self._get_comment_json(
                "https://api.bilibili.com/x/v2/reply/main",
                base_params,
                referer=referer,
            )
            if data.get("code") == 0:
                return data.get("data") or {}
        except Exception as e:
            logger.debug(f"[Bilibili] 评论 main 兜底异常 oid={oid}: {e}")

        try:
            page_num = max(1, int(next_cursor or 1))
            data = await self._get_comment_json(
                "https://api.bilibili.com/x/v2/reply",
                {
                    "oid": oid,
                    "type": type_,
                    "sort": 2,
                    "pn": page_num,
                    "ps": 20,
                },
                referer=referer,
            )
            if data.get("code") == 0:
                return self._normalize_legacy_reply_page(data.get("data") or {}, page_num)
        except Exception as e:
            logger.debug(f"[Bilibili] 评论 legacy 兜底异常 oid={oid}: {e}")

        return {}

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
        strict_list = []
        relaxed_list = []
        fallback_list = []
        seen = set()

        next_cursor = 0
        is_end = False

        max_pages = 5
        referer = f"https://www.bilibili.com/video/av{oid}"

        qr_check_counter = [0]

        for _ in range(max_pages):
            candidate_count = len(strict_list) + len(relaxed_list) + len(fallback_list)
            if is_end or len(strict_list) >= self.comment_limit or candidate_count >= self.comment_limit:
                break

            try:
                block = await self._fetch_comment_page(
                    oid=oid,
                    type_=type_,
                    next_cursor=next_cursor,
                    referer=referer,
                )
                if not block:
                    break

                top_replies = block.get("top_replies") or []
                replies = [*top_replies, *(block.get("replies") or [])]
                cursor = block.get("cursor") or {}
                is_end = bool(cursor.get("is_end"))
                try:
                    next_cursor = int(cursor.get("next", next_cursor + 1))
                except Exception:
                    next_cursor += 1

                for item in replies:
                    rpid = item.get("rpid") or item.get("rpid_str")
                    if rpid in seen:
                        continue
                    seen.add(rpid)

                    content = item.get("content", {})
                    member = item.get("member", {})

                    raw_msg = content.get("message") or ""
                    message = re.sub(r"\[.*?\]", "", raw_msg).strip()
                    message = self._neutralize_at_text(message)

                    pics = content.get("pictures") or []
                    pic_url = self.parser.norm_bili_img(pics[0].get("img_src")) if pics else None

                    if not message and not pic_url:
                        continue

                    if await self._should_skip_comment(message, pic_url, qr_check_counter):
                        continue

                    avatar = self.parser.norm_bili_img(member.get("avatar", "")) or ""

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
