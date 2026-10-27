"""Regression coverage for preserved source newlines and Tieba inline emotes."""
import asyncio
from playwright.async_api import async_playwright
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer, _normalise_content_blocks
from astrbot_plugin_parser_x.core.comment_canvas import CommentRichPart
from astrbot_plugin_parser_x.core.platform_emotes import iter_emote_matches


def test_body_flow_preserves_standalone_and_edge_newlines():
    blocks = [('text', '前\n'), ('text', '\n'), ('text', '\n后'), ('image', ' https://example.com/a.jpg ')]
    assert _normalise_content_blocks(blocks) == [('text', '前\n'), ('text', '\n'), ('text', '\n后'), ('image', 'https://example.com/a.jpg')]


def test_comment_text_newlines_are_visible_in_browser():
    async def run():
        renderer = UnifiedCardRenderer(None)
        fragment = renderer._render_comment_rich_text([CommentRichPart('text', '前\n\n后')])
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                await page.set_content('<div id="content">' + fragment + '</div>')
                assert await page.locator('#content').inner_text() == '前\n\n后'
            finally:
                await browser.close()
    asyncio.run(run())


def test_tieba_square_token_reaches_inline_emote_renderer():
    assert list(iter_emote_matches('前[微笑]后', platform_key='tieba', emotes={'[微笑]': 'https://example.com/emote.png'})) == [(1, 5, '[微笑]', 'https://example.com/emote.png')]
