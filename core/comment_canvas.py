from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from shutil import copyfile
from typing import Awaitable, Callable, Literal

from astrbot.api import logger
from playwright.async_api import async_playwright


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


DOUYIN_THEME = CommentTheme(
    platform_name="抖音",
    brand_text="抖",
    accent="#fe2c55",
    accent_soft="rgba(254,44,85,.15)",
    background="#0b0b0d",
    surface="#131316",
    nested_surface="#1c1c20",
    text="#f5f5f6",
    muted="#96969e",
    border="rgba(255,255,255,.09)",
    dark=True,
    portrait_cover=True,
)

WEIBO_THEME = CommentTheme(
    platform_name="微博",
    brand_text="微",
    accent="#ff8200",
    accent_soft="#fff1e4",
    background="#f5f5f5",
    surface="#ffffff",
    nested_surface="#f8f8f8",
    text="#1f1f1f",
    muted="#939393",
    border="#ececec",
)


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
        canvas_render: Callable[..., Awaitable[str]] | None = None,
    ):
        self._canvas_render = canvas_render
        self._render_lock = asyncio.Lock()

    def bind(self, canvas_render: Callable[..., Awaitable[str]] | None) -> None:
        self._canvas_render = canvas_render

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
                output.append(f'<span class="highlight">{self._text(part.text)}</span>')
            elif part.kind == "emoji-text":
                output.append(
                    f'<span class="emoji-text">{self._text(part.text)}</span>'
                )
            elif part.kind == "emote" and part.url:
                output.append(
                    f'<img class="emote" src="{self._url(part.url)}" '
                    f'alt="{self._url(part.text)}" '
                    'onerror="this.replaceWith(document.createTextNode(this.alt))">'
                )
            else:
                output.append(f"<span>{self._text(part.text)}</span>")
        return "".join(output)

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
        creator_liked = (
            '<span class="creator-liked">作者赞过</span>' if entry.creator_liked else ""
        )
        actions = (
            '<div class="actions">'
            f'<span class="action-meta">{self._text(metadata_text)}</span>'
            f'<span class="action">♡ {self._text(entry.like_text)}</span>'
            f'<span class="action">◌ {self._text(entry.reply_text)}</span>'
            f"{creator_liked}</div>"
        )

        if nested:
            return (
                '<div class="reply-card">'
                f'{avatar}<div class="reply-body">{author}'
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
            f'{avatar}<section class="comment-body">{author}'
            f'<div class="comment-content">{pinned}{rich}</div>'
            f"{sticker}{images}{actions}{first_reply}</section></article>"
        )

    def build_html(self, document: CommentDocument) -> str:
        theme = document.theme
        fallback = theme.brand_text
        entries = "".join(
            self._render_entry(entry, nested=False, fallback=fallback)
            for entry in document.entries
        )
        display_text = f"展示 {len(document.entries)} 条"
        cover_class = " cover-portrait" if theme.portrait_cover else ""
        cover = (
            f'<img class="cover{cover_class}" src="{self._url(document.cover)}" '
            'alt="" onerror="this.style.display=\'none\'">'
            if document.cover
            else ""
        )
        footer = self._text(
            document.footer_text or f"Parser X · {theme.platform_name}评论区"
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-comment-styles">
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;background:{theme.background};color:{theme.text}}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.page{{width:760px;padding:18px 22px 20px;background:{theme.background}}}
.shell{{overflow:hidden;border:1px solid {theme.border};border-radius:22px;background:{theme.surface};box-shadow:0 12px 35px rgba(0,0,0,{".24" if theme.dark else ".08"})}}
.header{{display:flex;min-height:96px;align-items:center;gap:14px;padding:17px 19px;border-bottom:1px solid {theme.border}}}
.brand{{display:grid;width:46px;height:46px;place-items:center;flex:0 0 46px;border-radius:14px;background:{theme.accent};color:#fff;font-size:21px;font-weight:800}}
.header-copy{{min-width:0;flex:1}}.header h1{{margin:0;overflow:hidden;font-size:23px;font-weight:680;text-overflow:ellipsis;white-space:nowrap}}
.header p{{margin:6px 0 0;color:{theme.muted};font-size:14px}}.cover{{width:96px;height:60px;border-radius:10px;object-fit:cover;background:{theme.nested_surface}}}.cover-portrait{{width:68px;height:88px;border-radius:12px}}
.comment-list{{padding:0 19px}}.comment-card{{display:grid;grid-template-columns:50px 1fr;gap:14px;padding:20px 0;border-bottom:1px solid {theme.border}}}.comment-card:last-child{{border-bottom:0}}
.avatar-shell{{position:relative;display:grid;place-items:center;overflow:hidden;border-radius:50%;background:{theme.accent_soft};color:{theme.accent};font-weight:800}}.avatar-shell img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.avatar{{width:50px;height:50px;font-size:20px}}.reply-avatar{{width:31px;height:31px;font-size:13px}}.comment-body,.reply-body{{min-width:0}}
.author-row{{display:flex;min-height:22px;align-items:center;gap:6px;flex-wrap:wrap}}.nickname,.reply-name{{max-width:310px;overflow:hidden;color:{theme.muted};font-size:17px;text-overflow:ellipsis;white-space:nowrap}}.reply-name{{max-width:250px;font-size:15px}}
.author-badge{{display:inline-flex;align-items:center;padding:2px 7px;border:1px solid transparent;border-radius:999px;background:{theme.accent};color:#fff;font-size:11px;font-weight:700;line-height:16px}}
.comment-content,.reply-content{{margin-top:8px;color:{theme.text};font-size:19px;line-height:1.62;word-break:break-word}}.reply-content{{font-size:16px}}.highlight{{color:{theme.accent}}}.emoji-text{{display:inline-block;margin:0 2px;padding:0 4px;border-radius:5px;background:{theme.accent_soft};color:{theme.muted}}}
.emote{{display:inline-block;width:24px;height:24px;margin:0 2px;object-fit:contain;vertical-align:-6px}}.pinned{{display:inline-block;margin-right:7px;padding:1px 7px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent};font-size:12px;line-height:21px;vertical-align:2px}}
.comment-image-wrap,.sticker-image-wrap{{display:block;width:fit-content;max-width:100%;margin:10px 0 0;overflow:hidden;border:1px solid {theme.border};border-radius:10px;background:{theme.nested_surface}}}.comment-image{{display:block;width:auto;height:auto;max-width:540px;object-fit:contain}}.sticker-image{{display:block;width:auto;height:auto;max-width:180px;max-height:180px;object-fit:contain}}
.actions{{display:flex;align-items:center;gap:17px;margin-top:9px;color:{theme.muted};font-size:13px;line-height:20px;flex-wrap:wrap}}.action-meta{{margin-right:auto}}.creator-liked{{padding:1px 6px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent}}}
.reply-card{{display:grid;grid-template-columns:31px 1fr;gap:9px;max-width:600px;margin-top:13px;padding:11px 12px;border-radius:12px;background:{theme.nested_surface}}}.reply-card .actions{{gap:12px}}
.footer{{padding:14px 18px 16px;border-top:1px solid {theme.border};color:{theme.muted};font-size:12px;text-align:center}}
</style></head><body><div id="parser-x-comment-root" class="page"><div class="shell">
<header class="header"><div class="brand">{self._text(theme.brand_text)}</div><div class="header-copy">
<h1>{self._text(document.work_title or theme.platform_name + "作品")}</h1>
<p>{self._text(theme.platform_name)}评论 · {self._text(display_text)} · {self._text(document.total_text)}</p>
</div>{cover}</header><main class="comment-list">{entries}</main><footer class="footer">{footer}</footer>
</div></div></body></html>"""

    async def render(self, out_path: Path, document: CommentDocument) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html = self.build_html(document)

        async with self._render_lock:
            if self._canvas_render is not None:
                try:
                    canvas_html = self._scale_for_astrbot_canvas(html)
                    rendered = await self._canvas_render(
                        canvas_html,
                        {},
                        return_url=False,
                        options={
                            "type": "jpeg",
                            "quality": 84,
                            "full_page": True,
                            "scale": "css",
                            "animations": "disabled",
                            "caret": "hide",
                        },
                    )
                    rendered_path = Path(str(rendered))
                    if rendered_path.is_file() and rendered_path.stat().st_size > 0:
                        if rendered_path.resolve() != out_path.resolve():
                            copyfile(rendered_path, out_path)
                        return
                except Exception as exc:
                    logger.warning(
                        f"{document.theme.platform_name}评论 Canvas 渲染失败，"
                        f"回退本地 Chromium: {exc}"
                    )

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(
                        viewport={"width": 760, "height": 100},
                        device_scale_factor=2,
                    )
                    await page.set_content(
                        html,
                        wait_until="networkidle",
                        timeout=20_000,
                    )
                    await page.screenshot(
                        path=str(out_path),
                        type="jpeg",
                        quality=84,
                        full_page=True,
                        animations="disabled",
                        caret="hide",
                    )
                finally:
                    await browser.close()

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
    "SocialCommentCanvas",
    "WEIBO_THEME",
]
