"""Visible text/frame edges, not only line boxes, have balanced outer spacing."""
import asyncio
from io import BytesIO
import pytest
from PIL import Image
from playwright.async_api import async_playwright
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer
from astrbot_plugin_parser_x.core.browser_backend import _inject_font_css
from astrbot_plugin_parser_x.core.comment_canvas import CommentAuthor, CommentEntry, CommentRichPart, CommentDocument, WEIBO_THEME

@pytest.mark.parametrize('nested', [False, True])
def test_visible_outer_comment_spacing(nested):
    async def run():
        reply = CommentEntry(CommentAuthor('回复者'), [CommentRichPart('text', '楼中楼正文')])
        root = CommentEntry(CommentAuthor('评论者'), [CommentRichPart('text', '评论正文')], first_reply=reply if nested else None)
        html = UnifiedCardRenderer(None).build_html(platform_name='微博', platform_key='weibo', author_name='作者', text='简介', comment_document=CommentDocument(WEIBO_THEME,'work','','1',[root]))
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width':900,'height':1400})
                await page.set_content(_inject_font_css(html))
                await page.evaluate('document.fonts.ready')
                box = await page.locator('.comment-card').bounding_box()
                body = await page.locator('.comment-card > .comment-body').bounding_box()
                image = Image.open(BytesIO(await page.locator('.comment-card').screenshot())).convert('RGB')
                # Ignore avatar; inspect text and (when present) the reply frame.
                left = round(body['x'] - box['x'])
                rows = [y for y in range(3, image.height-3) if any(max(image.getpixel((x,y)))>35 for x in range(left,image.width))]
                top, bottom = rows[0], image.height-1-rows[-1]
                assert abs(top-bottom)<=3, (nested,top,bottom)
                assert 29<=top<=36, (nested,top,bottom)
                if nested:
                    assert await page.locator('.reply-card').evaluate('(e)=>getComputedStyle(e).paddingTop')=='20.25px'
            finally:
                await browser.close()
    asyncio.run(run())
