"""Browser regressions for comments whose gallery images all fail to decode."""
import asyncio
import base64

import pytest
from playwright.async_api import async_playwright

from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, WEIBO_THEME,
)
from astrbot_plugin_parser_x.core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge, BiliCommentDocument, BiliCommentEntry, BiliRichPart,
)
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


BROKEN = 'data:image/png;base64,broken'
VALID = 'data:image/svg+xml;base64,' + base64.b64encode(
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="red"/></svg>'
).decode()


def entry(name, *, bili=False, text='', images=(), reply=None, sticker=''):
    if bili:
        return BiliCommentEntry(BiliAuthorBadge(name, avatar=VALID),
                                [BiliRichPart('text', text)], images=list(images), first_reply=reply)
    return CommentEntry(CommentAuthor(name, avatar=VALID),
                        [CommentRichPart('text', text)], images=list(images),
                        first_reply=reply, sticker_image=sticker)


def html(entries, *, bili=False):
    document = (BiliCommentDocument('id', '', '2', entries) if bili else
                CommentDocument(WEIBO_THEME, 'id', '', '2', entries))
    return UnifiedCardRenderer(None).build_html(
        author_name='作品作者', platform_name='B站' if bili else '微博',
        platform_key='bilibili' if bili else 'weibo', text='作品正文',
        comment_document=document,
    )


def in_browser(markup, assertions):
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width': 900, 'height': 900})
                await page.set_content(markup)
                await page.evaluate('window.__parserXGalleryReady')
                await assertions(page)
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('bili', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_image_only_failed_comment_is_removed_with_avatar_and_name(bili, nested):
    failed = entry('失败评论者', bili=bili, text=' \n', images=[BROKEN, BROKEN])
    root = (entry('保留父评论', bili=bili, text='父评论正文', reply=failed)
            if nested else failed)
    text = entry('文字评论者', bili=bili, text='保留文字', images=[BROKEN])
    mixed = entry('图片评论者', bili=bili, images=[BROKEN, VALID])

    async def check(page):
        assert await page.get_by_text('失败评论者', exact=True).count() == 0
        assert await page.locator('.reply-card').count() == 0
        assert await page.locator('.comment-card').count() == (3 if nested else 2)
        assert await page.locator('.comment-card > .comment-avatar').count() == (3 if nested else 2)
        assert await page.get_by_text('保留文字', exact=True).is_visible()
        assert await page.get_by_text('图片评论者', exact=True).is_visible()
        assert await page.locator('.justified-gallery img').count() == 1
        assert await page.locator('.comment-heading').is_visible()
        assert '加载失败' not in await page.locator('body').inner_text()

    in_browser(html([root, text, mixed], bili=bili), check)


@pytest.mark.parametrize('bili', [False, True])
def test_all_failed_comments_remove_section_and_orphan_heading(bili):
    reply = entry('失败回复者', bili=bili, images=[BROKEN])
    root = entry('失败父评论', bili=bili, images=[BROKEN], reply=reply)

    async def check(page):
        assert await page.locator('.comment-card, .reply-card').count() == 0
        assert await page.locator('.comment-heading').count() == 0
        assert await page.locator('.comment-section').count() == 0
        assert await page.get_by_text('作品正文', exact=True).is_visible()

    in_browser(html([root], bili=bili), check)


@pytest.mark.parametrize('bili', [False, True])
@pytest.mark.parametrize('reply_kind', ['text', 'image'])
def test_failed_parent_gallery_preserves_visible_nested_content(bili, reply_kind):
    reply = entry('有效回复者', bili=bili, text='有效回复正文' if reply_kind == 'text' else '',
                  images=[VALID] if reply_kind == 'image' else [])
    root = entry('保留父评论', bili=bili, images=[BROKEN], reply=reply)

    async def check(page):
        assert await page.get_by_text('保留父评论', exact=True).is_visible()
        assert await page.get_by_text('有效回复者', exact=True).is_visible()
        assert await page.locator('.comment-card').count() == 1
        assert await page.locator('.reply-card').count() == 1

    in_browser(html([root], bili=bili), check)


@pytest.mark.parametrize('kind', ['sticker', 'emote'])
def test_failed_gallery_preserves_other_visible_comment_images(kind):
    root = entry('表情评论者', images=[BROKEN], sticker=VALID if kind == 'sticker' else '')
    if kind == 'emote':
        root.content = [CommentRichPart('emote', '', VALID)]

    async def check(page):
        assert await page.get_by_text('表情评论者', exact=True).is_visible()
        assert await page.locator('.comment-sticker, .comment-emote').is_visible()
        assert await page.locator('.justified-gallery img').count() == 0

    in_browser(html([root]), check)


@pytest.mark.parametrize('nested_failure', [False, True])
def test_gallery_ready_waits_for_pending_sticker_before_removing_comment(nested_failure):
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                async def delayed_sticker(route):
                    await asyncio.sleep(0.1)
                    await route.fulfill(content_type='image/svg+xml',
                                        body=base64.b64decode(VALID.split(',')[1]))
                await page.route('https://images.test/sticker.svg', delayed_sticker)
                root = entry('延迟表情评论者', images=[] if nested_failure else [BROKEN],
                             sticker='https://images.test/sticker.svg',
                             reply=entry('失败回复者', images=[BROKEN]) if nested_failure else None)
                await page.set_content(html([root]), wait_until='domcontentloaded')
                await page.evaluate('window.__parserXGalleryReady')
                assert await page.get_by_text('延迟表情评论者', exact=True).is_visible()
                assert await page.locator('.comment-sticker').evaluate('(e) => e.naturalWidth') == 40
            finally:
                await browser.close()
    asyncio.run(run())
