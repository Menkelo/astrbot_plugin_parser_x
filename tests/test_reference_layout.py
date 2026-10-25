"""Scheme-three geometry at 900px (reference is 400px, scale 2.25)."""
import asyncio
import pytest
from playwright.async_api import async_playwright
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer
from astrbot_plugin_parser_x.core.comment_canvas import CommentAuthor, CommentEntry, CommentRichPart, CommentDocument, WEIBO_THEME


def test_reference_layout_geometry_in_browser():
    async def run():
        reply = CommentEntry(CommentAuthor('楼中楼'), [CommentRichPart('text', '楼中楼正文')])
        root = CommentEntry(CommentAuthor('评论者'), [CommentRichPart('text', '评论正文')], first_reply=reply)
        html = UnifiedCardRenderer(None).build_html(platform_name='Bilibili', platform_key='bilibili', title='方案三标题', author_name='作者', author_meta='10-23', text='简介原文', comment_document=CommentDocument(WEIBO_THEME, 'work', '', '2', [root, reply]))
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width':900, 'height':900})
                await page.set_content(html)
                async def style(selector, prop, pseudo=None):
                    return await page.locator(selector).first.evaluate('(e,a)=>getComputedStyle(e,a[1])[a[0]]', [prop, pseudo])
                assert await style('.card', 'borderRadius') == '0px'
                assert await style('.card', 'content', '::after') in ('none', 'normal')
                assert await style('.primary-block h1', 'width', '::after') == '72px'
                assert await style('.primary-block h1', 'height', '::after') == '4.5px'
                assert await style('.profile-block', 'borderTopWidth') == '0px'
                assert await style('.copy-block', 'borderRadius') == '13.5px'
                assert await style('.copy-block', 'paddingLeft') == '27px'
                assert await style('.comment-card', 'borderBottomWidth') == '0px'
                assert await style('.content-body', 'paddingBottom') == '0px'
                assert await style('.comment-card', 'paddingTop') == '22.5px'
                assert await style('.comment-card', 'paddingBottom') == '31.5px'
                gap = await page.evaluate("""() => {
                    const last = document.querySelector('.comment-card:last-child .comment-body').getBoundingClientRect();
                    return document.querySelector('.footer').getBoundingClientRect().top - last.bottom;
                }""")
                assert gap == pytest.approx(22.5)  # plain-text last row: compensate font leading
                await page.set_content(UnifiedCardRenderer(None).build_html(platform_name='Bilibili', author_name='作者', text='无评论正文'))
                assert await style('.content-body', 'paddingBottom') == '22.5px'
                await page.set_content(html)
                assert await style('.comment-card', 'left', '::after') == '105.75px'
                assert await style('.reply-card .comment-avatar', 'width') == '54px'
                assert await style('.reply-card .comment-content', 'fontSize') == '29.25px'
                assert await style('.comment-content', 'fontSize') == '31.5px'
                assert await style('.hero', 'height') == '480px' if await page.locator('.hero').count() else True
                assert await page.locator('.profile-platform').inner_text() == 'Bilibili'
                assert await page.locator('.profile-meta').count() == 0
                assert await page.locator('.comment-heading').inner_text() == '热门评论'
                assert await page.evaluate('document.documentElement.scrollWidth') == 900
            finally:
                await browser.close()
    asyncio.run(run())
