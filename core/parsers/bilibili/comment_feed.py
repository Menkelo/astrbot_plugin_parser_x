from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

from astrbot.api import logger
from msgspec import json as msgjson

from ...constants import COMMENT_FOOTER_BRAND
from ...data import ImageContent
from ...utils import cached_image_to_data_uri
from .comment_canvas import (
    BiliAuthorBadge,
    BiliCommentCanvas,
    BiliCommentDecor,
    BiliCommentDocument,
    BiliCommentEntry,
    BiliFanMedal,
    BiliRichPart,
)

_WBI_INDEX = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


@dataclass(slots=True)
class _RawCommentFeed:
    items: list[dict]
    owner_mid: str
    total: int


class BiliCommentFeed:
    """Adapt rconsole ``utils/bili-comment.js`` semantics for AstrBot."""

    CACHE_VERSION = "bili_comment_v11_unified_clean"

    def __init__(
        self,
        parser,
        canvas: BiliCommentCanvas,
        *,
        limit: int = 9,
    ):
        self.parser = parser
        self.canvas = canvas
        self.limit = max(1, int(limit))
        self._mixin_key = ""
        self._mixin_key_deadline = 0.0
        self._mixin_key_lock = asyncio.Lock()
        self._avatar_data_uri_cache: dict[str, str | None] = {}

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    def _headers(self, referer: str) -> dict[str, str]:
        headers = self.parser.headers.copy()
        headers["Referer"] = referer
        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck
        return headers

    @staticmethod
    def _image_key(url: object) -> str:
        name = str(url or "").rsplit("/", 1)[-1].split(".", 1)[0]
        return name.strip()

    async def _load_mixin_key(self) -> str:
        now = time.monotonic()
        if self._mixin_key and now < self._mixin_key_deadline:
            return self._mixin_key

        async with self._mixin_key_lock:
            now = time.monotonic()
            if self._mixin_key and now < self._mixin_key_deadline:
                return self._mixin_key
            try:
                payload = await self._request_json(
                    "https://api.bilibili.com/x/web-interface/nav",
                    {},
                    referer="https://www.bilibili.com/",
                )
                image_data = (payload.get("data") or {}).get("wbi_img") or {}
                source = self._image_key(image_data.get("img_url"))
                source += self._image_key(image_data.get("sub_url"))
                if len(source) < 64:
                    return ""
                self._mixin_key = "".join(source[index] for index in _WBI_INDEX)[:32]
                self._mixin_key_deadline = now + 12 * 60 * 60
                return self._mixin_key
            except Exception as exc:
                logger.debug(f"[Bilibili] 评论 WBI 密钥获取失败: {exc}")
                return ""

    @staticmethod
    def _sign(params: dict[str, object], mixin_key: str) -> dict[str, object]:
        values = {**params, "wts": int(time.time())}
        cleaned: dict[str, object] = {}
        for key, value in values.items():
            if isinstance(value, str):
                value = "".join(char for char in value if char not in "!'()*")
            cleaned[key] = value
        query = urllib.parse.urlencode(sorted(cleaned.items()))
        cleaned["w_rid"] = hashlib.md5(
            f"{query}{mixin_key}".encode(), usedforsecurity=False
        ).hexdigest()
        return cleaned

    async def _request_json(
        self,
        url: str,
        params: dict[str, object],
        *,
        referer: str,
    ) -> dict:
        response = await self.parser.http_get(
            url,
            params=params,
            headers=self._headers(referer),
            allow_redirects=True,
            timeout=8,
        )
        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")
        payload = msgjson.decode(response.content)
        if not isinstance(payload, dict):
            raise RuntimeError("评论接口返回了非对象数据")
        return payload

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("rpid") or item.get("rpid_str") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            output.append(item)
        return output

    @classmethod
    def _to_raw_feed(cls, payload: dict) -> _RawCommentFeed:
        block = payload.get("data") or {}
        replies = cls._dedupe(
            [
                *(block.get("hots") or []),
                *(block.get("top_replies") or []),
                *(block.get("replies") or []),
            ]
        )
        page = block.get("page") or {}
        cursor = block.get("cursor") or {}
        raw_total = page.get("count") or cursor.get("all_count") or len(replies)
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = len(replies)
        owner_mid = str((block.get("upper") or {}).get("mid") or "")
        return _RawCommentFeed(replies, owner_mid, total)

    async def fetch(self, oid: int, type_: int) -> _RawCommentFeed:
        referer = f"https://www.bilibili.com/video/av{oid}"
        page_size = 20
        candidate_target = max(20, min(60, self.limit * 2))
        base_params: dict[str, object] = {
            "type": type_,
            "oid": oid,
            "mode": 3,
            "ps": page_size,
        }

        mixin_key = await self._load_mixin_key()
        if mixin_key:
            try:
                items: list[dict] = []
                owner_mid = ""
                total = 0
                next_cursor = 0
                for _ in range(3):
                    payload = await self._request_json(
                        "https://api.bilibili.com/x/v2/reply/wbi/main",
                        self._sign(
                            {**base_params, "next": next_cursor},
                            mixin_key,
                        ),
                        referer=referer,
                    )
                    if payload.get("code") != 0:
                        logger.debug(
                            "[Bilibili] 评论 WBI 接口拒绝请求: "
                            f"code={payload.get('code')} "
                            f"message={payload.get('message')}"
                        )
                        break
                    feed = self._to_raw_feed(payload)
                    items = self._dedupe([*items, *feed.items])
                    owner_mid = owner_mid or feed.owner_mid
                    total = max(total, feed.total)
                    cursor = (payload.get("data") or {}).get("cursor") or {}
                    new_cursor = cursor.get("next")
                    if len(items) >= candidate_target or cursor.get("is_end"):
                        break
                    try:
                        new_cursor = int(new_cursor)
                    except (TypeError, ValueError):
                        break
                    if new_cursor == next_cursor:
                        break
                    next_cursor = new_cursor
                if items:
                    return _RawCommentFeed(items, owner_mid, total or len(items))
            except Exception as exc:
                logger.debug(f"[Bilibili] 评论 WBI 接口异常: {exc}")

        try:
            items = []
            owner_mid = ""
            total = 0
            for page_number in range(1, 4):
                payload = await self._request_json(
                    "https://api.bilibili.com/x/v2/reply",
                    {
                        "type": type_,
                        "oid": oid,
                        "sort": 1,
                        "ps": page_size,
                        "pn": page_number,
                        "nohot": 0 if page_number == 1 else 1,
                    },
                    referer=referer,
                )
                if payload.get("code") != 0:
                    logger.debug(
                        "[Bilibili] 评论经典接口拒绝请求: "
                        f"code={payload.get('code')} "
                        f"message={payload.get('message')}"
                    )
                    break
                feed = self._to_raw_feed(payload)
                items = self._dedupe([*items, *feed.items])
                owner_mid = owner_mid or feed.owner_mid
                total = max(total, feed.total)
                if len(items) >= candidate_target or not feed.items:
                    break
            if items:
                return _RawCommentFeed(items, owner_mid, total or len(items))
        except Exception as exc:
            logger.debug(f"[Bilibili] 评论经典接口异常: {exc}")

        return _RawCommentFeed([], "", 0)

    def _image_url(self, value: object) -> str:
        return self.parser.norm_bili_img(str(value or "")) or ""

    @staticmethod
    def _count_text(value: object) -> str:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number >= 100_000_000:
            return f"{number / 100_000_000:.1f}".removesuffix(".0") + "亿"
        if number >= 10_000:
            return f"{number / 10_000:.1f}".removesuffix(".0") + "万"
        return str(number)

    @staticmethod
    def _time_text(value: object) -> str:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return ""
        if timestamp <= 0:
            return ""
        if timestamp > 10_000_000_000:
            timestamp //= 1000

        now = datetime.now()
        comment_at = datetime.fromtimestamp(timestamp)
        delta_seconds = max(0, int((now - comment_at).total_seconds()))
        if delta_seconds < 60:
            return "刚刚"
        if delta_seconds < 3600:
            return f"{delta_seconds // 60}分钟前"
        if comment_at.date() == now.date():
            return f"今天{comment_at:%H:%M}"
        if comment_at.date() == (now - timedelta(days=1)).date():
            return f"昨天{comment_at:%H:%M}"
        if delta_seconds < 30 * 86400:
            return f"{delta_seconds // 86400}天前"
        if delta_seconds < 365 * 86400:
            return f"{comment_at:%m月%d日 %H:%M}"
        return f"{comment_at:%Y年%m月%d日}"

    @staticmethod
    def _color(value: object) -> str:
        if isinstance(value, int):
            if value <= 0:
                return ""
            return f"#{value & 0xFFFFFF:06x}"
        raw = str(value or "").strip()
        if not raw or raw == "0":
            return ""
        if raw.isdigit() and len(raw) != 6:
            try:
                return BiliCommentFeed._color(int(raw))
            except ValueError:
                return ""
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", raw)
        return f"#{match.group(1).lower()}" if match else ""

    @staticmethod
    def _append_plain(parts: list[BiliRichPart], text: str) -> None:
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if line:
                parts.append(BiliRichPart("text", text=line))
            if index < len(lines) - 1:
                parts.append(BiliRichPart("line-break"))

    def _rich_text(self, content: dict) -> list[BiliRichPart]:
        message = unescape(str(content.get("message") or ""))
        message = message.replace("\r\n", "\n").replace("\r", "\n")
        message = re.sub(r"\n{3,}", "\n\n", message).strip()
        if not message:
            return []

        markers: list[tuple[str, str, str]] = []
        for key, details in (content.get("emote") or {}).items():
            if not isinstance(details, dict):
                continue
            image = next(
                (
                    self._image_url(details.get(field))
                    for field in (
                        "url",
                        "gif_url",
                        "webp_url",
                        "img_url",
                        "image_url",
                        "uri",
                    )
                    if details.get(field)
                ),
                "",
            )
            if key and image:
                markers.append((str(key), "emote", image))

        for name in content.get("at_name_to_mid") or {}:
            decoded = unescape(str(name or "")).lstrip("@")
            if decoded:
                markers.append((f"@{decoded}", "highlight", f"@{decoded}"))

        for url, details in (content.get("jump_url") or {}).items():
            if not isinstance(details, dict):
                continue
            title = str(details.get("title") or "").strip()
            decoded_url = unescape(str(url or ""))
            if decoded_url and title:
                markers.append((decoded_url, "highlight", title))

        markers.sort(key=lambda marker: len(marker[0]), reverse=True)
        parts: list[BiliRichPart] = []
        index = 0
        while index < len(message):
            matched = next(
                (marker for marker in markers if message.startswith(marker[0], index)),
                None,
            )
            if matched is not None:
                key, kind, value = matched
                if kind == "emote":
                    parts.append(BiliRichPart("emote", text=key, url=value))
                else:
                    parts.append(BiliRichPart("highlight", text=value))
                consumed = len(key)
                if key.lower().startswith(("http://", "https://")):
                    full_url = re.match(r"https?://[^\s]+", message[index:], re.I)
                    if full_url:
                        consumed = max(consumed, len(full_url.group(0)))
                index += consumed
                continue

            positions = [
                message.find(marker[0], index)
                for marker in markers
                if message.find(marker[0], index) >= 0
            ]
            next_index = min(positions) if positions else len(message)
            self._append_plain(parts, message[index:next_index])
            index = next_index
        return parts

    def _images(self, content: dict) -> list[str]:
        output = []
        seen = set()
        for item in content.get("pictures") or []:
            if not isinstance(item, dict):
                continue
            url = self._image_url(item.get("img_src"))
            if url and url not in seen:
                seen.add(url)
                output.append(url)
        return output

    @staticmethod
    def _is_up(item: dict, owner_mid: str, *, nested: bool) -> bool:
        member = item.get("member") or {}
        comment_mid = item.get("mid") or item.get("mid_str")
        comment_mid = comment_mid or member.get("mid") or member.get("mid_str")
        if owner_mid and str(comment_mid or "") == owner_mid:
            return True
        return nested and bool((item.get("reply_control") or {}).get("up_reply"))

    def _fan_medal(self, member: dict) -> BiliFanMedal | None:
        details = member.get("fans_detail")
        if not isinstance(details, dict):
            return None
        name = str(
            details.get("medal_name")
            or details.get("name")
            or details.get("medalName")
            or ""
        ).strip()
        if not name:
            return None
        try:
            level_value = int(
                details.get("level")
                or details.get("medal_level")
                or details.get("fans_level")
                or 0
            )
        except (TypeError, ValueError):
            level_value = 0
        return BiliFanMedal(
            name=name,
            level=level_value or None,
            background=self._color(
                details.get("medal_color")
                or details.get("medal_color_start")
                or details.get("color")
                or details.get("background_color")
            ),
            foreground=self._color(details.get("medal_color_name")),
            level_background=self._color(
                details.get("medal_level_bg_color") or details.get("medal_color_end")
            ),
            level_foreground=self._color(
                details.get("medal_color_level")
                or details.get("level_color")
                or details.get("medal_color")
            ),
            border=self._color(
                details.get("medal_color_border")
                or details.get("border_color")
                or details.get("medal_color")
                or details.get("color")
            ),
        )

    def _decor(self, item: dict) -> BiliCommentDecor | None:
        member = item.get("member") or {}
        roots = [
            member.get("user_sailing_v2"),
            member.get("user_sailing"),
            item.get("member_sailing"),
        ]
        keys = ("card_bg", "card_bg_with_focus", "collect_card", "cardbg")
        for root in roots:
            if not isinstance(root, dict):
                continue
            for key in keys:
                candidate = root.get(key)
                if not isinstance(candidate, dict):
                    continue
                image = self._image_url(
                    candidate.get("image") or candidate.get("image_enhance")
                )
                fan = candidate.get("fan") or {}
                number = str(fan.get("num_desc") or "").strip()
                if not number and fan.get("number") is not None:
                    try:
                        number = f"{int(fan.get('number')):06d}"
                    except (TypeError, ValueError):
                        number = str(fan.get("number") or "")
                prefix = str(fan.get("num_prefix") or ("NO." if number else ""))
                text = f"{prefix}{number}" if number else ""
                color_source = fan.get("color")
                if not color_source:
                    colors = (fan.get("color_format") or {}).get("colors") or []
                    color_source = colors[0] if colors else ""
                if image or text:
                    return BiliCommentDecor(
                        image=image,
                        prefix=prefix,
                        number=number,
                        text=text,
                        color=self._color(color_source),
                    )
        return None

    def _author(self, item: dict, owner_mid: str, *, nested: bool) -> BiliAuthorBadge:
        member = item.get("member") or {}
        is_up = self._is_up(item, owner_mid, nested=nested)
        level_raw = (member.get("level_info") or {}).get("current_level")
        try:
            level = int(level_raw) if level_raw is not None else None
        except (TypeError, ValueError):
            level = None
        nickname_color = self._color(
            (member.get("vip") or {}).get("nickname_color")
            or (member.get("nameplate") or {}).get("nickname_color")
        )
        try:
            senior_status = int((member.get("senior") or {}).get("status") or 0)
        except (TypeError, ValueError):
            senior_status = 0
        return BiliAuthorBadge(
            nickname=str(member.get("uname") or "B站用户"),
            avatar=self._image_url(member.get("avatar")),
            nickname_color=nickname_color,
            level=level,
            senior=bool(member.get("is_senior_member") or senior_status > 0),
            is_up=is_up,
            fan_medal=None if is_up else self._fan_medal(member),
        )

    def adapt_comment(
        self,
        item: dict,
        owner_mid: str | int | None = None,
        *,
        nested: bool = False,
    ) -> BiliCommentEntry | None:
        content = item.get("content") or {}
        rich_text = self._rich_text(content)
        images = self._images(content)
        if not rich_text and not images:
            return None

        owner = str(owner_mid or "")
        control = item.get("reply_control") or {}
        location = re.sub(r"^IP属地：?", "", str(control.get("location") or "")).strip()
        try:
            reply_count = int(item.get("rcount") or 0)
        except (TypeError, ValueError):
            reply_count = 0
        first_reply = None
        if not nested:
            replies = item.get("replies") or []
            if replies and isinstance(replies[0], dict):
                first_reply = self.adapt_comment(
                    replies[0],
                    owner,
                    nested=True,
                )
        return BiliCommentEntry(
            author=self._author(item, owner, nested=nested),
            content=rich_text,
            images=images,
            time_text=self._time_text(item.get("ctime")),
            location=location,
            like_text=self._count_text(item.get("like")),
            reply_text=(
                f"回复 {self._count_text(reply_count)}" if reply_count > 0 else "回复"
            ),
            pinned=not nested
            and bool(
                control.get("is_up_top") or control.get("is_top") or item.get("is_top")
            ),
            up_liked=bool(
                (item.get("up_action") or {}).get("like") or control.get("up_like")
            ),
            meta_items=[
                value
                for value in (
                    "UP主回复" if control.get("up_reply") else "",
                    str(control.get("sub_reply_entry_text") or ""),
                )
                if value
            ],
            decor=None if nested else self._decor(item),
            first_reply=first_reply,
        )

    @staticmethod
    def _walk_entries(entries: list[BiliCommentEntry]) -> list[BiliCommentEntry]:
        output = []
        pending = list(entries)
        while pending:
            entry = pending.pop(0)
            output.append(entry)
            if entry.first_reply is not None:
                pending.append(entry.first_reply)
        return output

    async def _avatar_to_data_uri(self, avatar: str) -> str | None:
        return await cached_image_to_data_uri(
            self._avatar_data_uri_cache,
            self.parser.http_get,
            avatar,
            headers=self._headers("https://www.bilibili.com/"),
            referer="https://www.bilibili.com/",
            max_bytes=2 * 1024 * 1024,
            timeout=8,
            debug_label="[Bilibili] comment avatar",
        )

    async def _embed_avatars(self, entries: list[BiliCommentEntry]) -> None:
        all_entries = self._walk_entries(entries)
        avatar_urls = list(
            dict.fromkeys(
                entry.author.avatar
                for entry in all_entries
                if entry.author.avatar and not entry.author.avatar.startswith("data:")
            )
        )
        if not avatar_urls:
            return

        data_uris = await asyncio.gather(
            *(self._avatar_to_data_uri(url) for url in avatar_urls)
        )
        resolved = dict(zip(avatar_urls, data_uris, strict=True))
        for entry in all_entries:
            if data_uri := resolved.get(entry.author.avatar):
                entry.author.avatar = data_uri

    async def build_document(
        self,
        oid: int,
        type_: int,
        *,
        video_title: str,
        video_cover: str | None,
        owner_mid: str | int | None = None,
    ) -> BiliCommentDocument | None:
        raw_feed = await self.fetch(oid, type_)
        effective_owner_mid = owner_mid or raw_feed.owner_mid
        entries = []
        for item in raw_feed.items:
            entry = self.adapt_comment(item, effective_owner_mid)
            if entry is not None:
                entries.append(entry)
            if len(entries) >= self.limit:
                break
        if not entries:
            return None

        partial = raw_feed.total > len(entries) or len(raw_feed.items) > len(entries)
        document = BiliCommentDocument(
            work_title=video_title or "B站视频",
            cover=self._image_url(video_cover),
            total_text=f"{self._count_text(raw_feed.total)} 条评论",
            entries=entries,
            footer_text=(
                f"仅展示部分热门评论 · {COMMENT_FOOTER_BRAND}"
                if partial
                else f"{COMMENT_FOOTER_BRAND} · B站评论区"
            ),
        )
        await self._embed_avatars(document.entries)
        return document

    async def build_images(
        self,
        oid: int,
        type_: int,
        *,
        video_title: str,
        video_cover: str | None,
        owner_mid: str | int | None = None,
    ) -> list[ImageContent]:
        document = await self.build_document(
            oid,
            type_,
            video_title=video_title,
            video_cover=video_cover,
            owner_mid=owner_mid,
        )
        if document is None:
            return []
        serialised = json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.CACHE_VERSION}|{serialised}".encode()
        ).hexdigest()[:12]
        out_path = self.cache_dir / f"bili_comment_feed_{oid}_{digest}.jpg"
        if out_path.is_file() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def render() -> Path:
            await self.canvas.render(out_path, document)
            return out_path

        return [
            ImageContent(
                asyncio.create_task(
                    render(),
                    name=f"bili_comment_canvas_{oid}",
                )
            )
        ]


__all__ = ["BiliCommentFeed"]
