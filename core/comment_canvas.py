from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Literal

from .card_theme import (
    DOUYIN_CARD_THEME,
    MIYOUSHE_CARD_THEME,
    WEIBO_CARD_THEME,
    XIAOHEIHE_CARD_THEME,
    PlatformCardTheme,
)
from .comment_style import COMMENT_HEADER_ICON, standalone_comment_css
from .constants import COMMENT_FOOTER_BRAND
from .html_renderer import HtmlRenderService
from .unicode_emoji import render_unicode_emoji_html

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
class CommentTheme:
    platform_name: str
    brand_text: str
    accent: str
    accent_soft: str
    background: str
    surface: str
    nested_surface: str
    text: str
    muted: str
    border: str
    dark: bool = False
    portrait_cover: bool = False


def _comment_theme(
    theme: PlatformCardTheme,
    *,
    portrait_cover: bool = False,
) -> CommentTheme:
    return CommentTheme(
        platform_name=theme.display_name,
        brand_text=theme.glyph,
        accent=theme.accent,
        accent_soft=theme.accent_soft,
        background=theme.background,
        surface=theme.surface,
        nested_surface=theme.subtle,
        text=theme.text,
        muted=theme.muted,
        border=theme.border,
        portrait_cover=portrait_cover,
    )


DOUYIN_THEME = _comment_theme(DOUYIN_CARD_THEME, portrait_cover=True)
WEIBO_THEME = _comment_theme(WEIBO_CARD_THEME)
XIAOHEIHE_THEME = _comment_theme(XIAOHEIHE_CARD_THEME)
MIYOUSHE_THEME = _comment_theme(MIYOUSHE_CARD_THEME)


@dataclass(slots=True)
class CommentRichPart:
    kind: Literal["text", "line-break", "highlight", "emote", "emoji-text"]
    text: str = ""
    url: str = ""


@dataclass(slots=True)
class CommentBadge:
    text: str
    foreground: str = ""
    background: str = ""
    border: str = ""


@dataclass(slots=True)
class CommentAuthor:
    nickname: str
    avatar: str = ""
    nickname_color: str = ""
    badges: list[CommentBadge] = field(default_factory=list)


@dataclass(slots=True)
class CommentEntry:
    author: CommentAuthor
    content: list[CommentRichPart]
    images: list[str] = field(default_factory=list)
    sticker_image: str = ""
    time_text: str = ""
    location: str = ""
    like_text: str = "0"
    reply_text: str = "回复"
    pinned: bool = False
    creator_liked: bool = False
    meta_items: list[str] = field(default_factory=list)
    first_reply: CommentEntry | None = None


@dataclass(slots=True)
class CommentDocument:
    theme: CommentTheme
    work_title: str
    cover: str
    total_text: str
    entries: list[CommentEntry]
    footer_text: str = ""


