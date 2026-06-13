from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


class BiliSpaceRenderer:
    async def render_space_card(
        self,
        out_path: Path,
        *,
        name: str,
        mid: int,
        avatar: str | None,
        sign: str | None,
        level: int | None,
        official_title: str | None,
        following: int | None,
        follower: int | None,
        archive_count: int | None,
        representative_work: dict | None,
    ):
        def fmt_num(n: int | None) -> str:
            if n is None or isinstance(n, bool):
                return "-"
            if n >= 10000:
                return f"{n / 10000:.1f}万"
            return str(n)

        def esc(s: str | None) -> str:
            return escape(s or "")

        avatar_html = (
            f'<img class="avatar" src="{avatar}" alt="">'
            if avatar
            else '<div class="avatar avatar-ph"></div>'
        )

        sign_html = esc(sign or "这个人很神秘，什么都没有写")
        official_html = f'<div class="tag official">{esc(official_title)}</div>' if official_title else ""
        level_html = f'<div class="tag">Lv.{level}</div>' if level is not None else ""

        def rep_html(work: dict | None) -> str:
            if not work:
                return ""

            title = esc(work.get("title") or "未命名稿件")
            cover = work.get("cover")
            url = esc(work.get("url") or "")
            date = esc(work.get("date") or "")

            date_html = f'<div class="work-date">{date}</div>' if date else '<div class="work-date"></div>'

            if cover:
                cover_block = f'<img class="cover work-cover" src="{cover}" alt="">'
            else:
                # 代表作存在但封面失败时，不画大灰块，改为紧凑标题卡
                cover_block = ""

            link_start = f'<a class="work-link" href="{url}">' if url else '<div class="work-link">'
            link_end = "</a>" if url else "</div>"

            cover_wrap = ""
            if cover_block:
                cover_wrap = f'<div class="cover-wrap">{cover_block}</div>'

            return f"""
            {link_start}
              <div class="work-card">
                {cover_wrap}
                <div class="work-meta">
                  <div class="work-label">代表作</div>
                  <div class="work-title">{title}</div>
                  {date_html}
                </div>
              </div>
            {link_end}
            """

        works_section = (
            f'<div class="works">{rep_html(representative_work)}</div>'
            if representative_work
            else ""
        )

        html = f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8" />
          <style>
            * {{
              box-sizing: border-box;
            }}

            html {{
              margin: 0;
              padding: 0;
              width: 808px;
              background: linear-gradient(180deg, #f4f6f9 0%, #eaf0f7 100%);
            }}

            body {{
              margin: 0;
              padding: 26px;
              width: 808px;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
              background: linear-gradient(180deg, #f4f6f9 0%, #eaf0f7 100%);
              color: #1f2329;
              overflow-x: hidden;
            }}

            .card {{
              width: 756px;
              background: #fff;
              border-radius: 20px;
              padding: 24px;
              box-shadow: 0 12px 36px rgba(31,35,41,.09), 0 2px 8px rgba(31,35,41,.06);
            }}

            .top {{
              display: flex;
              gap: 16px;
              align-items: center;
            }}

            .avatar {{
              width: 74px;
              height: 74px;
              border-radius: 16px;
              object-fit: cover;
              background: #f0f1f3;
              flex-shrink: 0;
              border: 1px solid #eef1f4;
              display: block;
            }}

            .avatar-ph {{
              background: linear-gradient(135deg, #eef1f4, #e5e9ef);
            }}

            .name {{
              font-size: 24px;
              font-weight: 800;
              line-height: 1.2;
              word-break: break-word;
            }}

            .mid {{
              margin-top: 4px;
              font-size: 13px;
              color: #7a808a;
            }}

            .tags {{
              margin-top: 8px;
              display: flex;
              gap: 8px;
              flex-wrap: wrap;
            }}

            .tag {{
              font-size: 12px;
              padding: 4px 10px;
              border-radius: 999px;
              background: #f3f5f7;
              color: #4c5563;
              font-weight: 600;
            }}

            .official {{
              background: #e8f3ff;
              color: #2b6cb0;
            }}

            .sign {{
              margin-top: 16px;
              padding: 12px 14px;
              border-radius: 12px;
              background: #f8fafc;
              color: #2f3542;
              font-size: 14px;
              line-height: 1.6;
              white-space: pre-wrap;
              word-break: break-word;
            }}

            .stats {{
              margin-top: 16px;
              display: grid;
              grid-template-columns: repeat(3, minmax(0, 1fr));
              gap: 10px;
            }}

            .item {{
              background: #fbfcfd;
              border: 1px solid #edf0f3;
              border-radius: 12px;
              padding: 10px 12px;
            }}

            .k {{
              font-size: 12px;
              color: #7a808a;
            }}

            .v {{
              margin-top: 4px;
              font-size: 18px;
              font-weight: 800;
              color: #1f2329;
            }}

            .works {{
              margin-top: 16px;
              display: grid;
              grid-template-columns: 1fr;
              gap: 10px;
            }}

            .work-link {{
              text-decoration: none;
              color: inherit;
              display: block;
              height: 100%;
            }}

            .work-card {{
              background: #fbfcfd;
              border: 1px solid #edf0f3;
              border-radius: 14px;
              overflow: hidden;
              display: flex;
              flex-direction: column;
              height: 100%;
            }}

            .cover-wrap {{
              width: 100%;
              aspect-ratio: 16 / 9;
              background: #edf1f6;
              display: flex;
              align-items: center;
              justify-content: center;
              overflow: hidden;
            }}

            .cover {{
              width: 100%;
              height: 100%;
              object-fit: cover;
              object-position: center;
              display: block;
              background: #edf1f6;
            }}

            .cover.is-portrait {{
              object-fit: contain;
              width: auto;
              height: 100%;
              max-width: 100%;
            }}

            .cover.is-ultrawide {{
              object-fit: contain;
              width: 100%;
              height: auto;
              max-height: 100%;
            }}

            .work-meta {{
              padding: 12px 14px;
              display: flex;
              flex-direction: column;
              min-height: 96px;
            }}

            .work-label {{
              font-size: 12px;
              color: #7a808a;
              margin-bottom: 4px;
            }}

            .work-title {{
              font-size: 16px;
              color: #1f2329;
              line-height: 1.45;
              font-weight: 800;
              display: -webkit-box;
              -webkit-line-clamp: 2;
              -webkit-box-orient: vertical;
              overflow: hidden;
              min-height: 46px;
              word-break: break-word;
            }}

            .work-date {{
              margin-top: auto;
              font-size: 13px;
              color: #8b93a1;
            }}

            .footer {{
              margin-top: 12px;
              text-align: left;
              color: #9aa1ac;
              font-size: 12px;
              letter-spacing: .2px;
            }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="top">
              {avatar_html}
              <div>
                <div class="name">{esc(name)}</div>
                <div class="mid">UID: {mid}</div>
                <div class="tags">{level_html}{official_html}</div>
              </div>
            </div>

            <div class="sign">{sign_html}</div>

            <div class="stats">
              <div class="item"><div class="k">关注</div><div class="v">{fmt_num(following)}</div></div>
              <div class="item"><div class="k">粉丝</div><div class="v">{fmt_num(follower)}</div></div>
              <div class="item"><div class="k">稿件</div><div class="v">{fmt_num(archive_count)}</div></div>
            </div>

            {works_section}

            <div class="footer">Menkelo/astrbot_plugin_r_parser</div>
          </div>

          <script>
            for (const img of document.querySelectorAll("img")) {{
              img.addEventListener("error", () => {{
                const wrap = img.closest(".cover-wrap");
                if (wrap) {{
                  wrap.remove();
                }} else {{
                  img.style.display = "none";
                }}
              }});
            }}

            const imgs = document.querySelectorAll('img.work-cover');
            for (const img of imgs) {{
              const apply = () => {{
                const w = img.naturalWidth || 0;
                const h = img.naturalHeight || 0;
                if (!w || !h) return;
                const r = w / h;
                if (r < 1.2) img.classList.add('is-portrait');
                else if (r > 2.2) img.classList.add('is-ultrawide');
              }};
              if (img.complete) apply();
              else img.addEventListener('load', apply, {{ once: true }});
            }}
          </script>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": 808, "height": 10},
                    device_scale_factor=2,
                )

                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(220)

                height = await page.evaluate(
                    """
                    () => Math.max(
                      document.body.scrollHeight,
                      document.documentElement.scrollHeight
                    )
                    """
                )

                await page.set_viewport_size({"width": 808, "height": height})
                await page.wait_for_timeout(80)

                await page.screenshot(path=str(out_path), full_page=True)

            finally:
                await browser.close()
