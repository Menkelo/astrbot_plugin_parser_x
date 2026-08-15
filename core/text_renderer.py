import re
from html import escape
from pathlib import Path

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
                '<div class="profile">'
                f'<div class="avatar"><span>{initial}</span>{avatar_image}</div>'
                f'<div class="author">{self._safe_text(author_name)}</div></div>'
            )

        meta_parts = [display_name]
        if timestamp_text:
            meta_parts.append(str(timestamp_text))
        meta_text = " · ".join(meta_parts)
        text_html = self._render_text_html(text)
        text_block = f'<div class="text">{text_html}</div>' if text else ""

        html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style id="parser-x-content-card-styles">
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;background:{theme.background};color:{theme.text}}}
body{{padding:18px 22px 20px;overflow-x:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.card{{width:716px;overflow:hidden;border:1px solid {theme.border};border-radius:14px;background:{theme.surface}}}
.head{{display:flex;align-items:center;gap:12px;padding:16px 18px 13px}}
.platform-mark{{display:grid;width:34px;height:34px;place-items:center;flex:0 0 34px;border-radius:50%;background:{theme.accent};color:#fff;font-size:16px;font-weight:800}}
.head-copy{{min-width:0;flex:1}}.head h1{{margin:0;overflow-wrap:anywhere;color:{theme.text};font-size:21px;font-weight:700;line-height:1.42}}
.meta{{margin-top:3px;color:{theme.muted};font-size:13px;line-height:1.45}}
.body{{display:grid;gap:13px;padding:15px 18px 18px;border-top:1px solid {theme.border}}}
.profile{{display:flex;align-items:center;gap:10px}}.avatar{{position:relative;display:grid;width:36px;height:36px;place-items:center;flex:0 0 36px;overflow:hidden;border-radius:50%;background:{theme.accent_soft};color:{theme.accent};font-size:14px;font-weight:800}}
.avatar img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}.author{{min-width:0;overflow:hidden;color:{theme.text};font-size:16px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}}
.text{{color:{theme.text};font-size:18px;line-height:1.72;white-space:pre-wrap;word-break:break-word}}.text-link{{color:{theme.accent};font-weight:650}}
.footer{{display:flex;align-items:center;justify-content:flex-end;padding-top:12px;border-top:1px solid {theme.border};color:{theme.accent};font-size:12px;line-height:1.45;overflow-wrap:anywhere;word-break:break-all}}
</style></head><body><article class="card" data-card-style="minimal-feed">
<header class="head"><div class="platform-mark">{self._safe_text(theme.glyph)}</div><div class="head-copy">
<h1>{self._safe_text(card_title)}</h1><div class="meta">{self._safe_text(meta_text)}</div></div></header>
<div class="body">{profile_html}{text_block}<footer class="footer">{self._safe_text(COMMENT_FOOTER_BRAND)}</footer></div>
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
    ):
        html = self.build_html(
            platform_name=platform_name,
            platform_key=platform_key,
            author_name=author_name,
            author_avatar=author_avatar,
            title=title,
            text=text,
            timestamp_text=timestamp_text,
        )
        return await self.render_service.render(
            out_path,
            html,
            options={"type": "png", "full_page": True, "scale": "css"},
            target_width=760,
            bottom_padding=20,
        )
