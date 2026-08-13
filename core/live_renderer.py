from html import escape
from pathlib import Path

from .constants import COMMENT_FOOTER_BRAND
from .html_renderer import HtmlRenderService


class LiveCardRenderer:
    def __init__(self, render_service: HtmlRenderService):
        self.render_service = render_service

    async def render_live_card(
        self,
        out_path: Path,
        *,
        platform_name: str,
        title: str,
        streamer_name: str,
        cover: str | None,
        avatar: str | None,
        status_text: str,
        area_text: str | None,
        user_time_text: str | None = None,
    ):
        def esc(value: str | int | None) -> str:
            return escape(str(value or ""))

        cover_html = (
            f'<div class="cover-wrap"><img class="cover live-cover" src="{esc(cover)}" alt=""></div>'
            if cover
            else ""
        )
        avatar_html = (
            f'<img class="avatar" src="{esc(avatar)}" alt="">'
            if avatar
            else '<div class="avatar ph"></div>'
        )
        meta_html = f'<div class="meta">{esc(area_text)}</div>' if area_text else ""
        user_time_html = (
            f'<div class="user-time">{esc(user_time_text)}</div>'
            if user_time_text
            else ""
        )

        html = f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8" />
          <style>
            body {{
              margin: 0; padding: 24px; width: 700px;
              font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
              background: #f4f6fa; color: #1f2329;
            }}
            .card {{
              background: #fff; border-radius: 14px; overflow: hidden;
              box-shadow: 0 10px 30px rgba(31,35,41,.08), 0 2px 8px rgba(31,35,41,.05);
            }}
            .cover-wrap {{
              width: 100%; aspect-ratio: 16 / 9; background: #eef1f5;
              display: flex; align-items: center; justify-content: center; overflow: hidden;
            }}
            .cover {{
              width: 100%; height: 100%; object-fit: cover; object-position: center;
              display: block; background: #eef1f5;
            }}
            .cover.is-portrait {{
              object-fit: contain; width: auto; height: 100%; max-width: 100%;
            }}
            .cover.is-ultrawide {{
              object-fit: contain; width: 100%; height: auto; max-height: 100%;
            }}
            .ph {{ background: linear-gradient(135deg, #eef1f4, #e5e9ef); }}
            .body {{ padding: 14px 16px 16px; }}
            .topline {{
              display: flex; align-items: center; justify-content: space-between; gap: 10px;
              color: #6b7280; font-size: 13px; margin-bottom: 8px;
            }}
            .platform {{ font-weight: 800; color: #2563eb; }}
            .status {{
              padding: 3px 8px; border-radius: 999px; background: #eef6ff;
              color: #2563eb; font-weight: 700;
            }}
            .title {{
              font-size: 20px; font-weight: 800; line-height: 1.35;
              display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
            }}
            .user {{ margin-top: 12px; display: flex; align-items: center; gap: 10px; }}
            .avatar {{
              width: 36px; height: 36px; border-radius: 10px; object-fit: cover; background: #eef1f4;
            }}
            .uname {{ font-size: 16px; font-weight: 700; }}
            .user-time {{ margin-top: 3px; color: #7b8491; font-size: 12px; line-height: 1.35; }}
            .meta {{ margin-top: 10px; color: #6b7280; font-size: 13px; }}
            .footer {{ margin-top: 10px; color: #9aa1ac; font-size: 12px; }}
          </style>
        </head>
        <body>
          <div class="card">
            {cover_html}
            <div class="body">
              <div class="topline">
                <div class="platform">{esc(platform_name)}</div>
                <div class="status">{esc(status_text)}</div>
              </div>
              <div class="title">{esc(title)}</div>
              <div class="user">{avatar_html}<div><div class="uname">{esc(streamer_name)}</div>{user_time_html}</div></div>
              {meta_html}
              <div class="footer">{esc(COMMENT_FOOTER_BRAND)}</div>
            </div>
          </div>
        </body>
        </html>
        """

        return await self.render_service.render(
            out_path,
            html,
            options={"type": "png", "full_page": True, "scale": "css"},
            target_width=748,
            bottom_padding=24,
        )
