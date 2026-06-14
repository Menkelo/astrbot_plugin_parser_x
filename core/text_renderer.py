import re
from html import escape
from pathlib import Path

from playwright.async_api import async_playwright

from astrbot.api import logger

RICH_TEXT_RE = re.compile(
    r"(?P<topic>#[^#\s\r\n][^#\r\n]{0,60}?#)"
    r"|(?P<mention>[@\uff20][\w\u4e00-\u9fff\u3400-\u4dbf.-]{1,32})"
    r"|(?P<url>https?://[^\s<>()\"']+)"
    r"|(?P<link>\u7f51\u9875\u94fe\u63a5|\u62bd\u5956\u8be6\u60c5)"
)


class TextCardRenderer:
    _playwright_checked = False
    _playwright_available: bool | None = None

    @classmethod
    async def check_available(cls) -> bool:
        if cls._playwright_checked:
            return cls._playwright_available is True

        cls._playwright_checked = True
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                await browser.close()
        except Exception as e:
            cls._playwright_available = False
            logger.warning(
                "Text card renderer unavailable; run `playwright install chromium` "
                f"if text cards fail: {e}"
            )
            return False

        cls._playwright_available = True
        return True

    @staticmethod
    def _render_text_html(text: str) -> str:
        parts: list[str] = []
        last = 0

        for match in RICH_TEXT_RE.finditer(text or ""):
            parts.append(escape(text[last : match.start()]))
            parts.append(f'<span class="text-link">{escape(match.group(0))}</span>')
            last = match.end()

        parts.append(escape(text[last:]))
        return "".join(parts)

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
    ):
        if not await self.check_available():
            raise RuntimeError("Playwright Chromium is unavailable")

        avatar_html = (
            f'<img class="avatar" src="{escape(author_avatar)}" alt="">'
            if author_avatar
            else '<div class="avatar avatar-ph"></div>'
        )
        author_html = (
            f'<div class="author">{escape(author_name)}</div>' if author_name else ""
        )
        time_html = (
            f'<div class="time">{escape(timestamp_text)}</div>' if timestamp_text else ""
        )
        title_html = f'<div class="title">{escape(title)}</div>' if title else ""
        text_html = self._render_text_html(text)

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
              width: 760px;
              background: #f3f5f8;
            }}

            body {{
              margin: 0;
              padding: 26px;
              width: 760px;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
              background: #f3f5f8;
              color: #20242c;
              overflow-x: hidden;
            }}

            .card {{
              width: 708px;
              background: #fff;
              border: 1px solid #e7ebf0;
              border-radius: 16px;
              padding: 24px;
              box-shadow: 0 10px 30px rgba(32, 36, 44, .08);
            }}

            .meta {{
              display: flex;
              align-items: center;
              justify-content: flex-start;
              gap: 16px;
            }}

            .platform {{
              display: inline-flex;
              align-items: center;
              min-height: 24px;
              padding: 3px 9px;
              border-radius: 999px;
              background: #f6fbff;
              border: 1px solid #e2f1ff;
              color: #8ebfe9;
              font-size: 13px;
              font-weight: 750;
              line-height: 1.2;
            }}

            .time {{
              margin-top: 2px;
              color: #8c95a3;
              font-size: 13px;
              line-height: 1.45;
              text-align: left;
              word-break: break-word;
            }}

            .profile {{
              margin-top: 16px;
              display: flex;
              align-items: center;
              gap: 14px;
            }}

            .author-block {{
              min-width: 0;
            }}

            .avatar {{
              width: 54px;
              height: 54px;
              border-radius: 50%;
              object-fit: cover;
              background: #eef3f7;
              border: 1px solid #e7edf3;
              flex-shrink: 0;
              display: block;
            }}

            .avatar-ph {{
              background: linear-gradient(135deg, #eef5fb, #e4edf6);
            }}

            .author {{
              color: #20242c;
              font-size: 21px;
              font-weight: 850;
              line-height: 1.35;
              word-break: break-word;
            }}

            .title {{
              margin-top: 20px;
              color: #20242c;
              font-size: 20px;
              font-weight: 850;
              line-height: 1.45;
              word-break: break-word;
            }}

            .text {{
              margin-top: 18px;
              color: #303744;
              font-size: 18px;
              line-height: 1.78;
              white-space: pre-wrap;
              word-break: break-word;
            }}

            .text-link {{
              color: #8ebfe9;
              font-weight: 650;
            }}

            .footer {{
              margin-top: 12px;
              color: #9aa2ad;
              font-size: 12px;
              line-height: 1.4;
            }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="meta">
              <div class="platform">{escape(platform_name)}</div>
            </div>
            <div class="profile">
              {avatar_html}
              <div class="author-block">
                {author_html}
                {time_html}
              </div>
            </div>
            {title_html}
            <div class="text">{text_html}</div>
            <div class="footer">Menkelo/astrbot_plugin_r_parser</div>
          </div>

          <script>
            for (const img of document.querySelectorAll("img")) {{
              img.addEventListener("error", () => {{
                img.replaceWith(Object.assign(document.createElement("div"), {{
                  className: "avatar avatar-ph"
                }}));
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
                    viewport={"width": 760, "height": 10},
                    device_scale_factor=2,
                )

                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(120)

                height = await page.evaluate(
                    """
                    () => Math.max(
                      document.body.scrollHeight,
                      document.documentElement.scrollHeight
                    )
                    """
                )

                await page.set_viewport_size({"width": 760, "height": height})
                await page.wait_for_timeout(60)

                await page.screenshot(path=str(out_path), full_page=True)

            finally:
                await browser.close()
