from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


class BiliCommentRenderer:
    async def render_merged_comments(
        self,
        out_path: Path,
        comments: list[dict],
        video_title: str,
        video_cover: str | None,
        video_author: str | None = None,
        video_time: str | None = None,
    ):
        comments_html = ""

        for c in comments:
            uname = escape(c.get("uname", "") or "")
            avatar = c.get("avatar", "") or ""
            message = escape(c.get("message", "") or "").replace("\n", "<br>")
            pic = c.get("pic")
            comment_time = escape(c.get("comment_time") or "")

            # 保持你旧版头像渲染逻辑：有 URL 就 img；没有就不渲染默认头像。
            avatar_block = (
                f'<img class="avatar" src="{avatar}" alt="">'
                if avatar
                else '<div class="avatar-empty"></div>'
            )

            time_block = (
                f'<div class="comment-time">{comment_time}</div>'
                if comment_time
                else ""
            )

            img_block = (
                f"""
                <div class="img-box">
                    <img class="comment-img" src="{pic}" alt="">
                </div>
                """
                if pic
                else ""
            )

            comments_html += f"""
            <div class="card">
                <div class="user">
                    {avatar_block}
                    <div class="user-meta">
                        <div class="name">{uname}</div>
                        {time_block}
                    </div>
                </div>
                <div class="text">{message}</div>
                {img_block}
            </div>
            """

        cover_block = (
            f'<img class="v-cover" src="{video_cover}" alt="">' if video_cover else ""
        )

        meta_items = []
        if video_author:
            meta_items.append(f"UP主：{escape(video_author)}")
        if video_time:
            meta_items.append(f"发布：{escape(video_time)}")

        video_meta_html = ""
        if meta_items:
            video_meta_html = f"""
            <div class="v-meta">
                {"<span class='dot'>·</span>".join(meta_items)}
            </div>
            """

        html = f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8"/>
          <style>
            * {{
              box-sizing: border-box;
            }}

            html {{
              margin: 0;
              padding: 0;
              width: 560px;
              background: #f1f2f3;
            }}

            body {{
              margin: 0;
              padding: 0;
              width: 560px;
              min-width: 560px;
              max-width: 560px;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
              background: #f1f2f3;
              color: #222;
              overflow-x: hidden;
            }}

            .safe-area {{
              height: 54px;
              width: 100%;
            }}

            .page {{
              width: 560px;
              padding: 0 30px 30px 30px;
            }}

            .header {{
              width: 500px;
              display: flex;
              align-items: center;
              margin-bottom: 24px;
            }}

            .v-cover {{
              width: 80px;
              height: 50px;
              border-radius: 8px;
              object-fit: cover;
              margin-right: 14px;
              box-shadow: 0 2px 8px rgba(0,0,0,0.1);
              flex-shrink: 0;
              background: transparent;
              display: block;
            }}

            .v-info {{
              min-width: 0;
              flex: 1;
            }}

            .v-title {{
              font-size: 16px;
              font-weight: 800;
              color: #222;
              line-height: 1.4;
              display: -webkit-box;
              -webkit-line-clamp: 2;
              -webkit-box-orient: vertical;
              overflow: hidden;
              word-break: break-word;
            }}

            .v-meta {{
              margin-top: 5px;
              font-size: 11px;
              color: #8a93a0;
              line-height: 1.4;
              display: flex;
              flex-wrap: wrap;
              gap: 5px;
            }}

            .dot {{
              color: #c0c4cc;
              margin: 0 2px;
            }}

            .card {{
              width: 500px;
              background: #fff;
              border-radius: 16px;
              padding: 20px;
              margin-bottom: 16px;
              box-shadow: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
              overflow: hidden;
            }}

            .user {{
              display: flex;
              align-items: center;
              margin-bottom: 12px;
            }}

            .avatar {{
              width: 40px;
              height: 40px;
              border-radius: 8px;
              margin-right: 12px;
              object-fit: cover;
              display: block;
              flex-shrink: 0;
            }}

            .avatar-empty {{
              width: 40px;
              height: 40px;
              margin-right: 12px;
              flex-shrink: 0;
            }}

            .user-meta {{
              min-width: 0;
              flex: 1;
            }}

            .name {{
              font-weight: 800;
              font-size: 15px;
              color: #333;
              line-height: 1.25;
              word-break: break-all;
            }}

            .comment-time {{
              margin-top: 4px;
              font-size: 11px;
              color: #9aa1ac;
              line-height: 1.2;
            }}

            .text {{
              font-size: 16px;
              line-height: 1.6;
              color: #222;
              white-space: pre-wrap;
              word-break: break-word;
            }}

            .img-box {{
              margin-top: 12px;
              background: #f7f8fa;
              border-radius: 8px;
              overflow: hidden;
              width: 100%;
            }}

            .comment-img {{
              width: 100%;
              height: auto;
              max-height: none;
              object-fit: contain;
              display: block;
              border-radius: 8px;
              background: transparent;
            }}

            .footer {{
              width: 500px;
              text-align: center;
              margin-top: 30px;
              color: #aaa;
              font-size: 12px;
              font-weight: 500;
              letter-spacing: 0.5px;
            }}
          </style>
        </head>
        <body>
          <div class="safe-area"></div>

          <div class="page">
            <div class="header">
              {cover_block}
              <div class="v-info">
                <div class="v-title">{escape(video_title or "")}</div>
                {video_meta_html}
              </div>
            </div>

            {comments_html}

            <div class="footer">Menkelo/astrbot_plugin_r_parser</div>
          </div>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": 560, "height": 10},
                    device_scale_factor=2,
                )

                # 关键：用回旧版的 networkidle
                await page.set_content(html, wait_until="networkidle")

                height = await page.evaluate(
                    """
                    () => Math.max(
                      document.body.scrollHeight,
                      document.documentElement.scrollHeight
                    )
                    """
                )

                await page.set_viewport_size({"width": 560, "height": height})
                await page.wait_for_timeout(80)

                await page.screenshot(path=str(out_path), full_page=True)
            finally:
                await browser.close()
