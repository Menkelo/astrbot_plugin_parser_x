from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommentCanvasStyle:
    background: str
    surface: str
    nested_surface: str
    header: str
    header_text: str
    header_muted: str
    text: str
    muted: str
    border: str
    accent: str
    accent_soft: str


UNIFIED_COMMENT_STYLE = CommentCanvasStyle(
    background="#f2f4f7",
    surface="#ffffff",
    nested_surface="#f7f8fa",
    header="#282c36",
    header_text="#fafafc",
    header_muted="rgba(255,255,255,.64)",
    text="#1e2127",
    muted="#707682",
    border="#e2e5eb",
    accent="#5963d9",
    accent_soft="#eef0ff",
)


COMMENT_HEADER_ICON = (
    '<svg class="header-icon" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M8 19H5l-3 3V6a4 4 0 0 1 4-4h9a4 4 0 0 1 4 4v1"/>'
    '<path d="M9 11a4 4 0 0 1 4-4h5a4 4 0 0 1 4 4v10l-3-3h-6a4 4 0 0 1-4-4Z"/>'
    "</svg>"
)


def standalone_comment_css(*, extra_css: str = "") -> str:
    style = UNIFIED_COMMENT_STYLE
    return f"""
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;background:{style.background};color:{style.text}}}html{{overflow-x:hidden;scrollbar-width:none}}html::-webkit-scrollbar{{width:0;height:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.page{{width:760px;padding:18px 22px 20px;background:{style.background}}}.shell{{overflow:hidden;border:1px solid {style.border};border-radius:14px;background:{style.surface};box-shadow:0 12px 30px rgba(30,35,48,.10)}}
.header{{display:flex;min-height:58px;align-items:center;gap:10px;padding:0 16px;background:{style.header};color:{style.header_text}}}
.brand{{display:grid;width:32px;height:32px;place-items:center;flex:0 0 32px;border-radius:9px;background:rgba(255,255,255,.12)}}.header-icon{{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}
.header-copy{{min-width:0;flex:1}}.header-copy h1{{margin:0;font-size:15px;font-weight:700;line-height:1.35}}.header-copy p{{margin:2px 0 0;color:{style.header_muted};font-size:10px;line-height:1.25;letter-spacing:.06em}}.header-count{{color:{style.header_muted};font-size:12px;white-space:nowrap}}
.comment-list{{padding:4px 16px 9px}}.comment-card{{position:relative;display:grid;grid-template-columns:42px 1fr;gap:11px;padding:15px 0}}.comment-card+.comment-card{{border-top:1px solid {style.border}}}
.avatar-shell{{position:relative;display:grid;place-items:center;overflow:hidden;border:2px solid {style.surface};border-radius:50%;background:{style.accent_soft};color:{style.accent};font-weight:800;box-shadow:0 0 0 1px {style.border}}}.avatar-shell img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.avatar{{width:42px;height:42px;font-size:16px}}.reply-avatar{{width:28px;height:28px;font-size:12px}}.comment-body,.reply-body{{min-width:0}}
.comment-head{{display:flex;min-height:21px;align-items:flex-start;justify-content:space-between;gap:10px}}.author-row{{display:flex;min-width:0;min-height:21px;align-items:center;gap:5px;flex-wrap:wrap}}.comment-aside{{display:grid;justify-items:end;gap:2px}}.comment-meta{{margin-left:auto;color:{style.muted};font-size:11px;line-height:1.45;white-space:nowrap}}
.nickname,.reply-name{{max-width:310px;overflow:hidden;color:{style.text};font-size:16px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}}.reply-name{{max-width:245px;font-size:14px}}.author-badge,.up-badge{{display:inline-flex;align-items:center;padding:1px 6px;border:1px solid transparent;border-radius:5px;background:{style.accent_soft};color:{style.accent};font-size:11px;font-weight:700;line-height:16px}}
.comment-content,.reply-content{{margin-top:6px;color:{style.text};font-size:18px;line-height:1.62;word-break:break-word}}.reply-content{{font-size:16px}}.highlight{{color:{style.accent}}}.emoji-text{{display:inline-block;margin:0 2px;padding:0 4px;border-radius:5px;background:{style.accent_soft};color:{style.muted}}}.emote{{display:inline-block;width:24px;height:24px;margin:0 2px;object-fit:contain;vertical-align:-6px}}.pinned{{display:inline-block;margin-right:7px;padding:1px 7px;border-radius:5px;background:{style.accent_soft};color:{style.accent};font-size:12px;line-height:21px;vertical-align:2px}}
.comment-image-wrap{{display:block;width:fit-content;max-width:100%;margin:9px 0 0;overflow:hidden;border:1px solid {style.border};border-radius:8px;background:{style.nested_surface}}}.sticker-image-wrap{{display:block;width:fit-content;max-width:100%;margin:9px 0 0;overflow:hidden;border:1px solid {style.border};border-radius:8px;background:{style.nested_surface}}}.comment-image{{display:block;width:auto;height:auto;max-width:540px;object-fit:contain}}.sticker-image{{display:block;width:auto;height:auto;max-width:180px;max-height:180px;object-fit:contain}}
.actions{{display:flex;align-items:center;justify-content:flex-end;gap:15px;margin-top:7px;color:{style.muted};font-size:13px;line-height:20px;flex-wrap:wrap}}.action{{display:inline-flex;align-items:center;gap:4px}}.action-icon{{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}.creator-liked{{margin-right:auto;padding:1px 6px;border-radius:5px;background:{style.accent_soft};color:{style.accent}}}
.reply-card{{display:grid;grid-template-columns:28px 1fr;gap:9px;max-width:600px;margin-top:11px;padding:10px 11px;border-left:3px solid {style.accent};border-radius:0 10px 10px 0;background:{style.nested_surface}}}.reply-card .actions{{gap:12px}}.up-liked{{display:inline-block;margin-top:7px;padding:1px 7px;border-radius:5px;background:{style.accent_soft};color:{style.accent};font-size:12px}}
.footer{{display:grid;justify-items:center;gap:2px;padding:11px 16px 13px;border-top:1px solid {style.border};color:{style.muted};font-size:11px;line-height:1.45}}.footer-label:empty{{display:none}}.footer-brand{{overflow-wrap:anywhere;word-break:break-all}}
{extra_css}
""".strip()


__all__ = [
    "COMMENT_HEADER_ICON",
    "CommentCanvasStyle",
    "UNIFIED_COMMENT_STYLE",
    "standalone_comment_css",
]
