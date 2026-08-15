from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Literal

from ...card_theme import BILIBILI_CARD_THEME
from ...constants import COMMENT_FOOTER_BRAND
from ...html_renderer import HtmlRenderService

_REPLY_ICON = (
    '<svg class="reply-icon action-icon" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/>'
    "</svg>"
)

_LIKE_ICON = (
    '<svg class="like-icon action-icon" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>'
    "</svg>"
)


@dataclass(slots=True)
class BiliRichPart:
    kind: Literal["text", "line-break", "highlight", "emote"]
    text: str = ""
    url: str = ""


@dataclass(slots=True)
class BiliFanMedal:
    name: str
    level: int | None = None
    background: str = ""
    foreground: str = ""
    level_background: str = ""
    level_foreground: str = ""
    border: str = ""


@dataclass(slots=True)
class BiliAuthorBadge:
    nickname: str
    avatar: str = ""
    nickname_color: str = ""
    level: int | None = None
    senior: bool = False
    is_up: bool = False
    fan_medal: BiliFanMedal | None = None


@dataclass(slots=True)
class BiliCommentDecor:
    image: str = ""
    prefix: str = ""
    number: str = ""
    text: str = ""
    color: str = ""


@dataclass(slots=True)
class BiliCommentEntry:
    author: BiliAuthorBadge
    content: list[BiliRichPart]
    images: list[str] = field(default_factory=list)
    time_text: str = ""
    location: str = ""
    like_text: str = "0"
    reply_text: str = "回复"
    pinned: bool = False
    up_liked: bool = False
    meta_items: list[str] = field(default_factory=list)
    decor: BiliCommentDecor | None = None
    first_reply: BiliCommentEntry | None = None


@dataclass(slots=True)
class BiliCommentDocument:
    work_title: str
    cover: str
    total_text: str
    entries: list[BiliCommentEntry]
    footer_text: str = ""


