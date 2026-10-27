"""跨平台置顶评论必须扛得住过滤器截断。

历史回归：各平台把置顶条和普通评论一起交给 ``comment_filter.apply(limit=N)``，
置顶条一旦被判低信息量/广告/重复，或被前面几条挤出窗口，用户看到的就是
「置顶没了」。统一做法见 ``CommentFilter.apply_keeping_pinned``：置顶不参与
limit 竞争，过滤后重新接回首位。
"""
from __future__ import annotations

import pytest

from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor,
    CommentEntry,
    CommentRichPart,
)
from astrbot_plugin_parser_x.core.comment_filter import CommentFilter
from astrbot_plugin_parser_x.core.comment_settings import CommentFilterSettings


class _StubParser:
    """过滤器只在下载二维码图时用 parser；置顶用例不触发该路径。"""

    async def http_get(self, *args, **kwargs):
        raise AssertionError("置顶用例不应发起网络请求")


def _entry(text: str, *, pinned: bool = False) -> CommentEntry:
    return CommentEntry(
        author=CommentAuthor(nickname="测试"),
        content=[CommentRichPart("text", text=text)],
        pinned=pinned,
    )


def _filter(**overrides) -> CommentFilter:
    settings = CommentFilterSettings(
        enabled=True,
        mention_mode="balanced",
        qrcode=True,
        ads=True,
        duplicates=True,
        low_information=False,
        ad_threshold=6,
        **overrides,
    )
    return CommentFilter(_StubParser(), settings, platform="test")


@pytest.mark.asyncio
async def test_pinned_not_crowded_out_by_limit():
    entries = [
        _entry("这个隧道当时真的太震撼了，几十年才修完"),
        _entry("英法海底隧道讲了243年，看完了真觉得基建不容易"),
        _entry("置顶说明", pinned=True),
    ]
    kept = await _filter().apply_keeping_pinned(entries, limit=1)

    assert [entry.pinned for entry in kept] == [True, False]
    assert kept[0].content[0].text == "置顶说明"


@pytest.mark.asyncio
async def test_pinned_still_filtered_when_genuinely_spam():
    """置顶不等于免过滤：真垃圾内容照样剔掉，不能变成广告后门。"""
    entries = [
        _entry("置顶广告加微信 vx123456 代刷", pinned=True),
        _entry("看完了真觉得基建不容易，工程量大得离谱"),
    ]
    kept = await _filter().apply_keeping_pinned(entries, limit=5)

    assert all(not entry.pinned for entry in kept)
    assert len(kept) == 1


@pytest.mark.asyncio
async def test_pinned_moved_to_head_even_when_sorted_later():
    """平台按点赞/时间排序后，置顶条常常不在数组首位；必须挪到最前面。"""
    entries = [
        _entry("这个隧道当时真的太震撼了，几十年才修完"),
        _entry("英法海底隧道讲了243年，看完了真觉得基建不容易"),
        _entry("置顶说明", pinned=True),
    ]
    kept = await _filter().apply_keeping_pinned(entries, limit=5)

    assert kept[0].pinned is True
    assert [entry.content[0].text[:4] for entry in kept[1:]] == ["这个隧道", "英法海底"]


@pytest.mark.asyncio
async def test_no_pinned_falls_back_to_plain_apply():
    entries = [_entry("评论一"), _entry("评论二")]
    kept = await _filter().apply_keeping_pinned(entries, limit=1)
    plain = await _filter().apply(entries, limit=1)
    assert kept == plain
