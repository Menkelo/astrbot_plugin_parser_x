"""B站置顶评论必须扛得住过滤器截断。

历史回归：置顶评论由接口 ``top_replies`` 下发，落在候选列表首位，但
``build_document`` 只做 ``comment_filter.apply(candidates, limit=self.limit)``；
一旦置顶条被判为低信息量/广告/重复，或被前面几条挤掉，用户就只会看到
「置顶没了」。修法与知乎一致：置顶不参与 limit 竞争，过滤后接回首位。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge,
    BiliCommentEntry,
    BiliRichPart,
)
from astrbot_plugin_parser_x.core.parsers.bilibili.comment_feed import (
    BiliCommentFeed,
    _RawCommentFeed,
)


def _entry(nickname: str, text: str, *, pinned: bool = False) -> BiliCommentEntry:
    return BiliCommentEntry(
        author=BiliAuthorBadge(nickname=nickname),
        content=[BiliRichPart("text", text=text)],
        pinned=pinned,
    )


def _feed(entries: list[BiliCommentEntry], *, limit: int = 1) -> BiliCommentFeed:
    feed = BiliCommentFeed.__new__(BiliCommentFeed)
    feed.limit = limit
    feed._avatar_data_uri_cache = {}
    feed.parser = SimpleNamespace(config={})
    feed._image_url = lambda value: value or ""
    feed._count_text = lambda value: str(value)

    async def _fetch(oid: int, type_: int) -> _RawCommentFeed:
        return _RawCommentFeed([], "", 2)

    captured: list[BiliCommentEntry] = []

    def adapt_comment(item, owner_mid=None, *, nested=False):
        return item

    async def _apply(candidates, *, limit):
        captured.append(list(candidates))
        return list(candidates)[: max(1, int(limit))]

    async def _apply_keeping_pinned(candidates, *, limit):
        pinned = [entry for entry in candidates if entry.pinned]
        rest = [entry for entry in candidates if not entry.pinned]
        captured.append(list(rest))
        filtered = list(rest)[: max(1, int(limit))]
        return [*pinned, *filtered]

    async def _embed_avatars(_entries):
        return None

    feed.fetch = _fetch
    feed.adapt_comment = adapt_comment
    feed.comment_filter = SimpleNamespace(
        apply=_apply, apply_keeping_pinned=_apply_keeping_pinned,
    )
    feed._embed_avatars = _embed_avatars
    feed._candidates = entries
    feed._captured = captured
    return feed


@pytest.mark.asyncio
async def test_pinned_survives_limit_and_stays_first():
    pinned = _entry("UP", "置顶广告位", pinned=True)
    others = [_entry("A", "普通评论一"), _entry("B", "普通评论二")]
    feed = _feed([pinned, *others], limit=1)

    async def _fetch(oid: int, type_: int) -> _RawCommentFeed:
        return _RawCommentFeed(feed._candidates, "", 2)

    feed.fetch = _fetch
    document = await feed.build_document(1, 1, video_title="t", video_cover=None)

    assert document is not None
    assert [entry.pinned for entry in document.entries] == [True, False]
    assert document.entries[0].author.nickname == "UP"
    # 置顶条不能进入普通候选，否则仍会占用 limit 名额
    assert feed._captured == [others]


@pytest.mark.asyncio
async def test_without_pinned_entry_behaviour_is_unchanged():
    others = [_entry("A", "普通评论一"), _entry("B", "普通评论二")]
    feed = _feed(list(others), limit=1)

    async def _fetch(oid: int, type_: int) -> _RawCommentFeed:
        return _RawCommentFeed(feed._candidates, "", 2)

    feed.fetch = _fetch
    document = await feed.build_document(1, 1, video_title="t", video_cover=None)

    assert document is not None
    assert [entry.author.nickname for entry in document.entries] == ["A"]
