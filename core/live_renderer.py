from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


class LiveCardRenderer:
    async def render_live_card(
        self,
        out_path: Path,
        *,
        platform_name: str,
        title: str,
        streamer_name: str,
        room_id: str | int,
        cover: str | None,
        avatar: str | None,
        status_text: str,
        area_text: str | None,
        online: int | str | None,
        room_label: str = "房间号",
        online_label: str = "人气",
    ):
        def fmt_num(n: int | str | None) -> str:
            if n is None or isinstance(n, bool):
                return "-"
            if isinstance(n, str):
                return n or "-"
            if n >= 10000:
                return f"{n / 10000:.1f}万"
            return str(n)

        def esc(value: str | int | None) -> str:
            return escape(str(value or ""))

        cover_html = (
            f'<img class="cover live-cover" src="{esc(cover)}" alt="">'
            if cover
            else '<div class="cover ph"></div>'
        )
        avatar_html = (
            f'<img class="avatar" src="{esc(avatar)}" alt="">'
            if avatar
            else '<div class="avatar ph"></div>'
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
            .meta {{ margin-top: 10px; color: #6b7280; font-size: 13px; }}
            .stats {{
              margin-top: 12px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;
            }}
            .item {{
              border: 1px solid #edf0f3; border-radius: 10px; padding: 8px 10px; background: #fafbfd;
            }}
            .k {{ font-size: 12px; color: #8a93a0; }}
            .v {{ margin-top: 2px; font-size: 18px; font-weight: 800; color: #1f2329; }}
            .footer {{ margin-top: 10px; color: #9aa1ac; font-size: 12px; }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="cover-wrap">{cover_html}</div>
            <div class="body">
              <div class="topline">
                <div class="platform">{esc(platform_name)}</div>
                <div class="status">{esc(status_text)}</div>
              </div>
              <div class="title">{esc(title)}</div>
              <div class="user">{avatar_html}<div class="uname">{esc(streamer_name)}</div></div>
              <div class="meta">{esc(area_text or "-")}</div>
              <div class="stats">
                <div class="item"><div class="k">{esc(room_label)}</div><div class="v">{esc(room_id)}</div></div>
                <div class="item"><div class="k">{esc(online_label)}</div><div class="v">{fmt_num(online)}</div></div>
              </div>
              <div class="footer">Menkelo/astrbot_plugin_r_parser</div>
            </div>
          </div>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": 748, "height": 10},
                    device_scale_factor=2,
                )
                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_timeout(300)
                await page.evaluate(
                    """
                    () => {
                      const imgs = document.querySelectorAll('img.live-cover');
                      for (const img of imgs) {
                        const apply = () => {
                          const w = img.naturalWidth || 0;
                          const h = img.naturalHeight || 0;
                          if (!w || !h) return;
                          const r = w / h;
                          if (r < 1.2) img.classList.add('is-portrait');
                          else if (r > 2.2) img.classList.add('is-ultrawide');
                        };
                        if (img.complete) apply();
                        else img.addEventListener('load', apply, { once: true });
                      }
                    }
                    """
                )
                await page.wait_for_timeout(120)
                height = await page.evaluate("document.body.scrollHeight")
                await page.set_viewport_size({"width": 748, "height": height})
                await page.screenshot(path=str(out_path), full_page=True)
            finally:
                await browser.close()
