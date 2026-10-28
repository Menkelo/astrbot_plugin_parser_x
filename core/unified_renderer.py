"""统一卡片渲染器：正文 + 评论区一次性渲染出图。

合并了原 TextCardRenderer + CommentCanvasBase，
移除 Shadow DOM 与 PIL 拼接，单一 HTML → Playwright → PNG。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from html import escape
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .comment_gallery import GALLERY_CSS, GALLERY_SCRIPT
from .card_theme import resolve_platform_glyph
from .constants import COMMENT_FOOTER_BRAND as _FOOTER_BRAND
from .html_renderer import HtmlRenderService
from .platform_emotes import iter_emote_matches

EMOTE_WARMUP_SECONDS = 3.0
MAX_WARM_EMOTES = 32

# 复用评论区数据结构
from .comment_canvas import (
    CommentDocument,
    CommentEntry,
    CommentAuthor,
    CommentRichPart,
    CommentBadge,
    CommentTheme,
)
from .parsers.bilibili.comment_canvas import (
    BiliCommentDocument,
    BiliCommentEntry,
    BiliAuthorBadge,
    BiliRichPart,
    BiliFanMedal,
    BiliCommentDecor,
)

_RICH_TEXT_SUFFIX = (
    r"|(?P<mention>[@\uff20][\w\u4e00-\u9fff\u3400-\u4dbf.-]{1,32})"
    r"|(?P<url>https?://[^\s<>()\"']+)"
    r"|(?P<link>\u7f51\u9875\u94fe\u63a5|\u62bd\u5956\u8be6\u60c5|查看链接)"
)
RICH_TEXT_RE = re.compile(
    r"(?P<topic>#[^#\s<>]{1,80}#|(?<![\w/:])#[^#\s<>]{1,80})"
    + _RICH_TEXT_SUFFIX
)
# Douyin uses opening hashes: in #青春遗憾#随笔 the second hash starts a topic.
DOUYIN_RICH_TEXT_RE = re.compile(
    r"(?P<topic>#[^#\s<>]{1,80})" + _RICH_TEXT_SUFFIX
)

# B站视频评论中的分:秒 / 时:分:秒；先匹配完整链接，避免拆开 URL。
BILI_RICH_TEXT_RE = re.compile(
    DOUYIN_RICH_TEXT_RE.pattern
    + r"|(?P<timestamp>(?<![0-9A-Za-z_:])[0-9]{1,3}:(?:[0-5][0-9]:)?[0-5][0-9](?![0-9A-Za-z_:]))"
)

_FALLBACK_AVATAR_CHAR = "评"
_AVATAR_COLOR = "#1b2432"
# 统一 CSS：正文卡 + 评论区共用
_UNIFIED_CSS = """
/* ===== 全平台统一卡片版式（深色、900px、头图 + 头像署名行 + 正文流 + 评论区） ===== */
*{box-sizing:border-box}
html,body{margin:0;width:900px;background:#000000;color:#FFFFFF}
html{overflow-x:hidden;scrollbar-width:none}
html::-webkit-scrollbar{width:0;height:0}
body{padding:0;overflow-x:hidden;font-family:'Noto Sans SC','Noto Sans CJK SC','WenQuanYi Zen Hei','Noto Color Emoji',sans-serif}
.card{width:100%;position:relative;max-width:100%;overflow:hidden;border:0;border-radius:0;background:#000000;color:#FFFFFF}
/* 方案三原稿 400px → 输出 900px：内容区所有几何尺寸按 2.25 倍适配。 */
.content-body{padding:40.5px 36px 22.5px}
.content-body:has(>.comment-section){padding-bottom:0}

/* 头图固定 900x480（1.875:1） */
.hero{width:100%;height:480px;max-height:480px;overflow:hidden;border-bottom:0;background:#000}
.hero-image{display:block;width:100%;height:480px;min-height:0;max-height:480px;object-fit:cover;object-position:center}
.hero-image.media-contain,.hero-image.media-video{object-fit:cover;object-position:center;background:#000}

.primary-block{display:grid;gap:12px;padding:0;margin-bottom:36px}
.primary-block h1{margin:0;overflow-wrap:anywhere;color:#fff;font-size:37.125px;font-weight:600;line-height:1.45}
.primary-block h1::after{content:"";display:block;width:72px;height:4.5px;background:#2E323B;border-radius:4.5px;margin-top:27px}
.primary-block h1 .inline-emote{width:1.15em;height:1.15em;vertical-align:-0.2em}
.meta{display:none}
.metrics,.info-chips{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.metric,.info-chip{display:inline-flex;align-items:center;gap:5px;padding:5px 9px;border-radius:999px;background:#141414;color:#8a919e;font-size:22px;line-height:1.45}
.metric strong{color:#e8e8e8;font-weight:650}

.profile-block{padding:0;margin-bottom:31.5px}
.profile{display:flex;align-items:center;gap:24.75px}
.profile-avatar{position:relative;display:grid;width:81px;height:81px;place-items:center;flex:0 0 81px;overflow:hidden;border:2.25px solid #282B33;border-radius:50%;background:#1b2432;color:#FFFFFF;font-size:31.5px;font-weight:600}
.profile-avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.profile-copy{display:grid;min-width:0;gap:4.5px;flex:1}
.profile-name{display:block;min-width:0;flex-wrap:nowrap}
.author{display:block;max-width:none;margin:0;padding:0;border:0;border-radius:0;background:transparent;color:#FFFFFF;font-size:31.5px;line-height:1.3;font-weight:600;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
.profile-badge{background:#2a2d35;color:#c9cdd4;font-size:22px;font-weight:600;line-height:32px;padding:2px 12px;border-radius:8px}
.profile-meta{color:#555963;font-size:28px;line-height:1.3}

.profile-sub-row{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.profile-platform{font-size:22.5px;font-weight:600;line-height:1.5;padding:2.25px 11.25px;border-radius:6.75px;background:rgba(148,153,163,.14);color:#9499A3}
.copy-block{padding:20.25px 27px;background:#111215;border:2.25px solid #1E2026;border-radius:13.5px;margin-bottom:18px}
.text{color:#8A8F99;font-size:29.25px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.content-flow{display:grid;gap:18px;padding:0;margin-bottom:18px}
.flow-text{padding:20.25px 27px;border:2.25px solid #1E2026;border-radius:13.5px;background:#111215;color:#8A8F99;font-size:29.25px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.flow-image-wrap{width:100%;margin:0;overflow:hidden;border:0;border-radius:16px;background:transparent}
.flow-image{display:block;width:100%;max-width:100%;height:auto}
.text-link,.text-mention{color:#7B9CD3;font-weight:650}
.inline-emote{display:inline-block;width:1.15em;height:1.15em;margin:0 2px;object-fit:contain;vertical-align:-0.2em}

.info-block{padding:22px 32px}
.section-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.section-head h2{margin:0;color:#e8e8e8;font-size:28px;line-height:1.45}
.section-head>span{color:#8a919e;font-size:24px}
.info-chips{margin-top:11px}

/* ===== 评论区（内联在正文卡下方）===== */
.comment-section{padding:0}
.comment-heading{margin:27px 0 0;color:#9499A3;font-size:27px;font-weight:600;line-height:1.5}
.comment-list{padding:0}
.comment-card{display:flex;gap:24.75px;padding:22.5px 0 31.5px;position:relative}
/* 字体行框上下约有9px留白；只补偿外层，不改变楼中楼内部布局。
   以图片/回复框结尾时已有清晰边缘，仍保留31.5px底部留白。 */
.comment-card:has(>.comment-body>.comment-content:last-child){padding-bottom:22.5px}
.comment-card:not(:last-child)::after{content:"";position:absolute;bottom:0;left:105.75px;right:0;height:2.25px;background:#1E2026}
.comment-avatar{position:relative;flex:0 0 81px;width:81px;height:81px;border-radius:50%;display:flex;align-items:center;justify-content:center;overflow:hidden;border:2.25px solid #282B33;color:#FFFFFF;font-size:31.5px;font-weight:600}
.comment-avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.comment-body{min-width:0;flex:1}
.comment-nickname{display:flex;align-items:center;gap:10px;font-size:29.25px;font-weight:500;color:#9499A3}
.comment-nickname-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.comment-content{margin-top:11.25px;font-size:31.5px;line-height:1.5;color:#FFFFFF;word-break:break-word;overflow-wrap:anywhere}
.comment-highlight{color:#7B9CD3}
.comment-emoji-text{display:inline-block;margin:0 .15em;padding:0 .32em;border-radius:.32em;background:#262a3d;color:#c9cdd4;font-size:.85em;line-height:1.35;font-family:"Noto Color Emoji","Apple Color Emoji","Segoe UI Emoji","Noto Emoji",sans-serif;font-variant-emoji:emoji}
.comment-emote{display:inline-block;height:1.15em;width:auto;max-width:3em;margin:0 .1em;object-fit:contain;vertical-align:-.2em;}
.comment-image-wrap,.comment-sticker-wrap{display:block;width:fit-content;max-width:100%;margin-top:16px;overflow:hidden;border-radius:16px;background:#202228}
.comment-image{display:block;width:auto;height:auto;max-width:640px;object-fit:contain}
.comment-sticker{display:block;width:auto;height:auto;max-width:320px;max-height:320px;object-fit:contain}
.comment-pinned,.comment-author{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;height:1.55em;margin-left:5px;padding:0 .42em;border-radius:.3em;font-size:.72em;font-weight:600;line-height:1;white-space:nowrap}
.comment-pinned{background:#262a3d;color:#c9cdd4}
.comment-author{background:#3a2b16;color:#e8b45f}
.reply-card{display:flex;gap:20.25px;margin-top:13.5px;padding:20.25px 24.75px;background:#16181D;border:2.25px solid #1E2026;border-radius:13.5px}
.reply-card .comment-avatar{flex:0 0 54px;width:54px;height:54px;font-size:24.75px}
.reply-card .comment-body{min-width:0;flex:1}
.reply-card .comment-nickname{font-size:27px;line-height:normal}
.reply-card .comment-content{margin-top:11.25px;font-size:29.25px}

/* ===== 卡片底部品牌署名 ===== */
.footer{padding:15.75px 36px;border-top:2.25px solid #1E2026;text-align:center}
.footer-brand{color:#555963;font-size:25.875px;line-height:1.35}
"""

def _format_metric_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) >= 100_000_000:
            return f"{number / 100_000_000:.1f}".removesuffix(".0") + "亿"
        if abs(number) >= 10_000:
            return f"{number / 10_000:.1f}".removesuffix(".0") + "万"
        if number.is_integer():
            return f"{int(number):,}"
    return str(value or "").strip()

def _normalise_metrics(metrics: object) -> list[tuple[str, str]]:
    if isinstance(metrics, dict):
        source: list[Any] = list(metrics.items())
    elif isinstance(metrics, (list, tuple)):
        source = list(metrics)
    else:
        return []
    output: list[tuple[str, str]] = []
    for item in source:
        label: object = ""
        value: object = ""
        if isinstance(item, dict):
            label = item.get("label") or item.get("name")
            value = item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label, value = item[0], item[1]
        label_text = str(label or "").strip()
        value_text = _format_metric_value(value)
        if label_text and value_text:
            output.append((label_text, value_text))
        if len(output) >= 6:
            break
    return output

def _normalise_info_items(items: object) -> list[str]:
    if isinstance(items, str):
        source = [items]
    elif isinstance(items, (list, tuple, set)):
        source = list(items)
    else:
        return []
    output = []
    for item in source:
        text = str(item or "").strip()
        if text and text not in output:
            output.append(text)
        if len(output) >= 8:
            break
    return output

def _normalise_content_blocks(blocks: object) -> list[tuple[str, str]]:
    if not isinstance(blocks, (list, tuple)):
        return []
    output: list[tuple[str, str]] = []
    for item in blocks:
        kind: object = ""
        value: object = ""
        if isinstance(item, dict):
            kind = item.get("type") or item.get("kind")
            if kind == "text":
                value = item.get("text") or item.get("value")
            elif kind == "image":
                value = item.get("url") or item.get("src") or item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            kind, value = item[0], item[1]
        kind_text = str(kind or "").strip().lower()
        # Source whitespace is content for text blocks, including standalone newlines.
        value_text = str(value or "")
        if kind_text != "text":
            value_text = value_text.strip()
        if kind_text in {"text", "image"} and value_text:
            if kind_text == "text" and not value_text.strip() and not any(c in value_text for c in "\r\n"):
                continue
            output.append((kind_text, value_text))
    return output

class UnifiedCardRenderer:
    """统一卡片渲染器：正文区 + 评论区一次性渲染。"""

    def __init__(self, render_service: HtmlRenderService):
        self.render_service = render_service
        # 运行时表情解析器（由插件注入）。为空时评论表情原样走官方地址。
        self.emote_resolver: object | None = None
        self._emote_state: ContextVar[dict[str, object]] = ContextVar(
            "unified_emote_state"
        )

    @staticmethod
    def _escape_jinja(value: str) -> str:
        return value.replace("{", "&#123;").replace("}", "&#125;")

    @classmethod
    def _safe_text(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or "")))

    @classmethod
    def _safe_url(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or ""), quote=True))

    @classmethod
    def _render_basic_text_html(
        cls, text: str, *, bili_timestamps: bool = False, platform_key: str | None = None
    ) -> str:
        parts: list[str] = []
        last = 0
        pattern = (
            BILI_RICH_TEXT_RE if bili_timestamps
            else DOUYIN_RICH_TEXT_RE if platform_key in {'douyin', 'bilibili'}
            else RICH_TEXT_RE
        )
        for match in pattern.finditer(text or ""):
            parts.append(cls._safe_text(text[last : match.start()]))
            css_class = (
                "comment-highlight" if match.lastgroup == "timestamp"
                else "text-link text-mention" if match.lastgroup == "mention"
                else "text-link"
            )
            parts.append(
                f'<span class="{css_class}">'
                f"{cls._safe_text(match.group(0))}</span>"
            )
            last = match.end()
        parts.append(cls._safe_text(text[last:]))
        return "".join(parts)

    @classmethod
    def _render_text_html(
        cls,
        text: str,
        *,
        platform_key: str | None = None,
        emotes: object = None,
    ) -> str:
        catalog = emotes if isinstance(emotes, dict) else {}
        parts: list[str] = []
        last = 0
        for start, end, token, url in iter_emote_matches(
            text or "",
            platform_key or "",
            catalog,
        ):
            parts.append(cls._render_basic_text_html(text[last:start], platform_key=platform_key))
            if url:
                parts.append(
                    f'<img class="inline-emote" src="{cls._safe_url(url)}" '
                    f'alt="{cls._safe_url(token)}" '
                    'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                )
            else:
                parts.append(cls._safe_text(token))
            last = end
        parts.append(cls._render_basic_text_html(text[last:], platform_key=platform_key))
        return "".join(parts)

    @staticmethod
    def _avatar_color(nickname: str) -> str:
        return _AVATAR_COLOR

    def _emote_context(self) -> dict[str, object]:
        try:
            return self._emote_state.get()
        except LookupError:
            return {"urls": {}, "sheets": {}, "pending": {}, "platform": ""}

    def _render_avatar(self, nickname: str, avatar: str, *, size: int = 84) -> str:
        initial = self._safe_text((nickname or "").strip()[:1] or _FALLBACK_AVATAR_CHAR)
        color = self._avatar_color(nickname)
        image = ""
        if avatar:
            image = (
                f'<img src="{self._safe_url(avatar)}" alt="" '
                "onerror=\"this.style.display='none'\">"
            )
        return (
            f'<div class="comment-avatar" style="width:{size}px;height:{size}px;background:{color}">'
            f'<span>{initial}</span>{image}</div>'
        )

    # ---------- 通用评论区渲染（CommentDocument） ----------
    def _render_comment_entry(self, entry: CommentEntry, *, nested: bool, platform_key: str | None) -> str:
        avatar = self._render_avatar(entry.author.nickname, entry.author.avatar, size=54 if nested else 81)
        
        # 昵称行
        nickname_html = (
            '<div class="comment-nickname">'
            f'<span class="comment-nickname-text">{self._safe_text(entry.author.nickname)}</span>'
        )
        # 徽章顺序固定为昵称 → 作者 → 置顶。
        badges_html = ""
        if entry.author.is_up:
            badges_html += '<span class="comment-author">作者</span>'
        else:
            # 兼容只提供文字徽章、没有 is_up 标志的旧数据。
            # 徽章文案统一走通用模板的「作者」，不按平台改成 UP主/楼主。
            for badge in entry.author.badges:
                if badge.text in {"作者", "原博", "UP主", "楼主"}:
                    badges_html += '<span class="comment-author">作者</span>'
                    break
        if entry.pinned:
            badges_html += '<span class="comment-pinned">置顶</span>'
        nickname_html += badges_html + "</div>"

        # 正文内容
        content_html = self._render_comment_rich_text(entry.content)
        
        # 图片
        images_html = self._render_comment_gallery(entry.images)
        if entry.sticker_image:
            images_html += (
                '<div class="comment-sticker-wrap">'
                f'<img class="comment-sticker" src="{self._safe_url(entry.sticker_image)}" alt="" '
                "onerror=\"this.parentElement.style.display='none'\">"
                "</div>"
            )

        if nested:
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="comment-body">{nickname_html}'
                f'<div class="comment-content">{content_html}</div>{images_html}'
                "</div></div>"
            )

        # 顶层评论：包含首条回复 + 楼中楼其余回复
        first_reply_html = ""
        if entry.first_reply is not None:
            first_reply_html = self._render_comment_entry(entry.first_reply, nested=True, platform_key=platform_key)
        nested_replies_html = "".join(
            self._render_comment_entry(child, nested=True, platform_key=platform_key)
            for child in (getattr(entry, "replies", None) or ())
            if child is not entry.first_reply
        )

        return (
            '<article class="comment-card">'
            f'{avatar}<section class="comment-body">{nickname_html}'
            f'<div class="comment-content">{content_html}</div>'
            f"{images_html}{first_reply_html}{nested_replies_html}</section></article>"
        )

    def _render_comment_rich_text(self, parts: list[CommentRichPart]) -> str:
        output: list[str] = []
        for part in parts:
            kind = part.kind
            if kind == "line-break":
                output.append("<br>")
            elif kind == "highlight":
                output.append(
                    '<span class="comment-highlight">'
                    f"{self._safe_text(part.text)}</span>"
                )
            elif kind == "emoji-text":
                token = str(part.text or "")
                resolved = self._emote_context()["urls"].get(token)
                if resolved:
                    output.append(
                        f'<img class="comment-emote" src="{self._safe_url(resolved)}" '
                        f'alt="{self._safe_url(token)}">'
                    )
                else:
                    output.append(
                        '<span class="comment-emoji-text">'
                        f"{self._safe_text(token)}</span>"
                    )
            elif kind == "emote":
                token = str(part.text or "")
                state = self._emote_context()
                resolved = state["urls"].get(token)
                resolver = self.emote_resolver
                known = getattr(resolver, "known_tokens", lambda _key: set())(str(state["platform"])) if resolver else set()
                if not resolved and token in known:
                    output.append(f"<span>{self._safe_text(token)}</span>")
                    continue
                if not resolved and part.url:
                    resolved = part.url
                if not resolved:
                    output.append(f"<span>{self._safe_text(token)}</span>")
                    continue
                sprite = state["sheets"].get(token)
                style = ""
                if sprite:
                    # 雪碧图：整行只显示第一格，其余图层由卡片静态呈现。
                    cell, layers = sprite
                    style = (
                        f' style="width:{cell}px;height:{cell}px;max-width:{cell}px;'
                        f'object-fit:cover;object-position:0 0"'
                    )
                output.append(
                    f'<img class="comment-emote" src="{self._safe_url(resolved or part.url)}" '
                    f'alt="{self._safe_url(part.text)}"{style} '
                    'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                )
            else:
                output.append(f"<span>{self._render_basic_text_html(part.text)}</span>")
        # HTML collapses literal source newlines inside spans; make them explicit.
        return "".join(output).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    async def _warm_emotes(self, document: object, platform_key: str | None) -> None:
        resolver = self.emote_resolver
        state = {"urls": {}, "sheets": {}, "pending": {}, "platform": platform_key or "", "managed": set()}
        self._emote_state.set(state)
        if resolver is None or document is None:
            return
        entries = getattr(document, "entries", None) or ()
        sources: dict[str, str] = {}
        tokens: set[str] = set()
        def collect(entries):
            for entry in entries or ():
                for part in getattr(entry, "content", ()) or ():
                    if getattr(part, "kind", "") in {"emote", "emoji-text"} and getattr(part, "text", ""):
                        token = str(part.text)
                        tokens.add(token)
                        if getattr(part, "url", ""):
                            sources[token] = str(part.url)
                yield from collect(getattr(entry, "replies", None))
                first = getattr(entry, "first_reply", None)
                if first is not None:
                    yield from collect((first,))

        list(collect(entries))
        try:
            # Only our managed official catalog is warmed server-side. Arbitrary
            # comment image URLs keep their individual browser-rendered URLs.
            resolve = getattr(resolver, "resolve", None)
            known = getattr(resolver, "known_tokens", lambda _key: set())(platform_key or "")
            managed = {token for token in tokens if token in known}
            if managed and callable(resolve):
                selected = set(sorted(managed)[:MAX_WARM_EMOTES])
                state["urls"].update(await asyncio.wait_for(
                    resolve(platform_key or "", selected, sources={
                        token: sources[token] for token in selected if token in sources
                    }), timeout=EMOTE_WARMUP_SECONDS,
                ))
        except Exception:
            logger.debug("评论区表情解析失败", exc_info=True)
        sheets = getattr(resolver, "sheets", None)
        if callable(sheets):
            try:
                state["sheets"].update(dict(sheets() or {}))
            except Exception:
                logger.debug("表情雪碧图信息读取失败", exc_info=True)

    def _render_comment_section(self, document: CommentDocument | BiliCommentDocument | None, platform_key: str | None) -> str:
        if document is None:
            return ""
        
        # 判断文档类型
        is_bili = isinstance(document, BiliCommentDocument)
        entries = document.entries
        
        if not entries:
            return ""

        rendered_entries = []
        for entry in entries:
            if is_bili:
                rendered_entries.append(self._render_bili_comment_entry(entry, platform_key=platform_key))
            else:
                rendered_entries.append(self._render_comment_entry(entry, nested=False, platform_key=platform_key))

        return (
            '<section class="comment-section">'
            '<h2 class="comment-heading">热门评论</h2>'
            '<main class="comment-list">'
            f"{''.join(rendered_entries)}"
            "</main></section>"
        )

    # ---------- B站专用评论渲染 ----------
    def _render_bili_comment_entry(self, entry: BiliCommentEntry, *, platform_key: str | None, nested: bool = False) -> str:
        avatar = self._render_avatar(entry.author.nickname, entry.author.avatar, size=54 if nested else 81)

        nickname_html = (
            '<div class="comment-nickname">'
            f'<span class="comment-nickname-text">{self._safe_text(entry.author.nickname)}</span>'
        )
        badges_html = ""
        if entry.author.is_up:
            badges_html += '<span class="comment-author">作者</span>'
        if entry.pinned:
            badges_html += '<span class="comment-pinned">置顶</span>'
        nickname_html += badges_html + "</div>"

        content_html = self._render_bili_rich_text(entry.content)
        
        images_html = self._render_comment_gallery(entry.images)

        first_reply_html = (
            self._render_bili_comment_entry(entry.first_reply, platform_key=platform_key, nested=True)
            if entry.first_reply is not None else ""
        )
        nested_replies_html = "".join(
            self._render_bili_comment_entry(child, platform_key=platform_key, nested=True)
            for child in (getattr(entry, "replies", None) or ())
            if child is not entry.first_reply
        )

        # 层级由调用方决定，不能用「有无回复/装饰」猜测。
        if not nested:
            return (
                '<article class="comment-card">'
                f'{avatar}<section class="comment-body">{nickname_html}'
                f'<div class="comment-content">{content_html}</div>'
                f"{images_html}{first_reply_html}{nested_replies_html}</section></article>"
            )
        else:
            # 纯回复（楼中楼非首条）通常不单独渲染，由首条回复包含
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="comment-body">{nickname_html}'
                f'<div class="comment-content">{content_html}</div>{images_html}'
                "</div></div>"
            )

    def _render_bili_rich_text(self, parts: list[BiliRichPart]) -> str:
        output: list[str] = []
        for part in parts:
            kind = part.kind
            if kind == "line-break":
                output.append("<br>")
            elif kind == "highlight":
                output.append(
                    '<span class="comment-highlight">'
                    f"{self._safe_text(part.text)}</span>"
                )
            elif kind == "emote":
                token = str(part.text or "")
                state = self._emote_context()
                resolved = state["urls"].get(token)
                resolver = self.emote_resolver
                known = getattr(resolver, "known_tokens", lambda _key: set())(str(state["platform"])) if resolver else set()
                if not resolved and token in known:
                    output.append(f"<span>{self._safe_text(token)}</span>")
                    continue
                resolved = resolved or part.url
                if resolved:
                    output.append(
                        f'<img class="comment-emote" src="{self._safe_url(resolved)}" '
                        f'alt="{self._safe_url(token)}" '
                        'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                    )
                else:
                    output.append(f"<span>{self._safe_text(token)}</span>")
            else:
                output.append(f"<span>{self._render_basic_text_html(part.text, bili_timestamps=True)}</span>")
        return "".join(output)

    # ---------- 正文区渲染 ----------
    def _render_content_flow(
        self,
        blocks: list[tuple[str, str]],
        *,
        platform_key: str | None,
        emotes: object,
    ) -> str:
        if not blocks:
            return ""
        rendered: list[str] = []
        for kind, value in blocks:
            if kind == "text":
                rendered.append(
                    '<div class="flow-text">'
                    + self._render_text_html(value, platform_key=platform_key, emotes=emotes)
                    + "</div>"
                )
            else:
                rendered.append(
                    '<figure class="flow-image-wrap">'
                    f'<img class="flow-image" src="{self._safe_url(value)}" alt="" '
                    "onerror=\"this.parentElement.style.display='none'\">"
                    "</figure>"
                )
        return f'<section class="content-flow">{"".join(rendered)}</section>'

    # ---------- 主入口 ----------
    def _render_comment_gallery(self, urls) -> str:
        images = [f'<img class="comment-image" src="{self._safe_url(url)}" alt="">' for url in urls if url]
        if not images:
            return ""
        return '<div class="justified-gallery">' + ''.join(images) + '</div>'

    def build_html(
        self,
        *,
        platform_name: str,
        author_name: str | None,
        text: str,
        author_avatar: str | None = None,
        title: str | None = None,
        timestamp_text: str | None = None,
        platform_key: str | None = None,
        media_url: str | None = None,
        media_fit: str = "cover",
        content_kind: str | None = None,
        metrics: object = None,
        info_items: object = None,
        info_title: str | None = None,
        author_badge: str | None = None,
        comment_document: CommentDocument | BiliCommentDocument | None = None,
        emotes: object = None,
        content_blocks: object = None,
        content_layout: str | None = None,
        author_meta: str | None = None,
    ) -> str:
        glyph = resolve_platform_glyph(platform_key, platform_name)
        display_name = str(platform_name or "").strip()
        layout_class = " layout-xiaoheihe"
        
        footer_html = (
            '<footer class="footer">'
            f'<div class="footer-brand">{_FOOTER_BRAND}</div></footer>'
        )

        normalised_blocks = _normalise_content_blocks(content_blocks)
        # Some platforms use the same caption as both title and body. Compare
        # only the complete text actually rendered; preserve all body/media data.
        visible_body = (
            "\n\n".join(value.strip() for kind, value in normalised_blocks if kind == "text")
            if normalised_blocks else str(text or "").strip()
        )
        card_title = str(title or "").strip()
        if visible_body and card_title == visible_body.strip():
            card_title = ""
        card_title_html = (
            self._render_text_html(card_title, platform_key=platform_key, emotes=emotes)
            if card_title else ""
        )

        profile_html = ""
        if author_name:
            initial = self._safe_text(str(author_name).strip()[:1] or glyph)
            avatar_image = ""
            if author_avatar:
                avatar_image = (
                    f'<img src="{self._safe_url(author_avatar)}" alt="" '
                    "onerror=\"this.style.display='none'\">"
                )
            # Keep non-time attribution accepted by the API, but never display
            # parser-generated publication dates in the author row.
            meta_text = str(author_meta or "").strip()
            if re.fullmatch(r"(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}(?:[ T].*)?", meta_text):
                meta_text = ""
            origin_html = (
                f'<span class="profile-meta">{self._safe_text(meta_text)}</span>'
                if meta_text
                else ""
            )
            profile_html = (
                '<section class="profile-block"><div class="profile">'
                f'<div class="profile-avatar"><span>{initial}</span>{avatar_image}</div>'
                '<div class="profile-copy"><div class="profile-name">'
                f'<span class="author">{self._safe_text(author_name)}</span>'
                + (f'<span class="profile-badge">{self._safe_text(author_badge)}</span>' if author_badge else "")
                + '</div><div class="profile-sub-row">'
                + f'<span class="profile-platform">{self._safe_text(display_name)}</span>'
                + f"{origin_html}</div></div></div></section>"
            )

        # Public articles may omit account identity; never invent an author or
        # hide the known source platform together with the missing profile.
        if not profile_html and platform_key in {'coolapk', 'wechat_article'}:
            profile_html = (
                '<section class="profile-block"><div class="profile-sub-row">'
                f'<span class="profile-platform">{self._safe_text(display_name)}</span>'
                '</div></section>'
            )

        flow_html = self._render_content_flow(normalised_blocks, platform_key=platform_key, emotes=emotes)
        text_html = self._render_text_html(text, platform_key=platform_key, emotes=emotes)
        text_block = (
            f'<section class="copy-block"><div class="text">{text_html}</div></section>'
            if text and not normalised_blocks else ""
        )

        metric_items = _normalise_metrics(metrics)
        metric_html = ""
        if metric_items:
            metric_html = (
                '<div class="metrics">'
                + "".join(
                    '<span class="metric">'
                    f"<span>{self._safe_text(label)}</span>"
                    f"<strong>{self._safe_text(value)}</strong></span>"
                    for label, value in metric_items
                )
                + "</div>"
            )

        primary_block = ""
        if card_title_html or metric_html:
            primary_block = (
                '<section class="primary-block">'
                + (f"<h1>{card_title_html}</h1>" if card_title_html else "")
                + f"{metric_html}</section>"
            )

        # Legacy info arguments remain accepted for parser compatibility, but
        # plugin-generated informational chips are no longer part of the card.

        media_html = ""
        if media_url:
            if media_fit in ("contain", "video"):
                fit_class = f" media-{media_fit}"
            else:
                fit_class = ""
            media_html = (
                '<div class="hero">'
                f'<img class="hero-image{fit_class}" src="{self._safe_url(media_url)}" '
                'alt="" onerror="this.parentElement.style.display=\'none\'">'
                "</div>"
            )

        comments_html = self._render_comment_section(comment_document, platform_key)

        # 平台属性用于 CSS 选择器
        platform_attr = f' data-platform="{platform_key or platform_name}"' if platform_key else ""

        html = f"""<!doctype html>
<html lang="zh-CN" class="{layout_class.strip()}"{platform_attr}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-unified-card-styles">
{_UNIFIED_CSS}
{GALLERY_CSS}
</style></head><body><article class="card{layout_class}">

{media_html}<div class="content-body">{primary_block}
{profile_html}{text_block}{flow_html}{comments_html}</div>{footer_html}
</article>{GALLERY_SCRIPT}</body></html>"""

        return html

    async def render_card(
        self,
        out_path: Path,
        *,
        platform_name: str,
        author_name: str | None,
        text: str,
        author_avatar: str | None = None,
        title: str | None = None,
        timestamp_text: str | None = None,
        platform_key: str | None = None,
        media_url: str | None = None,
        media_fit: str = "cover",
        content_kind: str | None = None,
        metrics: object = None,
        info_items: object = None,
        info_title: str | None = None,
        author_badge: str | None = None,
        comment_document: CommentDocument | BiliCommentDocument | None = None,
        emotes: object = None,
        content_blocks: object = None,
        content_layout: str | None = None,
        author_meta: str | None = None,
    ):
        await self._warm_emotes(comment_document, platform_key)
        html = self.build_html(
            platform_name=platform_name,
            platform_key=platform_key,
            author_name=author_name,
            author_avatar=author_avatar,
            author_meta=author_meta,
            title=title,
            text=text,
            timestamp_text=timestamp_text,
            media_url=media_url,
            media_fit=media_fit,
            content_kind=content_kind,
            metrics=metrics,
            info_items=info_items,
            info_title=info_title,
            author_badge=author_badge,
            comment_document=comment_document,
            emotes=emotes,
            content_blocks=content_blocks,
            content_layout=content_layout,
        )
        normalized_layout = str(content_layout or "").strip().lower()
        phone_layout = normalized_layout in ("", "xiaoheihe")
        render_options: dict[str, object] = {
            "type": "png",
            "full_page": True,
            "scale": "css",
        }
        if platform_key in {'wechat_article', 'coolapk'}:
            flow_count = sum(kind == 'image' for kind, _ in _normalise_content_blocks(content_blocks))
            if flow_count:
                render_options['require_flow_images'] = flow_count
        target_width = 760
        if phone_layout:
            render_options["viewport_width"] = 900
            render_options["device_scale_factor"] = 1
            target_width = 900
        # 给评论区图片加载预留时间
        if comment_document is not None:
            render_options["settle_ms"] = 3000
        return await self.render_service.render(
            out_path,
            html,
            options=render_options,
            target_width=target_width,
            bottom_padding=20,
        )

__all__ = ["UnifiedCardRenderer"]