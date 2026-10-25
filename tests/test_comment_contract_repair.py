"""Focused regressions for comment HTTP, adaptation, and rendering contracts."""
import asyncio
from types import SimpleNamespace

import pytest
from msgspec import json as msgjson

from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor, CommentBadge, CommentDocument, CommentEntry, CommentRichPart,
    WEIBO_THEME,
)
from astrbot_plugin_parser_x.core.data import ParseResult, Platform
from astrbot_plugin_parser_x.core.parsers.weibo_comment import WeiboCommentFeed
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


@pytest.mark.parametrize("nested,field,expected", [
    (False, "like_text", "1.2万"),
    (False, "reply_text", "回复 12"),
    (False, "creator_liked", True),
    (True, "like_text", "1亿"),
    (True, "creator_liked", True),
])
def test_weibo_adapts_comment_and_reply_metrics_from_api_fields(nested, field, expected):
    feed = WeiboCommentFeed(SimpleNamespace(headers={}))
    entry = feed.adapt_comment({
        "user": {"id": 7, "screen_name": "owner"}, "text": "comment",
        "like_count": "12500", "total_number": "12", "reply_count": 999,
        "isLikedByMblogAuthor": True,
        "comments": [{"user": {"id": 8, "screen_name": "reader"},
                      "text": "reply", "like_count": 100000000,
                      "isLikedByMblogAuthor": True}],
    }, {"id": "7"}, {})

    assert entry is not None
    assert entry.author.is_up is True
    selected = entry.first_reply if nested else entry
    assert selected is not None
    assert getattr(selected, field) == expected


@pytest.mark.parametrize("platform_key,label", [
    ("weibo", "作者"), ("xiaohongshu", "作者"),
    ("bilibili", "作者"), ("douyin", "作者"), ("xiaoheihe", "作者"),
])
@pytest.mark.parametrize("nested", [False, True])
def test_generic_owner_flag_renders_platform_badge(platform_key, label, nested):
    owner = CommentEntry(CommentAuthor("owner", is_up=True),
                         [CommentRichPart("text", "owner comment")])
    entry = (CommentEntry(CommentAuthor("reader"), [CommentRichPart("text", "body")],
                          first_reply=owner) if nested else owner)
    document = CommentDocument(WEIBO_THEME, "work", "", "1", [entry])
    renderer = UnifiedCardRenderer(None)

    html = renderer.build_html(platform_name=platform_key, platform_key=platform_key,
                               author_name="poster", text="body", comment_document=document)

    assert html.count(f'<span class="comment-author">{label}</span>') == 1


def test_generic_owner_badge_does_not_duplicate_legacy_badge():
    owner = CommentEntry(CommentAuthor("owner", is_up=True,
                                       badges=[CommentBadge("原博")]), [])
    html = UnifiedCardRenderer(None)._render_comment_entry(
        owner, nested=False, platform_key="weibo",
    )
    assert html.count('<span class="comment-author">作者</span>') == 1


@pytest.mark.parametrize("suppressed", [False, True])
def test_comment_suppression_clears_unified_factory_only_when_requested(suppressed):
    async def comments():
        raise AssertionError("constructing a result must not fetch comments")

    factories = {
        "comment_image_task_factory": comments,
        "comment_document_task_factory": comments,
        "text_card_comment_factory": comments,
        "comment_timeout": 10,
    }
    result = ParseResult(Platform("weibo", "微博"), extra={
        **factories, "suppress_comments": suppressed, "keep": "unrelated",
    })

    for key, value in factories.items():
        if suppressed:
            assert key not in result.extra
        else:
            assert result.extra[key] == value
    assert result.extra["keep"] == "unrelated"


def test_comment_avatar_uses_styled_selector_and_owner_precedes_pin():
    owner = CommentEntry(CommentAuthor("owner", avatar="https://example.com/avatar.png", is_up=True),
                         [CommentRichPart("text", "comment")], pinned=True)
    renderer = UnifiedCardRenderer(None)
    html = renderer._render_comment_entry(owner, nested=False, platform_key="weibo")
    assert 'class="comment-avatar"' in html
    assert html.index('class="comment-author"') < html.index('class="comment-pinned"')


def test_bili_real_model_medal_and_reply_render_without_losing_comments():
    from astrbot_plugin_parser_x.core.parsers.bilibili.comment_canvas import (
        BiliAuthorBadge, BiliCommentDocument, BiliCommentEntry, BiliFanMedal, BiliRichPart,
    )
    reply = BiliCommentEntry(BiliAuthorBadge("owner", is_up=True),
                             [BiliRichPart("text", "reply")], pinned=True)
    entry = BiliCommentEntry(BiliAuthorBadge("reader", fan_medal=BiliFanMedal("medal", level=3)),
                             [BiliRichPart("text", "root")], first_reply=reply)
    document = BiliCommentDocument("work", "", "2", [entry])
    html = UnifiedCardRenderer(None)._render_comment_section(document, "bilibili")
    assert "medal" not in html and "root" in html and "reply" in html
    assert html.count('<article class="comment-card">') == 1
    assert html.count('<div class="reply-card">') == 1
    assert html.index('class="comment-author"') < html.index('class="comment-pinned"')


@pytest.mark.parametrize("cookie_field", ["cookie", "cookies"])
def test_weibo_parser_factory_accepts_owner_id_and_cookie_alias(cookie_field):
    from astrbot_plugin_parser_x.core.parsers.weibo import WeiboParser
    parser = object.__new__(WeiboParser)
    setattr(parser, cookie_field, "session=test")
    parser.headers = {}
    parser.enable_comment_card = True
    parser.comment_timeout = 5
    feed = parser.comment_feed = WeiboCommentFeed(parser)

    async def fetch(mid):
        return SimpleNamespace(items=[{"id": "comment", "user": {"id": "7", "screen_name": "owner"}, "text": "comment", "created_at": "Fri Jan 17 08:24:08 +0800 2020"}], total=1, has_more=False)
    async def apply(entries, **kwargs):
        return entries
    async def embed(entries):
        return None
    feed.fetch = fetch
    feed.comment_filter = SimpleNamespace(apply=apply)
    feed._embed_avatars = embed
    extra = parser._comment_extra("mid", title="work", cover=None, owner_id="7")
    document = asyncio.run(extra["text_card_comment_factory"]())
    assert document is not None
    assert document.entries[0].author.is_up is True


def test_weibo_fetch_awaits_response_bytes_and_reads_total_number():
    calls = []
    pages = [
        {"ok": 1, "data": {"data": [{"id": "first"}], "total_number": 42,
                           "total": 999, "max_id": "next", "max_id_type": 1}},
        {"ok": 1, "data": {"data": [{"id": "second"}], "total_number": 42,
                           "total": 999, "max_id": "0"}},
    ]

    async def http_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(content=msgjson.encode(pages[len(calls) - 1]))

    feed = WeiboCommentFeed(SimpleNamespace(
        http_get=http_get, headers={"User-Agent": "test"}, cookie="session=test",
    ))
    result = asyncio.run(feed.fetch("mid"))

    assert result.items == [{"id": "first"}, {"id": "second"}]
    assert result.total == 42
    assert result.has_more is False
    assert len(calls) == 2
    assert calls[0][1]["headers"]["Cookie"] == "session=test"
    assert calls[1][1]["params"]["max_id"] == "next"
    assert calls[1][1]["params"]["max_id_type"] == 1
