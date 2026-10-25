"""小黑盒（Heybox）社区帖子解析。

小黑盒网页端是纯 SPA，帖子正文只能通过 ``api.xiaoheihe.cn`` 获取，而该接口
需要 web 端签名与 SM 风控指纹，因此这里用纯 HTTP 完成，不需要浏览器。
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, ClassVar

import msgspec
from astrbot.api import logger

from ...comment_settings import CommentSettings
from ...data import (
    Author,
    ImageContent,
    MediaContent,
    ParseResult,
    Platform,
    VideoContent,
)
from ...download import Downloader
from ...html_renderer import HtmlRenderService
from ...card_flow import build_card_extra
from ..base import BaseParser, ParseException, handle
from ..xiaoheihe_comment import XiaoHeiHeCommentFeed

API_PATH: ClassVar[str] = "/bbs/app/link/tree"
API_HOST: ClassVar[str] = "api.xiaoheihe.cn"

# 富文本里的表情标记，如 [cube_鳄鱼]
_EMOJI_RE = re.compile(r"\[[a-zA-Z]+_[^\]]+\]")
# 帖子 text 字段里的图片/视频片段
_LINK_ID_RE = re.compile(r"(?:bbs/link|link)/(\d+)")

# 正文超过这个字数就当长文处理（不渲染，只回一句提示）。
# 实测 262 字的图帖要放行、1058 字以上的文章要拦，800 是两者中间的值。
_ARTICLE_MIN_CHARS = 800

# 表情包接口：返回 4 组共 161 个表情，token 形如 ``[cube_鳄鱼]``
_EMOJI_API_PATH = "/bbs/app/api/emojis/list"

# 正文里的表情 token，如 [cube_鳄鱼]
_EMOTE_TOKEN_RE = re.compile(r"\[([a-zA-Z]+)_([^\[\]\n]{1,32})\]")

# 正文卡片版式标识，交给 TextCardRenderer 选独立样式
_CARD_LAYOUT = "xiaoheihe"

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaoheihe.cn/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.xiaoheihe.cn",
}


class _Segment(msgspec.Struct, kw_only=True):
    """帖子正文的富文本片段。"""

    type: str = "text"
    text: str = ""
    url: str = ""
    width: Any = None
    height: Any = None


class _Chunk:
    """HTML 正文里切出来的一段：文字 + 媒体类型信息。"""

    __slots__ = ("text", "had_media", "has_picture", "images")

    def __init__(
        self,
        text: str,
        had_media: bool,
        has_picture: bool,
        images: list[str] | None = None,
    ) -> None:
        self.text = text
        self.had_media = had_media
        # 段内是否有真实截图（区别于 mention-game 游戏卡占位）
        self.has_picture = has_picture
        self.images = images or []


class _HtmlBodyParser(HTMLParser):
    """把长文帖 ``type == "html"`` 的正文拆成文字与图片。

    小黑盒长文（``is_article == 1``）会把正文存成一段 HTML，里面用
    ``<p>`` / ``<h*>>`` / ``<blockquote>`` / ``<ul><li>`` 排版，图片是
    ``<img data-original="...">``。另有一批 ``<img class="mention-game">``
    只是游戏卡片的占位符，本身没有图片地址，必须丢弃，否则会渲染成一段
    空条目。
    """

    # 遇到这些标签就切一段，避免整篇挤成一行
    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "ul",
            "ol",
            "li",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "tr",
        }
    )

    def __init__(self) -> None:
        # convert_charrefs=False：小黑盒作者手写的 "<10.21>" 这类不合法标签
        # 在 True 时会被整段吞掉，这里自己处理字符引用
        super().__init__(convert_charrefs=False)
        self.chunks: list[_Chunk] = []
        self.images: list[str] = []
        self._buf: list[str] = []
        self._li_depth = 0
        # 当前这段是否只放了游戏卡片/图片（没有文字）
        self._had_media = False
        self._has_picture = False
        self._chunk_images: list[str] = []

    # -- 内部 ---------------------------------------------------------
    def _flush(self) -> None:
        chunk = "".join(self._buf)
        self._buf.clear()
        chunk = re.sub(r"[ \t]*\n[ \t]*", "\n", chunk)
        chunk = chunk.strip()
        if chunk or self._had_media:
            self.chunks.append(
                _Chunk(chunk, self._had_media, self._has_picture, self._chunk_images)
            )
        self._had_media = False
        self._has_picture = False
        self._chunk_images = []

    # -- HTMLParser 回调 ----------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "img":
            attr = {key: (value or "") for key, value in attrs}
            src = attr.get("data-original") or attr.get("src") or ""
            classes = attr.get("class") or ""
            if "mention-game" in classes:
                # 游戏卡片占位符：本身没有图片地址，标记本段为“媒体块”
                self._had_media = True
                self._flush()
                return
            style = {}
            for declaration in attr.get("style", "").lower().split(";"):
                key, separator, value = declaration.partition(":")
                if separator:
                    style[key.strip()] = value.replace("!important", "").strip()
            hidden = (
                "hidden" in attr
                or attr.get("aria-hidden", "").lower() == "true"
                or style.get("display") == "none"
                or style.get("visibility") == "hidden"
            )
            # 只排除明确的 1×1 像素，不以“小图”阈值误伤正文表情包。
            tracking_pixel = all(
                re.fullmatch(r"1(?:\.0+)?(?:px)?", attr.get(axis) or style.get(axis, ""))
                for axis in ("width", "height")
            )
            if hidden or tracking_pixel:
                return
            class_tokens = set(classes.lower().split())
            is_emoji = any(
                token in {"emoji", "emoticon"}
                or token.startswith(("emoji-", "emoticon-"))
                for token in class_tokens
            )
            is_ui = bool(class_tokens & {"icon", "ui-icon", "logo", "avatar"}) or any(
                token.startswith("icon-") for token in class_tokens
            )
            if is_emoji or is_ui:
                # 行内表情保留可读文字，但不能升级为正文图片/封面。
                if is_emoji:
                    self._buf.append(attr.get("alt", ""))
                return
            self._flush()
            if src.startswith(("http://", "https://", "//")):
                self.images.append(src)
                self._chunk_images.append(src)
                self._had_media = True
                self._has_picture = True
            return
        if tag == "br":
            self._buf.append("\n")
            return
        if tag == "li":
            self._flush()
            self._li_depth += 1
            self._buf.append("· ")
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str):
        if tag == "li":
            self._flush()
            if self._li_depth > 0:
                self._li_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str):
        if data:
            self._buf.append(html_module.unescape(data))

    def handle_entityref(self, name: str):
        # 具名实体（&amp; &lt; ...）
        if name == "nbsp":
            self._buf.append(" ")
        else:
            self._buf.append(html_module.unescape(f"&{name};"))

    def handle_charref(self, name: str):
        self._buf.append(html_module.unescape(f"&#{name};"))

    def close(self) -> None:
        super().close()
        self._flush()

    # -- 对外 ---------------------------------------------------------
    def flow(self) -> list[tuple[str, str]]:
        """按原始顺序产出 ``("text", 文字)`` / ``("image", 图片地址)``。"""
        output: list[tuple[str, str]] = []
        chunks = self.chunks
        for index, chunk in enumerate(chunks):
            if chunk.text:
                # "第N款" 后面只有游戏卡、没有任何截图的，属于空条目
                if not (_COUNTER_RE.match(chunk.text) and _is_empty_game_entry(chunks, index)):
                    output.append(("text", chunk.text))
            for image_url in chunk.images:
                output.append(("image", image_url))
        return output

    def result(self) -> tuple[str, list[str]]:
        merged: list[str] = []
        pending = ""
        chunks = self.chunks
        for index, chunk in enumerate(chunks):
            text = chunk.text
            if not text:
                continue
            # 孤立的前缀（如 "·"）并入下一段，避免单独占一行
            if text in {"·", "-", "•"}:
                pending = "·"
                continue
            # "第N款" 小标题后面紧跟一个只有游戏卡的段落，且该卡片没有
            # 任何真实截图（下一段不是图片）-> 空条目，整条丢弃
            if _COUNTER_RE.match(text) and _is_empty_game_entry(chunks, index):
                continue
            merged.append(f"{pending} {text}".strip() if pending else text)
            pending = ""
        if pending:
            merged.append(pending)
        text = "\n".join(merged)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, self.images


_COUNTER_RE = re.compile(r"^第\s*\d+\s*[款个条项]$")


def _is_empty_game_entry(chunks: list[_Chunk], index: int) -> bool:
    """判断 "第N款" 这类小标题是否对应一条没有内容的空条目。

    小黑盒长文的游戏条目布局是::

        <p>第1款</p>
        <p><img class="mention-game" data-gameid="..."/></p>   # 游戏卡占位
        <p><img data-original="..."/></p>                     # 截图（可能没有）

    实测一篇 88 款的长文里，只有前 3 款带了截图，剩下 85 款全是
    "第N款 + 空游戏卡 + 第N+1款 ..." 的死循环。判据是只看当前条目自己
    这一段（到下一个条目标题或普通文字为止）：段内有真实截图就保留，
    否则就是空条目。
    """
    if index + 1 >= len(chunks):
        return False
    following = chunks[index + 1]
    # 紧跟的必须是纯媒体块（游戏卡占位），否则这个标题是有内容的
    if not (following.had_media and not following.text):
        return False

    cursor = index + 2
    while cursor < len(chunks):
        chunk = chunks[cursor]
        if chunk.has_picture:
            # 本段出现真实截图 -> 有内容
            return False
        if not chunk.text:
            cursor += 1
            continue
        # 后面紧跟的又是另一个条目标题 -> 本条到此为止，且没有内容
        if _COUNTER_RE.match(chunk.text):
            return True
        # 普通说明文字 -> 有内容
        return False
    return True


def _plain_text_length(link: dict[str, Any]) -> int:
    """粗略统计正文长度：所有分段文本去掉 HTML 标签和表情 token 后计字数。"""
    raw = link.get("text") or ""
    try:
        segments = json.loads(raw)
    except (ValueError, TypeError):
        return len(str(raw))
    if not isinstance(segments, list):
        return 0
    total = 0
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        chunk = str(seg.get("text") or "")
        chunk = re.sub(r"<[^>]+>", "", chunk)
        chunk = _EMOTE_TOKEN_RE.sub("", chunk)
        total += len(chunk.strip())
    return total


def _html_to_flow(html: str) -> list[tuple[str, str]]:
    """把长文 HTML 按原始顺序拆成 ``("text", ...)`` / ``("image", ...)``。

    小黑盒长文经常是 "一句话一张表情包" 的排布，图片必须留在原位，不能
    全部挪到正文末尾。
    """
    parser = _HtmlBodyParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - 畸形 HTML 兜底
        logger.debug("小黑盒 HTML 正文解析失败", exc_info=True)
        fallback = re.sub(r"<[^>]+>", "\n", html)
        fallback = re.sub(r"\n{2,}", "\n", fallback).strip()
        return [("text", fallback)] if fallback else []
    return parser.flow()


def _html_to_body(html: str) -> tuple[str, list[str]]:
    """把长文 HTML 转成 (纯文本, 图片 URL 列表)。"""
    parser = _HtmlBodyParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - 畸形 HTML 兜底
        logger.debug("小黑盒 HTML 正文解析失败", exc_info=True)
        fallback = re.sub(r"<[^>]+>", "\n", html)
        return re.sub(r"\n{2,}", "\n", fallback).strip(), []
    return parser.result()


def _image_key(url: str) -> str:
    """图片去重键：同一张图在 HTML 段与 img 段里路径前缀不同，
    取 ``日期/文件名`` 两段作为稳定标识。"""
    path = re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return path


def _cover_url(raw: object) -> str:
    """把 ``link.thumb`` 规整成封面原图地址。

    平台给的地址尾巴上挂着七牛的处理参数，
    ``thumbnail/1125x600>`` 会先裁再缩，竖着装横图，
    放到卡片的 hero 区会被二次裁切。这里去掉缩放参数只保留原图，
    让封面按它本来的比例显示。
    """
    url = str(raw or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    # 去掉末尾的缩放段。七牛的参数是 /thumbnail/WxH>，其中 ">" 在 JSON 里
    # 可能被转义成 %3E，两种写法都要认。
    url = re.sub(r"/thumbnail/\d+x\d+(?:>|%3E)?$", "", url)
    return url.rstrip("?&")


class XiaoHeiHeParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaoheihe", display_name="小黑盒")

    def __init__(self, config: Any, downloader: Downloader) -> None:
        super().__init__(config, downloader)
        self.headers.update(_HEADERS)

        # 小黑盒接口有风控，未登录时大概率返回 show_captcha，需填写 Cookie。
        cookies = config.get("cookies", {}) if hasattr(config, "get") else {}
        self.cookie = (
            str(cookies.get("xiaoheihe_cookie", "")) if isinstance(cookies, dict) else ""
        ).strip()
        if self.cookie:
            self.headers["Cookie"] = self.cookie

        # 评论区：复用统一评论画布，由正文卡片负责把评论区拼在下方。
        self.render_service = HtmlRenderService.from_config(config)
        comment_settings = CommentSettings.from_config(config, "xiaoheihe")
        self.comment_settings = comment_settings
        self.comment_limit = comment_settings.display_count
        self.comment_timeout = comment_settings.timeout
        self.comment_feed = XiaoHeiHeCommentFeed(
            self,
            limit=self.comment_limit,
        )

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service
    # 小黑盒有三种分享形态：
    #   1. www.xiaoheihe.cn/app/bbs/link/{数字}          —— 网页端帖子地址
    #   2. api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id={十六进制}  —— App 分享
    #   3. 同上，link_id 是十进制数字
    # 第 2/3 种以前不匹配，直接被当成普通文本放过去了。
    @handle("xiaoheihe", r"https?://(?:www\.)?xiaoheihe\.cn/app/bbs/link/(\d+)")
    @handle(
        "xiaoheihe",
        r"https?://api\.xiaoheihe\.cn/v3/bbs/app/api/web/share\?"
        r"(?:[^\s&#]*&)*link_id=([0-9a-fA-F]+)(?=&|#|\s|$)",
    )
    async def parse_post(self, searched: re.Match[str]) -> ParseResult:
        link_id = searched.group(1)
        payload = await self._fetch_post(link_id)
        if self._is_long_article(payload):
            # 长文帖（小黑盒自己标成"文章"的资讯/攻略/评测）动辄几千上万字，
            # 渲染成图片能有好几千像素高，刷屏且没人看得完，只回一句提示。
            # 走 ParseException 让它发到群里，但文案里不带 ⚠️ 之类的符号。
            raise ParseException("这是小黑盒长文帖，请点链接查看")
        return await self._build_result(link_id, payload)

    @staticmethod
    def _format_date(raw: object) -> str:
        """把 ``create_at``（Unix 秒）格式化成 ``MM-DD``。

        小黑盒的署名行习惯只显示月日，年份对帖子来说没什么意义。
        """
        try:
            ts = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ""
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts).strftime("%m-%d")

    @staticmethod
    def _is_long_article(link: dict[str, Any]) -> bool:
        """判断是不是小黑盒的"文章"型长帖。

        ``link_tag`` 是平台给帖子的类型标记：实测刷 50 条动态，
        ``27`` 是普通水贴、``1`` 是中等图文、``11`` 才是文章型长文
        （攻略、资讯、评测，正文长度普遍几千到几万字符）。
        """
        # 判据：正文很长 且 没有头图。
        # 实测数据（图数 / 正文字数 / 有无 thumb）：
        #   193973610  8 / 262  / 有   → 短，放行
        #   193744786  1 / 134  / 无   → 短，放行（无头图但有正文图）
        #   194227447  2 / 338  / 无   → 短，放行
        #   194561740  6 / 1058 / 无   → 长且无头图，拦
        #   193958995 23 / 1585 / 无   → 长且无头图，拦
        # 只看 head 会把 193744786 这类长图帖误拦；只看字数则 1058 字的
        # 评测文章和 262 字的图帖混在一起，两个条件组合才分得开。
        has_cover = bool(_cover_url(link.get("thumb")))
        return (not has_cover) and _plain_text_length(link) >= _ARTICLE_MIN_CHARS

    # -------------------------------------------------------------- 请求层
    def _build_url(self, link_id: str) -> str:
        from .sign import get_hkey, get_nonce

        ts = int(time.time())
        return (
            f"https://{API_HOST}{API_PATH}"
            "?os_type=web&app=heybox&client_type=web&version=999.0.4"
            f"&_time={ts}&nonce={get_nonce(ts)}&hkey={get_hkey(API_PATH, ts)}"
            f"&link_id={link_id}"
            "&page=1&index=1&limit=5&x_client_type=weboutapp"
            "&x_app=heybox_website&x_os_type=Windows&web_version=2.5"
        )

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service

    def _comment_document_factory(
        self,
        link_id: str,
        *,
        work_title: str,
        cover: str | None,
        emotes: dict[str, str] | None,
    ):
        """返回一个可 await 的工厂，供正文卡片在渲染时把评论区拼在下方。"""

        async def build():
            tree = await self.comment_feed.fetch_tree(link_id)
            if not tree:
                return None
            return await self.comment_feed.build_document(
                tree,
                work_title=work_title,
                cover=cover,
                emotes=emotes,
            )

        return build

    def _comment_image_factory(
        self,
        link_id: str,
        *,
        work_title: str,
        cover: str | None,
        emotes: dict[str, str] | None,
    ):
        """返回一个可 await 的工厂，产出已渲染好的评论图（视频帖用）。

        视频帖不把评论拼进正文卡，而是像其它平台一样单独出一张评论图。
        """

        async def build():
            tree = await self.comment_feed.fetch_tree(link_id)
            if not tree:
                return []
            document = await self.comment_feed.build_document(
                tree,
                work_title=work_title,
                cover=cover,
                emotes=emotes,
            )
            if document is None:
                return []
            return await self.comment_feed.render_images(
                document,
                link_id=link_id,
            )

        return build

    def build_comment_url(self, link_id: str, *, limit: int = 10) -> str:
        """评论接口与正文接口同路径，参数不同，签名规则一致。"""
        from .sign import get_hkey, get_nonce

        ts = int(time.time())
        return (
            f"https://{API_HOST}{API_PATH}"
            "?os_type=web&app=heybox&client_type=web&version=999.0.4"
            f"&_time={ts}&nonce={get_nonce(ts)}&hkey={get_hkey(API_PATH, ts)}"
            f"&link_id={link_id}&page=1&index=1&limit={max(1, int(limit))}"
            "&x_client_type=weboutapp&x_app=heybox_website"
            "&x_os_type=Windows&web_version=2.5"
        )

    @classmethod
    def _build_signed_url(cls, path: str) -> str:
        """按 web 端签名规则拼任意接口地址，供表情包等附属接口复用。"""
        from .sign import get_hkey, get_nonce

        ts = int(time.time())
        return (
            f"https://{API_HOST}{path}"
            "?os_type=web&app=heybox&client_type=web&version=999.0.4"
            f"&_time={ts}&nonce={get_nonce(ts)}&hkey={get_hkey(path, ts)}"
            "&x_client_type=weboutapp&x_app=heybox_website"
            "&x_os_type=Windows&web_version=2.5"
        )

    async def _fetch_emote_catalog(self) -> dict[str, str]:
        """拉取小黑盒表情包目录，返回 ``{"[cube_鳄鱼]": 图片地址}``。

        正文里的表情就是这种方括号 token，不转成图片就会以原始文本漏出来。
        接口失败不影响正文解析，直接返回空表。
        """
        try:
            resp = await self.http_get(
                self._build_signed_url(_EMOJI_API_PATH),
                headers=self.headers,
                retries=1,
            )
            data = resp.json() or {}
        except Exception:
            return {}
        if data.get("status") != "ok":
            return {}
        catalog: dict[str, str] = {}
        for group in (data.get("result") or {}).get("emoji_groups") or []:
            group_code = str(group.get("group_code") or "")
            for emoji in group.get("emojis") or []:
                code = str(emoji.get("code") or "")
                img = str(emoji.get("img") or "")
                if group_code and code and img:
                    catalog[f"[{group_code}_{code}]"] = img
        return catalog

    async def _fetch_device_id(self) -> str | None:
        """申请 SM 指纹 deviceId；失败不致命，部分帖文无它也可读。"""
        from .sm import SM_HOST, build_payload

        try:
            resp = await self.client.post(
                f"https://{SM_HOST}/deviceprofile/v4",
                headers={"Content-Type": "application/json"},
                json=build_payload(),
                timeout=15,
            )
            detail = (resp.json() or {}).get("detail") or {}
            device_id = detail.get("deviceId")
            if device_id:
                logger.debug("[小黑盒] 已获取 deviceId")
            return device_id
        except Exception as e:
            logger.debug(f"[小黑盒] 获取 deviceId 失败: {e}")
            return None

    async def _fetch_post(self, link_id: str) -> dict[str, Any]:
        url = self._build_url(link_id)
        resp = await self.http_get(url, headers=self.headers, retries=1)
        try:
            data = resp.json() or {}
        except Exception as e:
            raise ParseException(f"小黑盒接口返回非 JSON: {e}") from e

        status = data.get("status")
        if status == "ok":
            return (data.get("result") or {}).get("link") or {}

        msg = (data.get("msg") or "").strip()

        # 命中风控：补发指纹再试一次
        if status == "show_captcha":
            device_id = await self._fetch_device_id()
            if device_id:
                headers = dict(self.headers)
                headers["x-device-id"] = device_id
                resp = await self.http_get(
                    self._build_url(link_id), headers=headers, retries=1
                )
                data = resp.json() or {}
                if data.get("status") == "ok":
                    return (data.get("result") or {}).get("link") or {}
                msg = (data.get("msg") or "").strip()

        if data.get("status") == "show_captcha":
            raise ParseException("请确认链接仍可访问、Cookie 配置有效，然后重试。")
        if msg:
            raise ParseException(f"小黑盒解析失败: {msg}")
        raise ParseException(f"小黑盒解析失败: 未知状态 {status}")

    # -------------------------------------------------------------- 结果层
    async def _build_result(
        self, link_id: str, link: dict[str, Any]
    ) -> ParseResult:
        user = link.get("user") or {}
        name = user.get("username") or "小黑盒用户"
        avatar_url = user.get("avatar")
        avatar = (
            self.downloader.download_img(avatar_url, ext_headers=self.headers)
            if avatar_url
            else None
        )

        # 一次请求拿到全量表情目录，没有它正文里的 [cube_鳄鱼] 会漏成原始文本
        emotes = await self._fetch_emote_catalog()
        text, contents, flow, cover = self._parse_body(link)

        # 视频帖：正文里没有片段时，从 video 字段补
        if not any(isinstance(c, VideoContent) for c in contents):
            contents.extend(await self._video_contents(link))

        extra: dict[str, Any] = {
            # 小黑盒走独立的图文流版式（封面大图 + 紧排正文），
            # 不和 B 站/微博共用的 .content-flow 样式混在一起。
            "text_card_layout": _CARD_LAYOUT,
        }
        # 作者头像：卡片头部用真实头像，没有就退回首字母占位
        if avatar_url:
            extra["text_card_avatar"] = avatar_url
        # 署名行只显示发布日期（属地已按统一规范从所有平台卡片中移除）
        published = self._format_date(link.get("create_at"))
        if published:
            extra["text_card_meta"] = published
        # 表情目录交给卡片渲染层，由它把 token 换成表情图
        if emotes:
            extra["card_emotes"] = emotes
        # 图文按原始顺序排布（一句话一张表情包）；头图/拆图判定统一走
        # ``build_card_extra``，和其它平台共一份逻辑。
        extra = build_card_extra(
            flow,
            cover=cover,
            is_video=bool(link.get("has_video")),
            extra=extra,
        )
        if text:
            extra.setdefault("render_text_card", True)
        # 评论区拼接在正文卡片下方，与卡片合成同一张图（不走单独发图路径）。
        # 视频帖例外：它走 `comment_image_task_factory` 单独发评论图（和其它
        # 平台视频帖一致），卡片里再拼一次就会在群里出现两份评论区。
        if self.comment_settings.enabled:
            comment_title = (link.get("title") or "").strip()
            extra["text_card_comment_factory"] = self._comment_document_factory(
                link_id,
                work_title=comment_title,
                cover=cover,
                emotes=emotes,
            )
            extra["comment_timeout"] = 90

        return ParseResult(
            platform=self.platform,
            author=Author(name=name, avatar=avatar),
            title=(link.get("title") or "").strip() or None,
            text=text or None,
            timestamp=link.get("create_at"),
            # share_url 是 api.xiaoheihe.cn 的跳转链接，展示用原始帖子地址更合适
            url=f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}",
            contents=contents,
            extra=extra,
        )

    def _parse_body(
        self, link: dict[str, Any]
    ) -> tuple[str, list[MediaContent], list[tuple[str, str]], str]:
        """解析正文。

        返回 ``(纯文本, 媒体列表, 图文顺序块, 封面地址)``。第三项按帖子原始
        顺序记录 ``("text", 文字)`` / ``("image", 图片地址)``，长文帖里一句
        文字接一张表情包的那种排布全靠它还原，否则图片只能全部堆在正文末尾。
        第四项是 ``link.thumb`` 的头图，单独交给卡片 hero 区，不进正文流。
        """
        raw = link.get("text") or ""
        segments: list[_Segment] = []
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    segments = [_Segment(**item) for item in parsed if item]
            except (ValueError, TypeError):
                # 纯文本帖子
                segments = [_Segment(type="text", text=raw)]

        texts: list[str] = []
        contents: list[MediaContent] = []
        flow: list[tuple[str, str]] = []
        seen_images: set[str] = set()

        def add_image(url: str) -> None:
            key = _image_key(url)
            if key in seen_images:
                return
            seen_images.add(key)
            contents.append(
                ImageContent(self.downloader.download_img(url, ext_headers=self.headers))
            )
            flow.append(("image", url))

        def add_text(value: str) -> None:
            chunk = value.strip()
            if not chunk:
                return
            texts.append(chunk)
            flow.append(("text", chunk))

        # 视频帖的封面在 video_thumb 上，thumb 是平台给的裁切缩略图，优先用前者；
        # 但竖屏视频封面本身是竖图，交给 hero 区按 contain 显示即可。
        cover = _cover_url(link.get("thumb")) or _cover_url(link.get("video_thumb"))
        # 视频帖的 thumb 是平台按 1125x600 裁出来的横图，塞进正文会和
        # video_thumb 封面重复，也不适合竖屏视频，所以跳过。
        if cover and not link.get("has_video"):
            contents.insert(
                0,
                ImageContent(self.downloader.download_img(cover, ext_headers=self.headers)),
            )

        for seg in segments:
            if seg.type == "img" and seg.url:
                add_image(seg.url)
            elif seg.type == "video" and seg.url:
                # One source URL, no per-codec alternatives: keep the original
                # signed URL for every codec preference (no probing/transcoding).
                contents.append(
                    VideoContent(
                        self.downloader.download_video(
                            seg.url,
                            video_name=f"xiaoheihe_{link.get('linkid')}_{len(contents)}.mp4",
                            ext_headers=self.headers,
                        )
                    )
                )
            elif seg.type == "html" and seg.text:
                # 长文帖子：按段落顺序把文字和正文图交替还原
                for kind, value in _html_to_flow(seg.text):
                    if kind == "image":
                        add_image(value)
                    else:
                        add_text(value)
            elif seg.text:
                add_text(seg.text)

        # 表情 token 原样保留：渲染层拿到 card_emotes 目录后自己换成表情图，
        # 在这里拼 <img> 会被它当普通文本转义掉。
        text = "\n".join(texts)
        return text.strip(), contents, flow, cover

    async def _video_contents(self, link: dict[str, Any]) -> list[MediaContent]:
        """视频帖的视频本体。

        小黑盒把视频信息平铺在 link 顶层（实测字段）：
        ``video_url`` 直链 mp4（带 auth/sign 时效参数，无需 Cookie 也能下）、
        ``video_thumb`` 封面、``video_duration`` 秒、``video_width/height`` 尺寸。
        没有嵌套的 ``video`` 对象，早期按 ``link["video"]`` 取会永远落空。
        This API exposes one signed source, not codec-labelled representations;
        auto/avc/hevc therefore all honestly retain that URL without extra I/O.
        """
        url = link.get("video_url")
        if not url:
            return []
        cover = link.get("video_thumb") or link.get("thumb")
        cover_task = (
            self.downloader.download_img(cover, ext_headers=self.headers)
            if cover
            else None
        )
        return [
            VideoContent(
                self.downloader.download_video(
                    url,
                    video_name=f"xiaoheihe_{link.get('linkid')}.mp4",
                    ext_headers=self.headers,
                ),
                cover=cover_task,
                duration=float(link.get("video_duration") or 0),
            )
        ]

__all__ = ["XiaoHeiHeParser"]
