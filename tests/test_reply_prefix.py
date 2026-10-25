from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import replace

import pytest

from core.comment_canvas import CommentAuthor, CommentEntry, CommentRichPart
from core.comment_filter import CommentFilter
from core.comment_settings import CommentFilterSettings
from core.parsers.xiaohongshu_comment import XiaohongshuCommentFeed
from core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge,
    BiliCommentEntry,
    BiliRichPart,
)


def _filter(mode: str = "balanced") -> CommentFilter:
    settings = CommentFilterSettings.from_config(
        {"comments": {"filter": {
            "enabled": True,
            "mention_mode": mode,
            "qrcode": False,
            "ads": False,
            "duplicates": False,
            "low_information": False,
        }}}
    )
    return CommentFilter(object(), settings, platform="测试")


@pytest.mark.parametrize("mode", ["balanced", "strict"])
@pytest.mark.parametrize("bilibili", [False, True])
@pytest.mark.parametrize("prefix", [
    [("text", "回复 "), ("highlight", "@某人"), ("text", "：")],
    [("text", "回"), ("highlight", "复 ＠某人"), ("text", ": ")],
    [("highlight", "回复 @某人：")],
])
def test_nested_split_reply_prefix_preserves_rich_body(mode, bilibili, prefix):
    part_type = BiliRichPart if bilibili else CommentRichPart
    entry_type = BiliCommentEntry if bilibili else CommentEntry
    author = BiliAuthorBadge("用户") if bilibili else CommentAuthor("用户")
    body = [
        part_type("text", "正文回复:保留"),
        part_type("emote", "[微笑]", "https://img.example/emote.png"),
        part_type("line-break"),
        part_type("highlight", "第二行回复没关系", "https://example.com/body"),
    ]
    reply = entry_type(author, [part_type(kind, text) for kind, text in prefix] + body)
    main = entry_type(author, [part_type("text", "主评论")], first_reply=reply)

    result = asyncio.run(_filter(mode).apply([main], limit=1))

    assert len(result) == 1
    assert result[0].first_reply is not None
    assert result[0].first_reply.content == body


@pytest.mark.parametrize("mode", ["balanced", "strict", "off"])
def test_original_reply_words_without_target_are_preserved(mode):
    body = [
        CommentRichPart("text", "回复：正文原话"),
        CommentRichPart("line-break"),
        CommentRichPart("text", "这条回复很有用，回复:不是结构前缀"),
        CommentRichPart("emote", "[微笑]", "https://img.example/emote.png"),
    ]
    entry = CommentEntry(CommentAuthor("用户"), body.copy())

    result = asyncio.run(_filter(mode).apply([entry], limit=1))

    # The existing sanitizer applies NFKC; only structural deletion is forbidden.
    assert result[0].content == [
        replace(part, text=unicodedata.normalize("NFKC", part.text)) for part in body
    ]


def test_split_prefix_boundary_keeps_the_remaining_body_part_kind():
    entry = BiliCommentEntry(BiliAuthorBadge("用户"), [
        BiliRichPart("text", "回复 "),
        BiliRichPart("highlight", "@某人"),
        BiliRichPart("highlight", "：正文回复词保留"),
        BiliRichPart("line-break"),
        BiliRichPart("emote", "[微笑]", "https://img.example/emote.png"),
    ])

    result = asyncio.run(_filter().apply([entry], limit=1))

    assert result[0].content == [
        BiliRichPart("highlight", "正文回复词保留"),
        BiliRichPart("line-break"),
        BiliRichPart("emote", "[微笑]", "https://img.example/emote.png"),
    ]


@pytest.mark.parametrize("bilibili", [False, True])
@pytest.mark.parametrize("kind", ["emote", "highlight"])
@pytest.mark.parametrize("with_break", [False, True])
def test_reply_prefix_keeps_unlabelled_rich_media(bilibili, kind, with_break):
    part_type = BiliRichPart if bilibili else CommentRichPart
    entry_type = BiliCommentEntry if bilibili else CommentEntry
    author = BiliAuthorBadge("用户") if bilibili else CommentAuthor("用户")
    media = part_type(kind, "", "https://img.example/body.png")
    body = [media]
    if with_break:
        body.append(part_type("line-break"))
    body.append(part_type("text", " 正文"))
    entry = entry_type(author, [part_type("text", "回复 @某人：")] + body)
    result = asyncio.run(_filter().apply([entry], limit=1))
    # Existing mention cleanup trims the first text fragment; media/line breaks survive.
    assert result[0].content == [*body[:-1], replace(body[-1], text="正文")]


@pytest.mark.parametrize("target_key", ["target_comment", "targetComment"])
def test_xiaohongshu_nested_reply_does_not_prepend_target_name(tmp_path, target_key):
    class FakeParser:
        headers = {}
        cache_dir = tmp_path

    feed = XiaohongshuCommentFeed(FakeParser())
    item = {
        "content": "回复：这是正文[微笑R]\n第二行回复仍保留",
        "user_info": {"nickname": "用户"},
        target_key: {"user_info": {"nickname": "被回复者"}},
    }
    emotes = {"[微笑R]": "https://img.example/emote.png"}
    expected = feed.adapt_comment(item, "", emote_map=emotes)

    result = feed.adapt_comment(item, "", nested=True, emote_map=emotes)

    assert result is not None and expected is not None
    assert result.content == expected.content
    assert any(part.kind == "emote" for part in result.content)
    assert any(part.kind == "line-break" for part in result.content)
    assert "被回复者" not in "".join(part.text for part in result.content)
