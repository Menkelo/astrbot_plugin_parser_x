"""Shared HTML rendering core for domestic social comment canvases.

All platforms (Bilibili, Douyin, Weibo) render through this single minimal
template: avatar, nickname, body, images and an optional pinned marker. IP
location, timestamps and highlighted (accent coloured) inline text are
deliberately not rendered; body text uses one single colour. The entry
dataclasses keep their fields so parsers stay unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import COMMENT_FOOTER_BRAND
from .html_renderer import HtmlRenderService

_FALLBACK_AVATAR_CHAR = "评"

# 评论图底部署名（与正文卡页脚保持同一品牌）。
_FOOTER_SIGNATURE = COMMENT_FOOTER_BRAND

# Palette for avatar placeholders when no image is available.
_AVATAR_COLORS = (
    "#5b8ff9",
    "#f77234",
    "#14c9c9",
    "#8e7cc3",
    "#f7ba1e",
    "#e8684a",
)

_CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#000000;font-family:'Noto Sans SC','Noto Sans CJK SC','WenQuanYi Zen Hei','Noto Color Emoji',sans-serif;color:#f2f3f5;font-size:30px;-webkit-font-smoothing:antialiased;text-rendering:geometricPrecision}
html{overflow-x:hidden;scrollbar-width:none}
html::-webkit-scrollbar{width:0;height:0}
.footer{padding:26px 0 30px;text-align:center}
.footer-rule{height:2px;margin:0 0 18px;background:#1e1e1e}
.footer-brand{color:#8a8a8a;font-size:26px;letter-spacing:.5px}
.page{width:900px;padding:0}
.shell{width:900px;margin:0;background:#000000;border-radius:0}
.comment-list{padding:0}
.comment-card{display:flex;gap:20px;padding:28px 32px;border-bottom:1px solid #23252b}
.comment-card:last-child{border-bottom:none}
.avatar-shell{position:relative;flex:0 0 84px;width:84px;height:84px;border-radius:50%;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#fff;font-size:34px;font-weight:600}
.avatar-shell img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.comment-body{min-width:0;flex:1}
.nickname{display:flex;align-items:center;gap:10px;font-size:32px;font-weight:600;color:#f2f3f5}
.nickname-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.comment-content{margin-top:14px;font-size:34px;line-height:1.6;color:#f2f3f5;word-break:break-word}
.highlight{color:#7f8cff}
.emoji-text{display:inline-block;margin:0 .15em;padding:0 .32em;border-radius:.32em;background:#262a3d;color:#c9cdd4;font-size:.85em;line-height:1.35;font-family:"Noto Color Emoji","Apple Color Emoji","Segoe UI Emoji","Noto Emoji",sans-serif;font-variant-emoji:emoji}
.emote{display:inline-block;height:1.15em;width:auto;max-width:3em;margin:0 .1em;object-fit:contain;vertical-align:-.2em;}
.comment-image-wrap,.sticker-image-wrap{display:block;width:fit-content;max-width:100%;margin-top:16px;overflow:hidden;border-radius:16px;background:#202228}
.comment-image{display:block;width:auto;height:auto;max-width:640px;object-fit:contain}
.sticker-image{display:block;width:auto;height:auto;max-width:320px;max-height:320px;object-fit:contain}
.pinned,.author{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;height:1.55em;margin-left:5px;padding:0 .42em;border-radius:.3em;font-size:.72em;font-weight:600;line-height:1;white-space:nowrap}
.pinned{background:#262a3d;color:#c9cdd4}
.author{background:#3a2b16;color:#e8b45f}
.reply-card{display:flex;gap:18px;margin-top:18px;padding:18px 22px;background:#202228;border-radius:14px}
.reply-card .avatar-shell{flex:0 0 64px;width:64px;height:64px;font-size:28px}
.reply-card .comment-body{min-width:0;flex:1}
.reply-card .nickname{font-size:29px;line-height:1.6}
.reply-card .comment-content{margin-top:9px;font-size:31px}
"""


#: 主题里只带平台显示名时，用它换回平台 key（表情来源表按 key 存）。
#: 评论区主题名 -> 平台 key。缺一个平台，它的表情 token 就永远解析不出来，
#: 只会原样显示 `[s:ac:汗]` 这种源码，所以新增平台必须在这里登记。
_PLATFORM_KEY_BY_NAME = {
    "B站": "bilibili",
    "小红书": "xiaohongshu",
    "酷安": "coolapk",
    "NGA": "nga",
    "网易云音乐": "netease",
}


