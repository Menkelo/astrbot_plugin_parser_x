from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


class BiliDynamicRenderer:
    async def render_dynamic_card(
        self,
        out_path: Path,
        *,
        author_name: str,
        author_avatar: str | None,
        text: str | None,
        title: str | None = None,
        timestamp_text: str | None = None,
    ):
        avatar_html = (
            f'<img class="avatar" src="{author_avatar}" alt="">'
            if author_avatar
            else '<div class="avatar ph"></div>'
        )

        title_html = ""
        if title:
            title_html = f"""
            <div class="dyn-title">{escape(title)}</div>
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
              width: 688px;
              background: #f4f6fa;
            }}

            body {{
              margin: 0;
              padding: 24px;
              width: 688px;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
              background: #f4f6fa;
              color: #1f2329;
              overflow-x: hidden;
            }}

            .card {{
              width: 640px;
              background: #fff;
              border-radius: 16px;
              padding: 18px;
              box-shadow: 0 8px 24px rgba(0,0,0,.08);
            }}

            .head {{
              display: flex;
              align-items: center;
              gap: 10px;
            }}

            .avatar {{
              width: 42px;
              height: 42px;
              border-radius: 12px;
              object-fit: cover;
              background: #eef1f5;
              flex-shrink: 0;
              display: block;
            }}

            .ph {{
              background: linear-gradient(135deg, #eef1f4, #e5e9ef);
            }}

            .author {{
              font-size: 18px;
              font-weight: 800;
              line-height: 1.25;
              color: #1f2329;
              word-break: break-word;
            }}

            .time {{
              margin-top: 2px;
              font-size: 12px;
              color: #8a93a0;
              line-height: 1.2;
            }}

            .dyn-title {{
              margin-top: 14px;
              font-size: 18px;
              line-height: 1.45;
              font-weight: 850;
              color: #1f2329;
              word-break: break-word;
            }}

            .text {{
              margin-top: 12px;
              font-size: 15px;
              line-height: 1.7;
              white-space: pre-wrap;
              word-break: break-word;
              color: #2f3542;
            }}

            .foot {{
              margin-top: 14px;
              font-size: 12px;
              color: #9aa1ac;
              text-align: left;
            }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="head">
              {avatar_html}
              <div>
                <div class="author">{escape(author_name)}</div>
                <div class="time">{escape(timestamp_text or "")}</div>
              </div>
            </div>

            {title_html}

            <div class="text">{escape(text or "（无正文）")}</div>
            <div class="foot">Menkelo/astrbot_plugin_r_parser</div>
          </div>

          <script>
            for (const img of document.querySelectorAll("img")) {{
              img.addEventListener("error", () => {{
                img.style.display = "none";
              }});
            }}
          </script>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": 688, "height": 10},
                    device_scale_factor=2,
                )

                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(200)

                h = await page.evaluate(
                    """
                    () => Math.max(
                      document.body.scrollHeight,
                      document.documentElement.scrollHeight
                    )
                    """
                )

                await page.set_viewport_size({"width": 688, "height": h})
                await page.wait_for_timeout(80)

                await page.screenshot(path=str(out_path), full_page=True)

            finally:
                await browser.close()
