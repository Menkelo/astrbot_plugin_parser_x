from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar

from astrbot.api import logger
from PIL import Image, ImageOps, UnidentifiedImageError

from .comment_settings import CommentFilterSettings
from .utils import IMAGE_ACCEPT, normalize_image_url


class RichPartLike(Protocol):
    kind: str
    text: str
    url: str


class CommentEntryLike(Protocol):
    content: list[Any]
    images: list[str]
    first_reply: Any | None


EntryT = TypeVar("EntryT", bound=CommentEntryLike)


@dataclass(frozen=True, slots=True)
class CommentFilterReport:
    kept: int
    mention: int = 0
    qrcode: int = 0
    ads: int = 0
    duplicate: int = 0
    low_information: int = 0
    qr_errors: int = 0


@dataclass(slots=True)
class _MutableReport:
    kept: int = 0
    mention: int = 0
    qrcode: int = 0
    ads: int = 0
    duplicate: int = 0
    low_information: int = 0
    qr_errors: int = 0

    def freeze(self) -> CommentFilterReport:
        return CommentFilterReport(
            kept=self.kept,
            mention=self.mention,
            qrcode=self.qrcode,
            ads=self.ads,
            duplicate=self.duplicate,
            low_information=self.low_information,
            qr_errors=self.qr_errors,
        )


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_SPACE_RE = re.compile(r"[ \t\f\v\u00a0\u3000]+")
_MENTION_RE = re.compile(r"(?<![\w@])[@＠][ \t]*[^\s@＠，。！？、,:：；;]{1,32}")
_STRUCTURAL_REPLY_RE = re.compile(
    r"^\s*回复\s*[@＠][ \t]*[^\s@＠，。！？、,:：；;]{1,32}\s*[:：]\s*"
)
_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)[^\s]+|"
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|cn|net|org|top|xyz|vip|shop|cc|me|io|app|link)\b"
)
_SHORT_LINK_RE = re.compile(
    r"(?i)\b(?:t\.cn|dwz\.cn|suo\.im|url\.cn|bit\.ly|tinyurl\.com|"
    r"cutt\.ly|reurl\.cc|xhslink\.com|v\.douyin\.com)/[^\s]*"
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d(?:[ -]?\d){8}(?!\d)")
_QQ_RE = re.compile(r"(?i)(?:qq|扣扣)\s*[:：号]?\s*[1-9]\d{4,11}\b")
_WECHAT_RE = re.compile(
    r"(?i)(?:微信|微\s*信|薇信|威信|v\s*x|v信|wx)\s*"
    r"(?:号|联系|添加|加)?\s*[:：]?\s*[a-z][-_a-z0-9]{4,19}\b"
)
_CONTACT_WORD_RE = re.compile(r"(?i)微信|微\s*信|薇信|v\s*x|v信|wx|qq|扣扣|手机号|电话")
_CONTACT_VALUE_RE = re.compile(r"(?i)[:：号]\s*[a-z0-9][-_a-z0-9]{4,19}\b")
_LEAD_RE = re.compile(
    r"加我|加微|加v|私聊|私信我|联系我|进群|入群|扫码|扫二维码|"
    r"看主页|主页见|主页有|点头像|戳头像|置顶动态|评论区置顶"
)
_MARKETING_RE = re.compile(
    r"代理|招募|招聘|兼职|副业|返利|返现|带单|放款|贷款|借款|博彩|"
    r"赌博|棋牌|刷单|刷粉|代购|代练|陪玩|资源群|交流群|内部群|课程群|"
    r"免费领取|免费送|优惠券|低价出|出售|售卖"
)
_PROMISE_RE = re.compile(
    r"日赚|月入|稳赚|包赚|保本|高收益|零风险|躺赚|秒到账|无门槛|"
    r"最低价|白菜价|限时优惠|名额有限"
)
_RISK_RE = re.compile(r"博彩|赌博|刷单|返利|放款|贷款|裸聊|色情|成人")
_CALL_RE = re.compile(
    r"^(?:快来|来看|来看看|围观|速看|看看|求助|有人吗|在吗|出来|快看|"
    r"看这个|康康|救命)(?:啊|呀|哦|吧|嘛|哈|！|!|。|\s)*$"
)
_TOPIC_RE = re.compile(r"#[^#\s]{1,40}#|#[^#\s]{1,40}(?=\s|$)")


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _ZERO_WIDTH_RE.sub("", text)
    text = "".join(
        char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc"
    )
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _clean_fragment(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _ZERO_WIDTH_RE.sub("", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cc")
    return _SPACE_RE.sub(" ", text)


def _plain_text(content: list[RichPartLike]) -> str:
    output: list[str] = []
    for part in content:
        if part.kind == "line-break":
            output.append("\n")
        else:
            output.append(str(part.text or ""))
    return "".join(output)


def _sanitize_content(content: list[RichPartLike]) -> None:
    output = []
    for part in content:
        if part.kind == "line-break":
            output.append(part)
            continue
        cleaned = _clean_fragment(part.text)
        if cleaned or part.kind == "emote" or part.url:
            output.append(replace(part, text=cleaned))
    while output and output[0].kind == "line-break":
        output.pop(0)
    while output and output[-1].kind == "line-break":
        output.pop()
    content[:] = output


def _meaningful_text(value: str) -> str:
    return "".join(
        char for char in value if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _dedupe_key(value: str) -> str:
    return _meaningful_text(unicodedata.normalize("NFKC", value).casefold())


def _only_low_information(value: str) -> bool:
    meaningful = _meaningful_text(value)
    return not meaningful or meaningful.isdigit()


def _is_spammy_text(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return False
    if _TOPIC_RE.findall(value) and len(_TOPIC_RE.findall(value)) >= 6:
        return True
    if re.search(r"(.)\1{11,}", compact, flags=re.S):
        return True
    if len(compact) >= 24 and len(set(compact)) <= 3:
        return True
    return False


def _ad_score(value: str, mention_count: int) -> tuple[int, bool]:
    has_url = bool(_URL_RE.search(value) or _SHORT_LINK_RE.search(value))
    has_phone = bool(_PHONE_RE.search(value))
    has_account = bool(_QQ_RE.search(value) or _WECHAT_RE.search(value))
    has_contact = bool(
        has_phone
        or has_account
        or (_CONTACT_WORD_RE.search(value) and _CONTACT_VALUE_RE.search(value))
    )
    has_lead = bool(_LEAD_RE.search(value))
    has_marketing = bool(_MARKETING_RE.search(value))
    has_promise = bool(_PROMISE_RE.search(value))
    has_risk = bool(_RISK_RE.search(value))
    without_contact = _PHONE_RE.sub("", value)
    without_contact = _QQ_RE.sub("", without_contact)
    without_contact = _WECHAT_RE.sub("", without_contact)
    pure_contact = has_contact and len(_meaningful_text(without_contact)) <= 4

    score = 0
    if has_url:
        score += 3
    if has_contact:
        score += 3
    if has_lead:
        score += 2
    if has_marketing:
        score += 1
    if has_promise:
        score += 1
    if mention_count >= 2:
        score += 2

    high_confidence = bool(
        pure_contact
        or (has_contact and has_lead)
        or (has_url and has_lead)
        or (has_risk and (has_contact or has_lead or has_url))
    )
    return score, high_confidence


def _clean_mentions(value: str, mode: str) -> tuple[str, int, bool]:
    value = _STRUCTURAL_REPLY_RE.sub("", value, count=1)
    matches = list(_MENTION_RE.finditer(value))
    count = len(matches)
    if not count or mode == "off":
        return value, count, False
    if mode == "strict":
        return "", count, True

    cleaned = _MENTION_RE.sub(" ", value)
    cleaned = re.sub(r"^[\s,，、:：;；.!！?？~～…·]+", "", cleaned)
    cleaned = _clean_text(cleaned)
    meaningful = _meaningful_text(cleaned)
    pure_call = bool(_CALL_RE.fullmatch(cleaned))
    should_drop = not meaningful or count >= 2 or pure_call
    return cleaned, count, should_drop


def _clean_content_mentions(content: list[RichPartLike]) -> None:
    output = []
    structural_pending = True
    for part in content:
        if part.kind == "line-break":
            output.append(part)
            continue
        if part.kind == "emote" or part.url:
            output.append(part)
            structural_pending = False
            continue

        text = str(part.text or "")
        if structural_pending:
            text = _STRUCTURAL_REPLY_RE.sub("", text, count=1)
        text = _MENTION_RE.sub(" ", text)
        text = _clean_fragment(text)
        if text.strip():
            output.append(replace(part, text=text))
            structural_pending = False

    while output and output[0].kind == "line-break":
        output.pop(0)
    while output and output[-1].kind == "line-break":
        output.pop()
    for index, part in enumerate(output):
        if part.kind == "line-break" or part.kind == "emote" or part.url:
            continue
        cleaned = re.sub(r"^[\s,，、:：;；.!！?？~～…·]+", "", part.text)
        if cleaned:
            output[index] = replace(part, text=cleaned)
        else:
            output.pop(index)
        break
    content[:] = output


class CommentFilter:
    def __init__(
        self,
        parser,
        settings: CommentFilterSettings,
        *,
        platform: str,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
    ):
        self.parser = parser
        self.settings = settings
        self.platform = platform
        self.headers = dict(headers or {})
        self.referer = referer
        self._qr_cache: dict[str, bool | None] = {}
        self._qr_sem = asyncio.Semaphore(3)

    async def _download_image(self, url: str) -> bytes | None:
        request_headers = dict(self.headers)
        request_headers["Accept"] = IMAGE_ACCEPT
        if self.referer:
            request_headers["Referer"] = self.referer
        response = await self.parser.http_get(
            url,
            headers=request_headers,
            allow_redirects=True,
            timeout=3,
            retries=1,
        )
        if response.status_code >= 400:
            return None
        content = response.content or b""
        if not content or len(content) > 4 * 1024 * 1024:
            return None
        return content

    @staticmethod
    def _detect_qrcode_sync(content: bytes) -> bool:
        import zxingcpp

        with Image.open(io.BytesIO(content)) as source:
            if source.width * source.height > 20_000_000:
                raise ValueError("comment image exceeds QR scan pixel limit")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            if max(image.size) > 1800:
                image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            results = zxingcpp.read_barcodes(
                image,
                formats=zxingcpp.BarcodeFormat.QRCode,
                try_rotate=True,
                try_downscale=True,
                try_invert=True,
            )
            return bool(results)

    async def _has_qrcode(self, image_url: str) -> bool | None:
        url = normalize_image_url(image_url)
        if not url:
            return None
        if url in self._qr_cache:
            return self._qr_cache[url]
        if len(self._qr_cache) >= 512:
            self._qr_cache.clear()

        result: bool | None = None
        try:
            async with self._qr_sem:
                content = await asyncio.wait_for(
                    self._download_image(url),
                    timeout=4,
                )
                if content is not None:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(self._detect_qrcode_sync, content),
                        timeout=3,
                    )
        except (ModuleNotFoundError, UnidentifiedImageError, OSError, ValueError):
            result = None
        except Exception:
            result = None
        self._qr_cache[url] = result
        return result

    def _filter_text(self, entry: EntryT) -> tuple[str | None, int]:
        original = _clean_text(_plain_text(entry.content))
        structural_reply = bool(_STRUCTURAL_REPLY_RE.match(original))
        cleaned = original
        cleaned, mention_count, mention_drop = _clean_mentions(
            cleaned,
            self.settings.mention_mode,
        )
        if mention_drop:
            return "mention", mention_count

        if self.settings.ads:
            score, high_confidence = _ad_score(original, mention_count)
            if high_confidence or score >= self.settings.ad_threshold:
                return "ads", mention_count
            if _is_spammy_text(cleaned):
                return "ads", mention_count

        if structural_reply and self.settings.mention_mode != "off":
            _clean_content_mentions(entry.content)
            cleaned = _clean_text(_plain_text(entry.content))
        elif self.settings.mention_mode == "balanced" and mention_count:
            _clean_content_mentions(entry.content)
            cleaned = _clean_text(_plain_text(entry.content))

        if (
            self.settings.low_information
            and not entry.images
            and _only_low_information(cleaned)
        ):
            return "low_information", mention_count

        if not entry.content and not entry.images:
            return "low_information", mention_count
        return None, mention_count

    async def _filter_entry(
        self, entry: EntryT
    ) -> tuple[EntryT | None, str | None, int]:
        _sanitize_content(entry.content)
        reason, mention_count = self._filter_text(entry)
        if reason is not None:
            return None, reason, 0

        if self.settings.qrcode and entry.images:
            results = await asyncio.gather(
                *(self._has_qrcode(url) for url in entry.images[:3])
            )
            if any(result is True for result in results):
                return None, "qrcode", 0
            qr_errors = sum(result is None for result in results)
        else:
            qr_errors = 0

        if entry.first_reply is not None:
            reply, _, _ = await self._filter_entry(entry.first_reply)
            entry.first_reply = reply
        return entry, None, qr_errors

    async def apply(self, entries: list[EntryT], *, limit: int) -> list[EntryT]:
        if not self.settings.enabled:
            return entries[:limit]

        output: list[EntryT] = []
        seen: set[str] = set()
        report = _MutableReport()
        for entry in entries:
            filtered, reason, qr_errors = await self._filter_entry(entry)
            report.qr_errors += qr_errors
            if filtered is None:
                if reason:
                    setattr(report, reason, getattr(report, reason) + 1)
                continue

            key = _dedupe_key(_clean_text(_plain_text(filtered.content)))
            if not key and filtered.images:
                key = "|".join(
                    value
                    for value in (normalize_image_url(url) for url in filtered.images)
                    if value
                )
            if self.settings.duplicates and key and key in seen:
                report.duplicate += 1
                continue
            if key:
                seen.add(key)
            output.append(filtered)
            if len(output) >= max(1, int(limit)):
                break

        report.kept = len(output)
        frozen = report.freeze()
        counts = Counter(
            {
                "mention": frozen.mention,
                "qrcode": frozen.qrcode,
                "ads": frozen.ads,
                "duplicate": frozen.duplicate,
                "low_information": frozen.low_information,
                "qr_errors": frozen.qr_errors,
            }
        )
        logger.debug(
            f"[评论区][{self.platform}] stage=filter result="
            f"{'ok' if output else 'filtered_empty'} kept={frozen.kept} "
            + " ".join(f"{key}={value}" for key, value in counts.items())
        )
        return output


__all__ = ["CommentFilter", "CommentFilterReport"]