class SocialCommentCanvas:
    """Platform-neutral Canvas renderer for domestic social comment feeds."""

    def __init__(
        self,
        render_service: HtmlRenderService | None = None,
    ):
        self.render_service = render_service or HtmlRenderService()

    def bind(self, html_render) -> None:
        self.render_service.bind(html_render)

    @staticmethod
    def _escape_jinja(value: str) -> str:
        # html_render treats the supplied HTML as a Jinja template. Encoding braces
        # prevents user comments such as ``{{ ... }}`` from becoming template code.
        return value.replace("{", "&#123;").replace("}", "&#125;")

    @classmethod
    def _text(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or "")))

    @classmethod
    def _url(cls, value: object) -> str:
        return cls._escape_jinja(escape(str(value or ""), quote=True))

    @staticmethod
    def _initial(nickname: str, fallback: str) -> str:
        value = (nickname or "").strip()
        return value[:1] or fallback

    def _render_avatar(
        self,
        author: CommentAuthor,
        *,
        small: bool,
        fallback: str,
    ) -> str:
        size_class = "reply-avatar" if small else "avatar"
        initial = self._text(self._initial(author.nickname, fallback))
        image = ""
        if author.avatar:
            image = (
                f'<img src="{self._url(author.avatar)}" alt="" '
                "onerror=\"this.style.display='none'\">"
            )
        return (
            f'<div class="{size_class} avatar-shell">'
            f"<span>{initial}</span>{image}</div>"
        )

    def _render_badges(self, badges: list[CommentBadge]) -> str:
        output = []
        for badge in badges:
            styles = []
            if badge.foreground:
                styles.append(f"color:{badge.foreground}")
            if badge.background:
                styles.append(f"background:{badge.background}")
            if badge.border:
                styles.append(f"border-color:{badge.border}")
            style = f' style="{self._url(";".join(styles))}"' if styles else ""
            output.append(
                f'<span class="author-badge"{style}>{self._text(badge.text)}</span>'
            )
        return "".join(output)

    def _render_author(self, author: CommentAuthor, *, small: bool) -> str:
        class_name = "reply-name" if small else "nickname"
        color = (
            f' style="color:{self._url(author.nickname_color)}"'
            if author.nickname_color
            else ""
        )
        return (
            '<div class="author-row">'
            f'<span class="{class_name}"{color}>{self._text(author.nickname)}</span>'
            f"{self._render_badges(author.badges)}</div>"
        )

    def _render_rich_text(self, parts: list[CommentRichPart]) -> str:
        output = []
        for part in parts:
            if part.kind == "line-break":
                output.append("<br>")
            elif part.kind == "highlight":
                output.append(
                    '<span class="highlight">'
                    f"{self._render_unicode_text(part.text)}</span>"
                )
            elif part.kind == "emoji-text":
                output.append(
                    '<span class="emoji-text">'
                    f"{self._render_unicode_text(part.text)}</span>"
                )
            elif part.kind == "emote" and part.url:
                output.append(
                    f'<img class="emote" src="{self._url(part.url)}" '
                    f'alt="{self._url(part.text)}" '
                    'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                )
            else:
                output.append(f"<span>{self._render_unicode_text(part.text)}</span>")
        return "".join(output)

    def _render_unicode_text(self, value: object) -> str:
        return render_unicode_emoji_html(
            str(value or ""),
            escape_text=self._text,
            escape_url=self._url,
        )

    def _render_images(self, images: list[str], *, sticker: bool = False) -> str:
        class_name = "sticker-image" if sticker else "comment-image"
        wrap_class = "sticker-image-wrap" if sticker else "comment-image-wrap"
        return "".join(
            f'<div class="{wrap_class}">'
            f'<img class="{class_name}" src="{self._url(url)}" alt="" '
            "onerror=\"this.parentElement.style.display='none'\">"
            "</div>"
            for url in images
            if url
        )

    def _render_entry(
        self,
        entry: CommentEntry,
        *,
        nested: bool,
        fallback: str,
    ) -> str:
        avatar = self._render_avatar(
            entry.author,
            small=nested,
            fallback=fallback,
        )
        author = self._render_author(entry.author, small=nested)
        pinned = '<span class="pinned">置顶</span>' if entry.pinned else ""
        rich = self._render_rich_text(entry.content)
        images = self._render_images(entry.images)
        sticker = self._render_images(
            [entry.sticker_image] if entry.sticker_image else [],
            sticker=True,
        )
        metadata = [entry.time_text, entry.location, *entry.meta_items]
        metadata_text = " · ".join(item for item in metadata if item)
        metadata_html = (
            f'<span class="comment-meta">{self._text(metadata_text)}</span>'
            if metadata_text
            else ""
        )
        creator_liked = (
            '<span class="creator-liked">作者赞过</span>' if entry.creator_liked else ""
        )
        actions = (
            '<div class="actions">'
            f"{creator_liked}"
            f'<span class="action">{_REPLY_ICON}{self._text(entry.reply_text)}</span>'
            f'<span class="action">{_LIKE_ICON}{self._text(entry.like_text)}</span>'
            "</div>"
        )
        head = f'<div class="comment-head">{author}{metadata_html}</div>'

        if nested:
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="reply-body">{head}'
                f'<div class="reply-content">{rich}</div>{sticker}{images}'
                f"{actions}</div></div>"
            )

        first_reply = (
            self._render_entry(
                entry.first_reply,
                nested=True,
                fallback=fallback,
            )
            if entry.first_reply is not None
            else ""
        )
        return (
            '<article class="comment-card">'
            f'{avatar}<section class="comment-body">{head}'
            f'<div class="comment-content">{pinned}{rich}</div>'
            f"{sticker}{images}{actions}{first_reply}</section></article>"
        )

    @staticmethod
    def _footer_label(document: CommentDocument) -> str:
        custom = (document.footer_text or "").replace(
            COMMENT_FOOTER_BRAND,
            "",
        )
        custom = custom.strip(" ·")
        if "仅展示部分热门评论" in custom:
            return "仅展示部分热门评论"
        return ""

    def render_entries_fragment(self, document: CommentDocument) -> str:
        """Render comment rows without the standalone comment-card shell."""
        fallback = "评"
        return "".join(
            self._render_entry(entry, nested=False, fallback=fallback)
            for entry in document.entries
        )

    @classmethod
    def footer_label(cls, document: CommentDocument) -> str:
        return cls._footer_label(document)

    def build_html(self, document: CommentDocument) -> str:
        entries = self.render_entries_fragment(document)
        footer_label = self._text(self.footer_label(document))
        footer_brand = self._text(COMMENT_FOOTER_BRAND)
        styles = standalone_comment_css()
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-comment-styles">
{styles}
</style></head><body><div id="parser-x-comment-root" class="page"><div class="shell">
<header class="header"><div class="brand">{COMMENT_HEADER_ICON}</div><div class="header-copy">
<h1>热门评论</h1><p>COMMENTS</p></div><span class="header-count">共 {self._text(document.total_text)}</span></header>
<main class="comment-list">{entries}</main><footer class="footer"><span class="footer-label">{footer_label}</span><span class="footer-brand">{footer_brand}</span></footer>
</div></div></body></html>"""

    async def render(self, out_path: Path, document: CommentDocument) -> None:
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
    "CommentAuthor",
    "CommentBadge",
    "CommentDocument",
    "CommentEntry",
    "CommentRichPart",
    "CommentTheme",
    "DOUYIN_THEME",
    "MIYOUSHE_THEME",
    "SocialCommentCanvas",
    "WEIBO_THEME",
    "XIAOHEIHE_THEME",
]
