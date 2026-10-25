"""User-requested borderless card and concise presentation."""
import asyncio
import pytest
from playwright.async_api import async_playwright
from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, WEIBO_THEME,
)
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


@pytest.mark.parametrize('published', ['10-23', '2026-10-23', '2026-10-23 12:34:56'])
def test_publication_time_is_not_rendered(published):
    html = UnifiedCardRenderer(None).build_html(
        platform_name='抖音', author_name='作者', text='简介',
        author_meta=published, timestamp_text=published,
    )
    assert published not in html
    assert 'class="profile-meta"' not in html
    assert 'class="profile-platform">抖音</span>' in html


def test_outer_card_is_square_and_borderless_but_internal_frames_remain():
    async def run():
        reply = CommentEntry(CommentAuthor('回复者'), [CommentRichPart('text', '回复正文')])
        entry = CommentEntry(CommentAuthor('读者'), [CommentRichPart('text', '评论')], first_reply=reply)
        html = UnifiedCardRenderer(None).build_html(
            platform_name='抖音', author_name='作者', text='简介',
            comment_document=CommentDocument(WEIBO_THEME, 'work', '', '1', [entry]),
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width': 900, 'height': 900})
                await page.set_content(html)
                async def style(selector, prop, pseudo=None):
                    return await page.locator(selector).first.evaluate(
                        '(e,a)=>getComputedStyle(e,a[1])[a[0]]', [prop, pseudo])
                assert await style('.card', 'borderRadius') == '0px'
                assert await style('.card', 'borderTopWidth') == '0px'
                assert await style('.card', 'content', '::after') in ('none', 'normal')
                assert await style('.copy-block', 'borderRadius') == '13.5px'
                assert await style('.reply-card', 'borderRadius') == '13.5px'
                assert await style('.profile-avatar', 'borderRadius') == '50%'
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('empty', [False, True])
def test_hot_comments_heading_only_for_visible_comments(empty):
    entries = [] if empty else [CommentEntry(CommentAuthor('读者'), [CommentRichPart('text', '第一条评论')])]
    html = UnifiedCardRenderer(None).build_html(
        platform_name='微博', author_name='作者', text='简介正文',
        comment_document=CommentDocument(WEIBO_THEME, 'work', '', '1', entries),
    )
    if empty:
        assert 'class="comment-heading"' not in html
    else:
        assert '<h2 class="comment-heading">热门评论</h2>' in html
        assert html.index('简介正文') < html.index('热门评论') < html.index('第一条评论')


def test_non_time_attribution_is_preserved():
    html = UnifiedCardRenderer(None).build_html(
        platform_name='微博', author_name='作者', text='简介',
        author_meta='转发自 @原作者',
    )
    assert '转发自 @原作者' in html