class BiliCommentCanvas:
    """Render the semantic comment model through AstrBot Canvas/T2I."""

    def __init__(
        self,
        render_service: HtmlRenderService | None = None,
    ):
        self.render_service = render_service or HtmlRenderService()

    def bind(self, html_render) -> None:
        self.render_service.bind(html_render)

    @staticmethod
    def _escape_jinja(value: str) -> str:
        return value.replace("{", "&#123;").replace("}", "&#125;")

    @classmethod
    def _text(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or "")))

    @classmethod
    def _url(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or ""), quote=True))

    def _render_avatar(self, author: BiliAuthorBadge, *, small: bool) -> str:
        class_name = "reply-avatar" if small else "avatar"
        fallback = self._text((author.nickname or "B")[:1])
        image = ""
        if author.avatar:
            image = (
                f'<img src="{self._url(author.avatar)}" alt="" '
                "onerror=\"this.style.display='none'\">"
            )
        return (
            f'<div class="{class_name} avatar-shell">'
            f"<span>{fallback}</span>{image}</div>"
        )

    def _render_fan_medal(self, medal: BiliFanMedal | None) -> str:
        if medal is None:
            return ""
        styles = []
        mapping = {
            "--medal-bg": medal.background,
            "--medal-fg": medal.foreground,
            "--medal-level-bg": medal.level_background,
            "--medal-level-fg": medal.level_foreground,
            "--medal-border": medal.border,
        }
        for key, value in mapping.items():
            if value:
                styles.append(f"{key}:{value}")
        style = f' style="{self._url(";".join(styles))}"' if styles else ""
        level = (
            f'<span class="fan-level">{medal.level}</span>'
            if medal.level is not None
            else ""
        )
        return (
            f'<span class="fan-medal"{style}>'
            f'<span class="fan-name">{self._text(medal.name)}</span>'
            f"{level}</span>"
        )

    def _render_author(self, author: BiliAuthorBadge, *, small: bool) -> str:
        color = (
            f' style="color:{self._url(author.nickname_color)}"'
            if author.nickname_color
            else ""
        )
        name_class = "reply-name" if small else "nickname"
        name = f'<span class="{name_class}"{color}>{self._text(author.nickname)}</span>'
        level = ""
        if author.level is not None:
            senior = " senior" if author.senior else ""
            flash = '<span class="senior-flash">⚡</span>' if author.senior else ""
            level = (
                f'<span class="level level-{author.level}{senior}">'
                f"LV{author.level}{flash}</span>"
            )
        medal = self._render_fan_medal(author.fan_medal)
        up = '<span class="up-badge">UP</span>' if author.is_up else ""
        return f'<div class="author-row">{name}{level}{medal}{up}</div>'

    def _render_rich_text(self, parts: list[BiliRichPart]) -> str:
        output = []
        for part in parts:
            if part.kind == "line-break":
                output.append("<br>")
            elif part.kind == "highlight":
                output.append(f'<span class="highlight">{self._text(part.text)}</span>')
            elif part.kind == "emote" and part.url:
                output.append(
                    f'<img class="emote" src="{self._url(part.url)}" '
                    f'alt="{self._url(part.text)}" '
                    'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                )
            else:
                output.append(f"<span>{self._text(part.text)}</span>")
        return "".join(output)

    def _render_images(self, images: list[str]) -> str:
        return "".join(
            '<div class="comment-image-wrap">'
            f'<img class="comment-image" src="{self._url(url)}" alt="" '
            "onerror=\"this.parentElement.style.display='none'\">"
            "</div>"
            for url in images
            if url
        )

    def _render_decor(self, decor: BiliCommentDecor | None) -> str:
        if decor is None:
            return ""
        image = (
            '<span class="decor-image">'
            f'<img src="{self._url(decor.image)}" alt="" '
            "onerror=\"this.parentElement.style.display='none'\"></span>"
            if decor.image
            else ""
        )
        label = decor.text
        if decor.number:
            label = f"{decor.prefix}{decor.number}"
        color = f' style="color:{self._url(decor.color)}"' if decor.color else ""
        text = (
            f'<span class="decor-text"{color}>{self._text(label)}</span>'
            if label
            else ""
        )
        return f'<div class="decor">{image}{text}</div>' if image or text else ""

    def _render_entry(self, entry: BiliCommentEntry, *, nested: bool) -> str:
        avatar = self._render_avatar(entry.author, small=nested)
        author = self._render_author(entry.author, small=nested)
        pinned = '<span class="pinned">置顶</span>' if entry.pinned else ""
        rich = self._render_rich_text(entry.content)
        images = self._render_images(entry.images)
        metadata = [entry.time_text, entry.location, *entry.meta_items]
        metadata_text = " · ".join(item for item in metadata if item)
        action_meta = (
            f'<span class="action-meta">{self._text(metadata_text)}</span>'
            if metadata_text
            else ""
        )
        up_liked = '<div class="up-liked">UP主觉得很赞</div>' if entry.up_liked else ""
        actions = (
            '<div class="actions">'
            f"{action_meta}"
            f'<span class="action">{_LIKE_ICON}'
            f"{self._text(entry.like_text)}</span>"
            f'<span class="action">{_REPLY_ICON}{self._text(entry.reply_text)}</span>'
            "</div>"
        )
        decor = "" if nested else self._render_decor(entry.decor)

        if nested:
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="reply-body">{author}'
                f'<div class="reply-content">{rich}</div>{images}'
                f"{actions}{up_liked}</div></div>"
            )

        first_reply = (
            self._render_entry(entry.first_reply, nested=True)
            if entry.first_reply is not None
            else ""
        )
        return (
            '<article class="comment-card">'
            f'{avatar}<section class="comment-body">'
            f'<div class="comment-head">{author}{decor}</div>'
            f'<div class="comment-content">{pinned}{rich}</div>'
            f"{images}{actions}{up_liked}{first_reply}</section></article>"
        )

    @staticmethod
    def _footer_label(document: BiliCommentDocument) -> str:
        custom = (document.footer_text or "").replace(
            COMMENT_FOOTER_BRAND,
            "",
        )
        custom = custom.strip(" ·")
        if "仅展示部分热门评论" in custom:
            return "仅展示部分热门评论"
        return f"热门评论 {len(document.entries)} / {document.total_text}"

    def render_entries_fragment(self, document: BiliCommentDocument) -> str:
        """Render Bilibili comment rows without the standalone shell."""
        return "".join(
            self._render_entry(entry, nested=False) for entry in document.entries
        )

    @classmethod
    def footer_label(cls, document: BiliCommentDocument) -> str:
        return cls._footer_label(document)

    def build_html(self, document: BiliCommentDocument) -> str:
        theme = BILIBILI_CARD_THEME
        cover = (
            f'<img class="cover" src="{self._url(document.cover)}" alt="" '
            "onerror=\"this.style.display='none'\">"
            if document.cover
            else ""
        )
        entries = self.render_entries_fragment(document)
        footer_label = self._text(self.footer_label(document))
        footer_brand = self._text(COMMENT_FOOTER_BRAND)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-comment-styles">
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;background:{theme.background};color:{theme.text}}}html{{overflow-x:hidden;scrollbar-width:none}}html::-webkit-scrollbar{{width:0;height:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.page{{width:760px;padding:18px 22px 20px;background:{theme.background}}}.shell{{overflow:hidden;border:1px solid {theme.border};border-radius:14px;background:{theme.surface}}}
.header{{display:flex;min-height:82px;align-items:center;gap:12px;padding:15px 16px 11px}}
.brand{{display:grid;width:34px;height:34px;place-items:center;flex:0 0 34px;border-radius:50%;background:{theme.accent};color:#fff;font-size:16px;font-weight:800}}
.header-copy{{min-width:0;flex:1}}.header-copy h1{{margin:0;overflow:hidden;font-size:21px;font-weight:700;line-height:1.42;text-overflow:ellipsis;white-space:nowrap}}
.header-copy p{{margin:3px 0 0;color:{theme.muted};font-size:13px;line-height:1.45}}.cover{{width:88px;height:54px;border-radius:9px;object-fit:cover;background:{theme.subtle}}}
.comment-list{{padding:0 16px}}.comment-card{{position:relative;display:grid;grid-template-columns:44px 1fr;gap:12px;padding:15px 0;border-top:1px solid {theme.border}}}
.avatar,.reply-avatar{{position:relative;display:grid;place-items:center;overflow:hidden;border-radius:50%;background:{theme.accent_soft};color:{theme.accent};font-weight:800}}.avatar-shell img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.avatar{{width:44px;height:44px;font-size:17px}}.reply-avatar{{width:28px;height:28px;font-size:12px}}.comment-body,.reply-body{{min-width:0}}
.comment-head{{display:flex;min-height:22px;justify-content:space-between;gap:12px}}.author-row{{display:flex;min-width:0;align-items:center;gap:5px;flex-wrap:wrap}}
.nickname,.reply-name{{max-width:300px;overflow:hidden;color:{theme.text};font-size:16px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}}.reply-name{{max-width:240px;font-size:14px}}
.level{{height:17px;padding:0 4px;border-radius:3px;background:#c9ccd0;color:#fff;font-size:11px;font-weight:700;line-height:17px}}
.level-2{{background:#8cd49c}}.level-3{{background:#7cccec}}.level-4{{background:#fbbc8c}}.level-5{{background:#ec642c}}.level-6{{background:#f34c4c}}.senior-flash{{font-size:10px}}
.fan-medal{{display:inline-flex;height:18px;max-width:120px;overflow:hidden;border:1px solid var(--medal-border,#ff6699);border-radius:3px;font-size:11px;line-height:16px}}
.fan-name{{max-width:88px;overflow:hidden;padding:0 4px;background:var(--medal-bg,#ff6699);color:var(--medal-fg,#fff);text-overflow:ellipsis;white-space:nowrap}}
.fan-level{{min-width:18px;padding:0 3px;background:var(--medal-level-bg,#fff);color:var(--medal-level-fg,#ff6699);text-align:center}}.up-badge{{height:18px;padding:0 5px;border-radius:999px;background:{theme.accent_soft};color:{theme.accent};font-size:11px;line-height:18px}}
.decor{{display:flex;max-width:115px;align-items:center;gap:3px;color:{theme.accent};font-size:10px;font-weight:700}}.decor-image{{width:34px;height:28px;overflow:hidden}}.decor-image img{{width:auto;height:38px;transform:translate(-55%,-5px)}}
.comment-content,.reply-content{{margin-top:6px;color:{theme.text};font-size:18px;line-height:1.62;word-break:break-word}}.reply-content{{font-size:16px}}.highlight{{color:#00aeec}}
.emote{{display:inline-block;width:23px;height:23px;margin:0 2px;object-fit:contain;vertical-align:-5px}}.pinned{{display:inline-block;margin-right:7px;padding:0 6px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent};font-size:12px;line-height:21px;vertical-align:2px}}
.comment-image-wrap{{display:block;width:fit-content;max-width:100%;margin:9px 0 0;overflow:hidden;border:1px solid {theme.border};border-radius:8px;background:{theme.subtle}}}.comment-image{{display:block;width:auto;height:auto;max-width:540px;object-fit:contain}}
.actions{{display:flex;align-items:center;gap:15px;margin-top:7px;color:{theme.muted};font-size:13px;line-height:20px;flex-wrap:wrap}}.action-meta{{margin-right:auto}}.action{{display:inline-flex;align-items:center;gap:4px}}.action-icon{{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}
.up-liked{{display:inline-block;margin-top:7px;padding:1px 7px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent};font-size:12px}}
.reply-card{{display:grid;grid-template-columns:28px 1fr;gap:9px;max-width:590px;margin-top:11px;padding:4px 0 4px 12px;border-left:2px solid {theme.border}}}
.reply-card .actions{{gap:12px}}.footer{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px 15px;border-top:1px solid {theme.border};color:{theme.muted};font-size:12px;line-height:1.45;flex-wrap:wrap}}.footer-brand{{color:{theme.accent};overflow-wrap:anywhere;word-break:break-all}}
</style>
</head>
<body><div id="parser-x-comment-root" class="page"><div class="shell">
<header class="header"><div class="brand">B</div><div class="header-copy">
<h1>{self._text(document.work_title or "B站视频")}</h1>
<p>B站 · {self._text(document.total_text)}</p>
</div>{cover}</header><main class="comment-list">{entries}</main><footer class="footer"><span>{footer_label}</span><span class="footer-brand">{footer_brand}</span></footer>
</div></div></body></html>"""

    async def render(self, out_path: Path, document: BiliCommentDocument) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html = self.build_html(document)

        await self.render_service.render(
            out_path,
            self._scale_for_astrbot_canvas(html),
            options={
                "type": "jpeg",
                "quality": self.render_service.jpeg_quality,
                "full_page": True,
                "scale": "css",
            },
            target_width=1140,
            fallback_width=760,
            bottom_padding=20,
        )

    @staticmethod
    def _scale_for_astrbot_canvas(html: str) -> str:
        """Fill AstrBot's 1280px Canvas viewport without changing local fallback."""
        return html.replace(
            '<style id="parser-x-comment-styles">',
            '<style id="parser-x-comment-styles">\n'
            "@media (min-width:1000px){html,body{width:1140px}"
            "#parser-x-comment-root{transform:scale(1.5);transform-origin:0 0}}",
            1,
        )


__all__ = [
    "BiliAuthorBadge",
    "BiliCommentCanvas",
    "BiliCommentDecor",
    "BiliCommentDocument",
    "BiliCommentEntry",
    "BiliFanMedal",
    "BiliRichPart",
]