class CommentCanvasBase:
    """Single minimal comment feed renderer shared by all platforms."""

    def __init__(self, render_service: HtmlRenderService | None = None):
        self.render_service = render_service or HtmlRenderService()
        # 运行时表情解析器（由插件注入）；为空时表情直接回落到 [表情名] 文本。
        self.emote_resolver: object | None = None
        # 每次 build_html 前由插件填充：平台 key -> 待解析表情名集合。
        self._pending_emotes: dict[str, set[str]] = {}
        # 本次渲染已解析好、直接可用的表情地址（token -> data URI）。
        self._emote_urls: dict[str, str] = {}
        # 本次渲染所属平台 key，用于判断哪些 token 值得去联网解析。
        self._emote_platform: str = ""

    # ------------------------------------------------------------------
    # Escaping
    # ------------------------------------------------------------------
    @staticmethod
    def _escape_jinja(value: str) -> str:
        # 历史上渲染器把这份 HTML 当作 Jinja 模板，所以转义花括号防止评论内容如 ``{{ ... }}`` 被当成模板代码（当前本地渲染已不解析 Jinja，转义仍然保留）。
        return value.replace("{", "&#123;").replace("}", "&#125;")

    @classmethod
    def _text(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or "")))

    @classmethod
    def _url(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or ""), quote=True))

    # ------------------------------------------------------------------
    # Fragments
    # ------------------------------------------------------------------
    def _render_unicode_text(self, value: object) -> str:
        # emoji 交给容器内的彩色 emoji 字体渲染（不再包成远程图片）。
        return self._text(value)

    @staticmethod
    def _avatar_color(nickname: str) -> str:
        return _AVATAR_COLORS[sum(map(ord, nickname or "")) % len(_AVATAR_COLORS)]

    def _render_avatar(self, nickname: str, avatar: str, *, small: bool) -> str:
        size_class = "reply-avatar" if small else "avatar"
        initial = self._text((nickname or "").strip()[:1] or _FALLBACK_AVATAR_CHAR)
        color = self._avatar_color(nickname)
        image = ""
        if avatar:
            image = (
                f'<img src="{self._url(avatar)}" alt="" '
                "onerror=\"this.style.display='none'\">"
            )
        return (
            f'<div class="{size_class} avatar-shell" '
            f'style="background:{color}"><span>{initial}</span>{image}</div>'
        )

    def _render_rich_text(self, parts: Iterable[Any]) -> str:
        output: list[str] = []
        for part in parts:
            kind = part.kind
            if kind == "line-break":
                output.append("<br>")
            elif kind == "highlight":
                # Only @mentions keep the accent colour; links, topics and any
                # other highlighted fragment render in the single body colour.
                text = str(part.text or "")
                if text.startswith(("@", "\uff20")):
                    output.append(
                        '<span class="highlight">'
                        f"{self._render_unicode_text(part.text)}</span>"
                    )
                else:
                    output.append(
                        f"<span>{self._render_unicode_text(part.text)}</span>"
                    )
            elif kind == "emoji-text":
                output.append(
                    '<span class="emoji-text">'
                    f"{self._render_unicode_text(part.text)}</span>"
                )
            elif kind == "emote":
                # 平台目录（如 NGA 的 [s:ac:汗]）不一定带 url，token 才是唯一可靠的线索，
                # 所以一律按 token 登记待解析，解析成功再出图。
                token = str(part.text or "")
                resolved = self._emote_urls.get(token)
                if not resolved and token:
                    self._pending_emotes.setdefault(token, set()).add(token)
                source = resolved or str(part.url or "")
                if source:
                    output.append(
                        f'<img class="emote" src="{self._url(source)}" '
                        f'alt="{self._url(token)}" '
                        'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                    )
                elif token:
                    output.append(f'<span class="emoji-text">{self._render_unicode_text(token)}</span>')
            else:
                output.append(f"<span>{self._render_unicode_text(part.text)}</span>")
        return "".join(output)

    def _render_images(self, urls: Iterable[str], *, sticker: bool = False) -> str:
        img_class = "sticker-image" if sticker else "comment-image"
        wrap_class = "sticker-image-wrap" if sticker else "comment-image-wrap"
        return "".join(
            f'<div class="{wrap_class}">'
            f'<img class="{img_class}" src="{self._url(url)}" alt="" '
            "onerror=\"this.parentElement.style.display='none'\">"
            "</div>"
            for url in urls
            if url
        )

    def _render_pinned_badge(self, entry: Any) -> str:
        # Only top-level entries can be pinned; the marker trails the nickname.
        if not getattr(entry, "pinned", False):
            return ""
        return '<span class="pinned">置顶</span>'

    @staticmethod
    def _is_author_entry(entry: Any) -> bool:
        """True when the comment was written by the work's own author (UP主/原博)."""
        author = getattr(entry, "author", None)
        if author is None:
            return False
        if getattr(author, "is_up", False):
            return True
        for badge in getattr(author, "badges", None) or ():
            label = str(getattr(badge, "text", "") or "").strip()
            if label in {"作者", "原博", "UP主"}:
                return True
        return False

    def _render_author_badge(self, entry: Any) -> str:
        # Order inside the nickname row is: nickname, 作者, 置顶.
        # 文案统一是通用模板的「作者」，不按平台改成 UP主/楼主。
        if not self._is_author_entry(entry):
            return ""
        return '<span class="author">作者</span>'

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------
    def _render_entry(self, entry: Any, *, nested: bool) -> str:
        avatar = self._render_avatar(
            entry.author.nickname,
            entry.author.avatar,
            small=nested,
        )
        name = (
            '<div class="nickname">'
            f'<span class="nickname-text">{self._text(entry.author.nickname)}</span>'
            f"{self._render_author_badge(entry)}"
            f"{self._render_pinned_badge(entry)}</div>"
        )
        rich = self._render_rich_text(entry.content)
        images = self._render_images(entry.images)

        if nested:
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="comment-body">{name}'
                f'<div class="comment-content">{rich}</div>{images}'
                "</div></div>"
            )

        first_reply = (
            self._render_entry(entry.first_reply, nested=True)
            if entry.first_reply is not None
            else ""
        )
        # 楼中楼: platforms that expose several child replies per root fill
        # ``replies``; skip the one already rendered as ``first_reply``.
        nested_replies = "".join(
            self._render_entry(child, nested=True)
            for child in (getattr(entry, "replies", None) or ())
            if child is not entry.first_reply
        )
        return (
            '<article class="comment-card">'
            f'{avatar}<section class="comment-body">{name}'
            f'<div class="comment-content">{rich}</div>'
            f"{images}{first_reply}{nested_replies}</section></article>"
        )

    # ------------------------------------------------------------------
    # Document / page
    # ------------------------------------------------------------------
    def render_entries_fragment(self, document: Any) -> str:
        """Render comment rows without the standalone page shell."""
        return "".join(
            self._render_entry(entry, nested=False) for entry in document.entries
        )

    def _layout_css(self, document: Any) -> str:
        """平台专属覆盖，接在共享 ``_CSS`` 之后，不影响其它平台。"""
        theme = getattr(document, "theme", None)
        platform_name = str(getattr(theme, "platform_name", "") or "")
        return _LAYOUT_CSS.get(platform_name, "")

    def build_html(self, document: Any) -> str:
        entries = self.render_entries_fragment(document)
        # 评论图接在正文卡下方，不输出「热门评论」标题栏，
        # 但保留底部署名，和其他平台的卡片一致。
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-comment-styles">
{_CSS}
{self._layout_css(document)}
</style></head><body><div id="parser-x-comment-root" class="page"><div class="shell">
<main class="comment-list">{entries}</main>
</div></div></body></html>"""

    @staticmethod
    def _document_platform_key(document: Any) -> str:
        theme = getattr(document, "theme", None)
        name = str(getattr(theme, "platform_name", "") or "")
        return _PLATFORM_KEY_BY_NAME.get(name, "")

    async def _warm_emotes(self, document: Any) -> None:
        """渲染前把评论区里的平台表情按需下载好，失败就回落文本。"""
        resolver = self.emote_resolver
        if resolver is None:
            return
        self._pending_emotes = {}
        platform_key = self._document_platform_key(document)
        self._emote_platform = platform_key
        self._emote_urls = {}
        # 先走一遍渲染，收集需要解析的表情名（不改动 document）。
        self.render_entries_fragment(document)
        if not self._pending_emotes:
            return
        resolved: dict[str, str] = {}
        if platform_key:
            try:
                resolved.update(await resolver.resolve(platform_key, set(self._pending_emotes)))
            except Exception:
                logger.debug("评论区表情解析失败", exc_info=True)
        self._emote_urls = resolved
        self._pending_emotes = {}

    async def render(
        self,
        out_path: Path,
        document: Any,
        *,
        target_width: int = 900,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await self._warm_emotes(document)
        html = self.build_html(document)
        self._emote_urls = {}
        await self.render_service.render(
            out_path,
            html,
            options={
                "type": "png",
                "full_page": True,
                "scale": "css",
            },
            target_width=target_width,
            fallback_width=target_width,
            bottom_padding=20,
        )

# 平台专属排版覆盖。共享 _CSS 是按 34px 正文设计的，各平台卡片正文尺度不同，
# 需要对齐字号时在这里按 platform_name 加覆盖，不要改 _CSS——那会连累所有平台。
_LAYOUT_CSS: dict[str, str] = {
    "小黑盒": """
.comment-content{margin-top:14px;font-size:30px;line-height:1.6;color:#e8e8e8}
.nickname{font-size:28px}
.reply-card .nickname{font-size:25px}
.reply-card .comment-content{margin-top:9px;font-size:27px;color:#dcdcdc}
""",
}

__all__ = ["CommentCanvasBase"]
