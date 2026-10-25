"""Current unified card contract: reference palette, concise metadata, owner badge."""
import asyncio

import pytest
from playwright.async_api import async_playwright

from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor, CommentBadge, CommentDocument, CommentEntry, CommentRichPart, WEIBO_THEME,
)
from astrbot_plugin_parser_x.core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge, BiliCommentDocument, BiliCommentEntry, BiliFanMedal, BiliRichPart,
)
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


@pytest.mark.parametrize("platform", ["weibo", "xiaohongshu", "bilibili", "douyin", "xiaoheihe"])
@pytest.mark.parametrize("legacy", [None, "原博", "UP主", "作者"])
def test_owner_badge_is_author_for_top_level_and_nested(platform, legacy):
    author = CommentAuthor("poster", is_up=legacy is None,
                           badges=[CommentBadge(legacy)] if legacy else [])
    reply = CommentEntry(author, [CommentRichPart("text", "reply")])
    entry = CommentEntry(author, [CommentRichPart("text", "root")],
                         first_reply=reply, pinned=True)
    html = UnifiedCardRenderer(None)._render_comment_entry(entry, nested=False, platform_key=platform)
    assert html.count('<span class="comment-author">作者</span>') == 2
    assert "原博" not in html and "UP主" not in html
    assert html.index('class="comment-author"') < html.index('class="comment-pinned"')


def test_bili_owner_and_nested_owner_use_same_author_badge_without_medals():
    reply = BiliCommentEntry(BiliAuthorBadge("owner", is_up=True), [BiliRichPart("text", "reply")])
    owner = BiliCommentEntry(BiliAuthorBadge("owner", is_up=True), [BiliRichPart("text", "owner comment")])
    root = BiliCommentEntry(BiliAuthorBadge("reader", fan_medal=BiliFanMedal("REMOVE_MEDAL", level=9)),
                            [BiliRichPart("text", "root")], first_reply=reply)
    html = UnifiedCardRenderer(None)._render_comment_section(
        BiliCommentDocument("work", "", "3", [root, owner]), "bilibili")
    assert "REMOVE_MEDAL" not in html and "bili-fan-medal" not in html
    assert html.count('<span class="comment-author">作者</span>') == 2
    assert "root" in html and "reply" in html and "owner comment" in html


def test_no_plugin_info_panel_but_original_body_and_attribution_remain():
    html = UnifiedCardRenderer(None).build_html(
        platform_name="微博", author_name="作者", text="原文中的附加文字不应误删",
        info_items=["正文完整保留", "原图 2 张", "含转发原文"], info_title="附加文字",
        author_meta="转发自 @原作者",
    )
    assert 'class="info-block"' not in html and 'class="info-chip"' not in html
    assert "正文完整保留" not in html and "含转发原文" not in html
    assert "原文中的附加文字不应误删" in html and "转发自 @原作者" in html


def test_reference_palette_and_blue_mentions_in_real_browser():
    async def run():
        reply = CommentEntry(CommentAuthor("作者", is_up=True), [CommentRichPart("text", "reply")])
        entry = CommentEntry(CommentAuthor("读者"), [CommentRichPart("highlight", "@作者")],
                             first_reply=reply, pinned=True)
        html = UnifiedCardRenderer(None).build_html(
            platform_name="小黑盒", platform_key="xiaoheihe", author_name="作品作者",
            title="标题", text="简介原文 @作者 https://example.com",
            author_meta="10-23", comment_document=CommentDocument(WEIBO_THEME, "work", "", "3", [entry, reply]),
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 900})
                await page.set_content(html)
                async def color(selector, prop="color"):
                    return await page.locator(selector).first.evaluate("(e,p)=>getComputedStyle(e)[p]", prop)
                assert await color(".card", "backgroundColor") == "rgb(0, 0, 0)"
                assert await color("h1") == "rgb(255, 255, 255)"
                assert await color(".author") == "rgb(255, 255, 255)"
                assert await color(".text") == "rgb(138, 143, 153)"
                assert await color(".copy-block", "backgroundColor") == "rgb(17, 18, 21)"
                assert await color(".comment-nickname") == "rgb(148, 153, 163)"
                assert await color(".comment-content") == "rgb(255, 255, 255)"
                assert await color(".reply-card .comment-content") == "rgb(255, 255, 255)"
                assert await color(".reply-card", "backgroundColor") == "rgb(22, 24, 29)"
                assert await page.locator('.profile-meta').count() == 0
                assert await color(".footer-brand") == "rgb(85, 89, 99)"
                assert await color(".profile-avatar", "backgroundColor") == "rgb(27, 36, 50)"
                assert await color(".comment-avatar", "backgroundColor") == "rgb(27, 36, 50)"
                assert await color(".comment-avatar", "borderTopColor") == "rgb(40, 43, 51)"
                assert await page.locator('.comment-card').first.evaluate("e=>getComputedStyle(e,'::after').backgroundColor") == "rgb(30, 32, 38)"
                assert await color(".text-mention") == "rgb(123, 156, 211)"
                assert await color(".comment-highlight") == "rgb(123, 156, 211)"
                # Reference has no author/pin design: retain established styles.
                assert await color(".comment-author") == "rgb(232, 180, 95)"
                assert await color(".comment-pinned", "backgroundColor") == "rgb(38, 42, 61)"
                assert await color(".text-link:not(.text-mention)") == "rgb(123, 156, 211)"
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize("bili", [False, True])
def test_comment_link_labels_and_plain_urls_are_highlighted(bili):
    renderer = UnifiedCardRenderer(None)
    part = BiliRichPart if bili else CommentRichPart
    render = renderer._render_bili_rich_text if bili else renderer._render_comment_rich_text
    html = render([part("highlight", "相关视频"), part("text", "普通正文 https://example.com @读者")])
    assert '<span class="comment-highlight">相关视频</span>' in html
    assert '<span class="text-link">https://example.com</span>' in html
    assert '<span class="text-link text-mention">@读者</span>' in html
    assert "普通正文" in html


def test_visual_refresh_does_not_reuse_previous_card_cache(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    result = ParseResult(Platform("weibo", "微博"), text="body")
    values = ["weibo", "", "", "", "", "", "body", "", "cover", "",
              "None", "None", "None", "None", "", "none", "text_card_unified_v4_reference_palette"]
    old_digest = hashlib.md5("\n".join(values).encode()).hexdigest()[:12]
    old_path = tmp_path / f"text_card_weibo_{old_digest}.png"
    old_path.write_bytes(b"old palette")
    calls = []
    async def render_card(out_path, **kwargs):
        calls.append(kwargs)
        out_path.write_bytes(b"new palette")
    plugin = object.__new__(ParserXPlugin)
    plugin.cache_dir = tmp_path
    plugin.text_renderer = SimpleNamespace(render_card=render_card)
    card = asyncio.run(plugin._build_text_card_content(result))
    assert len(calls) == 1
    assert asyncio.run(card.get_path()) != old_path
    assert old_path.read_bytes() == b"old palette"
