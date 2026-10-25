"""AcFun's anonymous comment contract (no live network in tests)."""
import asyncio
import importlib

import pytest
from types import SimpleNamespace

from astrbot_plugin_parser_x.core.comment_settings import CommentSettings


def make_feed(pages, *, count=10, headers=None):
    calls = []

    async def http_get(url, **kwargs):
        calls.append((url, kwargs))
        value = pages[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(json=lambda: value)

    parser = SimpleNamespace(
        http_get=http_get, headers=headers or {},
        comment_settings=CommentSettings.from_config(
            {"comments": {"display_count": count}}, "acfun"),
    )
    module = importlib.import_module("astrbot_plugin_parser_x.core.parsers.acfun_comment")
    return module.AcFunCommentFeed(parser), calls


def raw(identity=1, text="有内容的评论", **fields):
    return {"commentId": identity, "userId": 9, "userName": "读者",
            "content": text, **fields}


def test_fetch_document_maps_anonymous_root_contract():
    feed, calls = make_feed([{
        "result": 0, "commentCount": 12, "totalPage": 1,
        "rootComments": [raw(isUp=True, isSticky=True, likeCount=12500,
                             headUrl=[{"url": "https://example.com/avatar.jpg"}])],
    }], headers={"Cookie": "secret", "cOoKiE": "secret", "User-Agent": "test"})
    document = asyncio.run(feed.fetch_document("48922032"))
    assert document is not None
    assert document.theme.platform_name == "AcFun"
    assert document.total_text == "12 条评论"
    entry = document.entries[0]
    assert entry.author.nickname == "读者"
    assert entry.author.avatar == "https://example.com/avatar.jpg"
    assert entry.author.is_up and entry.pinned
    assert entry.like_text == "1.2万"
    assert calls[0][0] == "https://www.acfun.cn/rest/pc-direct/comment/list"
    assert calls[0][1]["params"] == {
        "sourceId": "48922032", "sourceType": 3, "page": 1,
        "pivotCommentId": 0, "newPivotCommentId": 0,
    }
    assert calls[0][1]["headers"]
    assert all(key.lower() != "cookie" for key in calls[0][1]["headers"])
    assert feed.parser.headers["Cookie"] == "secret"


def test_root_pages_refill_filtered_slots_and_dedupe_special_sections():
    feed, calls = make_feed([
        {"result": 0, "totalPage": 5, "commentCount": 80,
         "stickyComments": [raw(1, "置顶内容")],
         "hotComments": [raw(1, "置顶内容"), raw(2, "@朋友")],
         "godComments": [raw(3, "神评内容")],
         "rootComments": [raw(1, "置顶内容"), raw(3, "神评内容")]},
        {"result": 0, "totalPage": 5, "rootComments": [raw(4, "补位内容")]},
    ], count=3)
    document = asyncio.run(feed.fetch_document("42"))
    assert document is not None
    assert [entry.content[0].text for entry in document.entries] == [
        "置顶内容", "神评内容", "补位内容"]
    assert document.entries[0].pinned
    assert [call[1]["params"]["page"] for call in calls] == [1, 2]


def test_root_pagination_is_bounded_even_when_filter_rejects_everything():
    page = {"result": 0, "totalPage": 999, "rootComments": [raw(1, "@朋友")]}
    feed, calls = make_feed([page] * 3)
    assert asyncio.run(feed.fetch_document("42")) is None
    assert len(calls) == 3


def test_parent_map_selects_first_usable_reply_without_sublist_request():
    feed, calls = make_feed([{
        "result": 0, "rootComments": [raw(1)],
        "subCommentsMap": {"1": {"pcursor": "more", "subComments": [
            raw(2, "@朋友"), raw(3, "值得讨论的回复", isUp=True, replyToUserName="楼主"),
        ]}},
    }])
    document = asyncio.run(feed.fetch_document("42"))
    reply = document.entries[0].first_reply
    assert reply is not None
    assert reply.author.is_up
    assert "值得讨论的回复" in "".join(part.text for part in reply.content)
    assert len(calls) == 1


def test_sublist_is_bounded_and_only_fills_missing_usable_reply():
    feed, calls = make_feed([
        {"result": 0, "rootComments": [raw(1, subCommentCount=20)],
         "subCommentsMap": {"1": {"pcursor": "next", "subComments": [raw(2, "@朋友")]}}},
        {"result": 0, "subComments": [raw(3, "@朋友")], "pcursor": "again"},
        {"result": 0, "subComments": [raw(4, "实际有效的回复")], "pcursor": "more"},
    ])
    document = asyncio.run(feed.fetch_document("42"))
    assert document.entries[0].first_reply is not None
    assert document.entries[0].first_reply.content[0].text == "实际有效的回复"
    assert len(calls) == 3
    assert calls[1][0].endswith("/comment/sublist")
    assert calls[1][1]["params"] == {
        "sourceId": "42", "sourceType": 3, "rootCommentId": "1", "pcursor": "next", "page": 1}
    assert calls[2][1]["params"]["pcursor"] == "again"
    assert calls[2][1]["params"]["page"] == 2


def test_sublist_exhaustion_never_exceeds_two_requests_per_displayed_root():
    feed, calls = make_feed([
        {"result": 0, "rootComments": [raw(1, subCommentCount=20)]},
        {"result": 0, "subComments": [raw(2, "@朋友")], "pcursor": "next"},
        {"result": 0, "subComments": [], "pcursor": "more"},
    ])
    document = asyncio.run(feed.fetch_document("42"))
    assert document.entries[0].first_reply is None
    assert len(calls) == 3


def test_ubb_text_images_and_unknown_emotes_have_safe_minimal_mapping(monkeypatch):
    feed, calls = make_feed([{
        "result": 0, "rootComments": [raw(
            text="[b]正文&amp;内容[/b]\n[url=https://example.com]链接[/url]"
                 "[emot=acfun,1788/][img]https://example.com/ubb.jpg[/img]",
            imageUrl="//example.com/photo.jpg", headUrl="//example.com/avatar.jpg",
            postDate="10-20 15:29")],
    }])
    # QR detection is external image IO, not part of this adapter test.
    async def no_qr(url):
        return False
    monkeypatch.setattr(feed.comment_filter, "_has_qrcode", no_qr)
    document = asyncio.run(feed.fetch_document("42"))
    entry = document.entries[0]
    assert entry.author.avatar == "https://example.com/avatar.jpg"
    assert entry.images == ["https://example.com/photo.jpg", "https://example.com/ubb.jpg"]
    assert entry.time_text == "10-20 15:29"
    assert any(part.kind == "line-break" for part in entry.content)
    text = "".join(part.text for part in entry.content)
    assert "正文&内容" in text and "链接" in text
    assert "[b]" not in text and "[url=" not in text and "[img]" not in text
    # Do not invent unverified CDN URLs for opaque AcFun emotion IDs.
    assert "[emot=acfun,1788/]" in text
    assert len(calls) == 1


@pytest.mark.parametrize("page", [
    {"result": 1, "rootComments": [raw()]}, None, [], {"result": 0, "rootComments": []},
])
def test_api_failure_or_empty_feed_returns_none(page):
    feed, _ = make_feed([page])
    assert asyncio.run(feed.fetch_document("42")) is None


def test_transport_failure_propagates():
    feed, _ = make_feed([RuntimeError("network unavailable")])
    with pytest.raises(RuntimeError, match="network unavailable"):
        asyncio.run(feed.fetch_document("42"))


def test_disabled_settings_do_not_make_requests():
    feed, calls = make_feed([])
    feed.settings = CommentSettings.from_config({"comments": {"enabled": False}}, "acfun")
    assert asyncio.run(feed.fetch_document("42")) is None
    assert calls == []


def test_reply_target_is_structural_prefix_and_not_lost():
    feed, _ = make_feed([{
        "result": 0, "rootComments": [raw()],
        "subCommentsMap": {"1": {"subComments": [raw(2, "这个细节很重要", replyToUserName="某人")]}},
    }])
    reply = asyncio.run(feed.fetch_document("42")).entries[0].first_reply
    assert reply is not None
    assert "某人" in "".join(part.text for part in reply.content)


def test_missing_ids_do_not_drop_unrelated_roots():
    feed, _ = make_feed([{"result": 0, "rootComments": [
        raw(None, "第一条有意义的评论"), raw(None, "第二条不同的评论"),
    ]}])
    assert len(asyncio.run(feed.fetch_document("42")).entries) == 2


def test_no_more_cursor_stops_root_pagination():
    feed, calls = make_feed([{"result": 0, "totalPage": 99,
                              "pcursor": "no_more", "rootComments": [raw()]}])
    assert asyncio.run(feed.fetch_document("42")) is not None
    assert len(calls) == 1
