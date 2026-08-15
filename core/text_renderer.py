import re
from html import escape
from pathlib import Path
from typing import Any

from .card_theme import resolve_card_theme
from .constants import COMMENT_FOOTER_BRAND
from .html_renderer import HtmlRenderService

RICH_TEXT_RE = re.compile(
    r"(?P<topic>#[^#\s\r\n][^#\r\n]{0,60}?#)"
    r"|(?P<mention>[@\uff20][\w\u4e00-\u9fff\u3400-\u4dbf.-]{1,32})"
    r"|(?P<url>https?://[^\s<>()\"']+)"
    r"|(?P<link>\u7f51\u9875\u94fe\u63a5|\u62bd\u5956\u8be6\u60c5)"
)


class TextCardRenderer:
    def __init__(self, render_service: HtmlRenderService):
        self.render_service = render_service

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
    def _render_text_html(cls, text: str) -> str:
        parts: list[str] = []
        last = 0

        for match in RICH_TEXT_RE.finditer(text or ""):
            parts.append(cls._safe_text(text[last : match.start()]))
            parts.append(
                f'<span class="text-link">{cls._safe_text(match.group(0))}</span>'
            )
            last = match.end()

        parts.append(cls._safe_text(text[last:]))
        return "".join(parts)

    @staticmethod
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

    @classmethod
    def _normalise_metrics(cls, metrics: object) -> list[tuple[str, str]]:
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
            value_text = cls._format_metric_value(value)
            if label_text and value_text:
                output.append((label_text, value_text))
            if len(output) >= 6:
                break
        return output

    @staticmethod
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

    @staticmethod
    def supports_comment_document(document: object | None) -> bool:
        if document is None:
            return False

        from .comment_canvas import CommentDocument
        from .parsers.bilibili.comment_canvas import BiliCommentDocument

        return isinstance(document, (BiliCommentDocument, CommentDocument)) and bool(
            document.entries
        )

    def _render_comment_section(self, document: object | None) -> str:
        if not self.supports_comment_document(document):
            return ""

        total_text = str(getattr(document, "total_text", "") or "").strip()
        from .comment_canvas import CommentDocument, SocialCommentCanvas
        from .parsers.bilibili.comment_canvas import (
            BiliCommentCanvas,
            BiliCommentDocument,
        )

        if isinstance(document, BiliCommentDocument):
            entries_html = BiliCommentCanvas(
                self.render_service
            ).render_entries_fragment(document)
        elif isinstance(document, CommentDocument):
            entries_html = SocialCommentCanvas(
                self.render_service
            ).render_entries_fragment(document)
        else:  # pragma: no cover - guarded by supports_comment_document
            return ""

        if not entries_html:
            return ""

        entries_count = len(document.entries)
        summary = f"展示 {entries_count}"
        if total_text:
            summary += f" / {total_text}"
        return (
            '<section class="comments-block">'
            '<div class="section-head"><h2>热门评论</h2>'
            f"<span>{self._safe_text(summary)}</span></div>"
            f'<div class="comment-list">{entries_html}</div></section>'
        )

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
        comment_document: object | None = None,
    ):
        theme = resolve_card_theme(platform_key, platform_name)
        display_name = str(platform_name or theme.display_name).strip()
        card_title = str(title or "").strip() or f"{display_name}内容"

        profile_html = ""
        if author_name:
            initial = self._safe_text(str(author_name).strip()[:1] or theme.glyph)
            avatar_image = ""
            if author_avatar:
                avatar_image = (
                    f'<img src="{self._safe_url(author_avatar)}" alt="" '
                    "onerror=\"this.style.display='none'\">"
                )
            profile_html = (
                '<section class="profile-block"><div class="profile">'
                f'<div class="profile-avatar"><span>{initial}</span>{avatar_image}</div>'
                '<div class="profile-copy"><div class="profile-name">'
                f'<span class="author">{self._safe_text(author_name)}</span>'
                + (
                    f'<span class="profile-badge">{self._safe_text(author_badge)}</span>'
                    if author_badge
                    else ""
                )
                + f'</div><span class="profile-meta">来自 {self._safe_text(display_name)}</span>'
                "</div></div></section>"
            )

        meta_parts = []
        if timestamp_text:
            meta_parts.append(str(timestamp_text))
        if content_kind:
            meta_parts.append(str(content_kind))
        if not meta_parts:
            meta_parts.append(display_name)
        meta_text = " · ".join(meta_parts)
        text_html = self._render_text_html(text)
        text_block = (
            f'<section class="copy-block"><div class="text">{text_html}</div></section>'
            if text
            else ""
        )

        metric_items = self._normalise_metrics(metrics)
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

        normalised_info = self._normalise_info_items(info_items)
        info_html = ""
        if normalised_info:
            chips = "".join(
                f'<span class="info-chip">{self._safe_text(item)}</span>'
                for item in normalised_info
            )
            info_html = (
                '<section class="info-block"><div class="section-head">'
                f"<h2>{self._safe_text(info_title or '作品信息')}</h2></div>"
                f'<div class="info-chips">{chips}</div></section>'
            )

        media_html = ""
        if media_url:
            fit_class = " media-contain" if media_fit == "contain" else ""
            media_html = (
                '<div class="hero">'
                f'<img class="hero-image{fit_class}" src="{self._safe_url(media_url)}" '
                'alt="" onerror="this.parentElement.style.display=\'none\'">'
                "</div>"
            )

        comments_html = self._render_comment_section(comment_document)

        html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-content-card-styles">
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;background:{theme.background};color:{theme.text}}}html{{overflow-x:hidden;scrollbar-width:none}}html::-webkit-scrollbar{{width:0;height:0}}
body{{padding:18px 22px 20px;overflow-x:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.card{{width:716px;overflow:hidden;border:1px solid {theme.border};border-radius:14px;background:{theme.surface}}}
.brand-bar{{display:flex;min-height:64px;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;background:{theme.accent};color:#fff}}
.brand-copy{{display:flex;min-width:0;align-items:center;gap:9px}}.platform-mark{{display:grid;width:34px;height:34px;place-items:center;flex:0 0 34px;border:1px solid rgba(255,255,255,.55);border-radius:10px;color:#fff;font-size:16px;font-weight:800}}
.brand-name{{overflow:hidden;font-size:18px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}}.product-name{{font-size:13px;opacity:.9;white-space:nowrap}}
.hero{{width:100%;max-height:430px;overflow:hidden;border-bottom:1px solid {theme.border};background:{theme.subtle}}}.hero-image{{display:block;width:100%;min-height:240px;max-height:430px;object-fit:cover}}.hero-image.media-contain{{object-fit:contain}}
.primary-block{{display:grid;gap:10px;padding:19px 20px 17px}}.primary-block h1{{margin:0;overflow-wrap:anywhere;color:{theme.text};font-size:25px;font-weight:700;line-height:1.42}}.meta{{color:{theme.muted};font-size:13px;line-height:1.45}}
.metrics,.info-chips{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}.metric,.info-chip{{display:inline-flex;align-items:center;gap:5px;padding:5px 9px;border-radius:999px;background:{theme.subtle};color:{theme.muted};font-size:13px;line-height:1.45}}.metric strong{{color:{theme.text};font-size:14px}}
.profile-block,.copy-block,.info-block,.comments-block,.footer{{border-top:1px solid {theme.border}}}.profile-block{{padding:15px 20px}}.profile{{display:flex;align-items:center;gap:10px}}.profile-avatar{{position:relative;display:grid;width:40px;height:40px;place-items:center;flex:0 0 40px;overflow:hidden;border:1px solid {theme.border};border-radius:50%;background:{theme.accent_soft};color:{theme.accent};font-size:14px;font-weight:800}}
.profile-avatar img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}.profile-copy{{display:grid;min-width:0;gap:2px}}.profile-name{{display:flex;min-width:0;align-items:center;gap:7px;flex-wrap:wrap}}.author{{max-width:440px;overflow:hidden;color:{theme.text};font-size:16px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}}.profile-badge{{padding:1px 7px;border-radius:999px;background:{theme.accent_soft};color:{theme.accent};font-size:11px;font-weight:700;line-height:18px}}.profile-meta{{color:{theme.muted};font-size:12px}}
.copy-block{{padding:17px 20px 18px}}.text{{color:{theme.text};font-size:18px;line-height:1.72;white-space:pre-wrap;word-break:break-word}}.text-link{{color:{theme.accent};font-weight:650}}
.info-block,.comments-block{{padding:17px 20px}}.section-head{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.section-head h2{{margin:0;color:{theme.text};font-size:19px;line-height:1.45}}.section-head>span{{color:{theme.muted};font-size:12px}}.info-chips{{margin-top:11px}}.info-chip{{border:1px solid {theme.border}}}
.comment-list{{margin-top:4px}}.comment-card{{position:relative;display:grid;grid-template-columns:42px 1fr;gap:11px;padding:15px 0;border-top:1px solid {theme.border}}}.avatar-shell{{position:relative;display:grid;place-items:center;overflow:hidden;border-radius:50%;background:{theme.accent_soft};color:{theme.accent};font-weight:800}}.avatar-shell img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}.avatar{{width:42px;height:42px;font-size:16px}}.reply-avatar{{width:28px;height:28px;font-size:12px}}.comment-body,.reply-body{{min-width:0}}.comment-head{{display:flex;min-height:22px;justify-content:space-between;gap:10px}}
.author-row{{display:flex;min-height:21px;min-width:0;align-items:center;gap:5px;flex-wrap:wrap}}.nickname,.reply-name{{max-width:310px;overflow:hidden;color:{theme.text};font-size:16px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}}.reply-name{{max-width:250px;font-size:14px}}.author-badge,.up-badge{{display:inline-flex;align-items:center;padding:1px 6px;border:1px solid transparent;border-radius:999px;background:{theme.accent_soft};color:{theme.accent};font-size:11px;font-weight:700;line-height:16px}}
.comment-content,.reply-content{{margin-top:6px;color:{theme.text};font-size:17px;line-height:1.62;word-break:break-word}}.reply-content{{font-size:15px}}.highlight{{color:{theme.accent}}}.emoji-text{{display:inline-block;margin:0 2px;padding:0 4px;border-radius:5px;background:{theme.accent_soft};color:{theme.muted}}}.emote{{display:inline-block;width:23px;height:23px;margin:0 2px;object-fit:contain;vertical-align:-5px}}.pinned{{display:inline-block;margin-right:7px;padding:0 6px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent};font-size:12px;line-height:21px;vertical-align:2px}}
.comment-image-wrap,.sticker-image-wrap{{display:block;width:fit-content;max-width:100%;margin:9px 0 0;overflow:hidden;border:1px solid {theme.border};border-radius:8px;background:{theme.subtle}}}.comment-image{{display:block;width:auto;height:auto;max-width:520px;object-fit:contain}}.sticker-image{{display:block;width:auto;height:auto;max-width:170px;max-height:170px;object-fit:contain}}
.actions{{display:flex;align-items:center;gap:14px;margin-top:7px;color:{theme.muted};font-size:13px;line-height:20px;flex-wrap:wrap}}.action{{display:inline-flex;align-items:center;gap:4px}}.action-icon{{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}.action-meta{{margin-right:auto}}.creator-liked,.up-liked{{display:inline-block;padding:1px 6px;border-radius:5px;background:{theme.accent_soft};color:{theme.accent};font-size:12px}}.reply-card{{display:grid;grid-template-columns:28px 1fr;gap:9px;max-width:590px;margin-top:11px;padding:4px 0 4px 12px;border-left:2px solid {theme.border}}}.reply-card .actions{{gap:12px}}
.level{{height:17px;padding:0 4px;border-radius:3px;background:#c9ccd0;color:#fff;font-size:11px;font-weight:700;line-height:17px}}.level-2{{background:#8cd49c}}.level-3{{background:#7cccec}}.level-4{{background:#fbbc8c}}.level-5{{background:#ec642c}}.level-6{{background:#f34c4c}}.senior-flash{{font-size:10px}}.fan-medal{{display:inline-flex;height:18px;max-width:120px;overflow:hidden;border:1px solid var(--medal-border,#ff6699);border-radius:3px;font-size:11px;line-height:16px}}.fan-name{{max-width:88px;overflow:hidden;padding:0 4px;background:var(--medal-bg,#ff6699);color:var(--medal-fg,#fff);text-overflow:ellipsis;white-space:nowrap}}.fan-level{{min-width:18px;padding:0 3px;background:var(--medal-level-bg,#fff);color:var(--medal-level-fg,#ff6699);text-align:center}}.decor{{display:flex;max-width:115px;align-items:center;gap:3px;color:{theme.accent};font-size:10px;font-weight:700}}.decor-image{{width:34px;height:28px;overflow:hidden}}.decor-image img{{width:auto;height:38px;transform:translate(-55%,-5px)}}
.footer{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 20px 15px;color:{theme.muted};font-size:12px;line-height:1.45;flex-wrap:wrap}}.footer-brand{{color:{theme.accent};overflow-wrap:anywhere;word-break:break-all}}
</style></head><body><article class="card" data-card-style="minimal-feed">
<header class="brand-bar"><div class="brand-copy"><div class="platform-mark">{self._safe_text(theme.glyph)}</div><div class="brand-name">{self._safe_text(display_name)}</div></div><div class="product-name">Parser X · 统一长卡</div></header>
{media_html}<section class="primary-block"><h1>{self._safe_text(card_title)}</h1><div class="meta">{self._safe_text(meta_text)}</div>{metric_html}</section>
{profile_html}{text_block}{info_html}{comments_html}<footer class="footer"><span>Parser X · 跨平台内容解析</span><span class="footer-brand">{self._safe_text(COMMENT_FOOTER_BRAND)}</span></footer>
</article></body></html>"""

        return html

    async def render_text_card(
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
        comment_document: object | None = None,
    ):
        html = self.build_html(
            platform_name=platform_name,
            platform_key=platform_key,
            author_name=author_name,
            author_avatar=author_avatar,
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
        )
        return await self.render_service.render(
            out_path,
            html,
            options={"type": "png", "full_page": True, "scale": "css"},
            target_width=760,
            bottom_padding=20,
        )
