"""Offline regressions for tokens observed in the real Kuaishou share API."""
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.parsers.kuaishou_emotes import (
    kuaishou_comment_parts, load_kuaishou_emotes,
)
from astrbot_plugin_parser_x.core.parsers.kuaishou_comment import KuaishouCommentFeed
from astrbot_plugin_parser_x.core.comment_settings import CommentSettings


def test_first_party_catalog_covers_observed_tokens():
    catalog = load_kuaishou_emotes()
    assert len(catalog) >= 700
    for token in ("[生气]", "[汗]", "[捂脸]", "[亲亲]", "[憨笑哪吒]", "[冰冰冰]", "[略略略]"):
        assert catalog[token].startswith(("https://p66-plat.wskwai.com/kimg/bs2/emotion/",
                                          "https://p66.a.yximgs.com/bs2/emotion/"))


def test_order_repeated_tokens_newlines_and_unknown_text():
    text = "老师[汗][汗]未知[不存在的快手表情]\n[捂脸]😂"
    parts = kuaishou_comment_parts(text)
    assert [p.kind for p in parts] == ["text", "emote", "emote", "text", "line-break", "emote", "text"]
    assert [p.text for p in parts] == ["老师", "[汗]", "[汗]", "未知[不存在的快手表情]", "", "[捂脸]", "😂"]
    assert parts[1].url == parts[2].url


def test_empty_and_literal_brackets_are_lossless():
    assert kuaishou_comment_parts("") == []
    assert [(p.kind, p.text) for p in kuaishou_comment_parts("[] [arbitrary] 😀")] == [("text", "[] [arbitrary] 😀")]


@pytest.mark.asyncio
async def test_share_root_and_inline_reply_both_tokenized_without_extra_requests():
    pages = iter([{"result": 2}, {"result": 1, "commentCount": 1,
        "rootComments": [{"type": 0, "comment_id": "r", "content": "老师[汗]"}],
        "subCommentsMap": {"r": {"subComments": [{"type": 0, "content": "[憨笑哪吒]"}]}}}])
    calls = []
    async def post(url, **kwargs):
        calls.append(url)
        data = next(pages)
        return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)
    parser = SimpleNamespace(client=SimpleNamespace(post=post), headers={}, ios_headers={}, config={},
        comment_settings=CommentSettings.from_config({"comments": {"enabled": True}}, "kuaishou"))
    document = await KuaishouCommentFeed(parser).fetch_document("3xmmnz8yiftpsyi")
    assert len(calls) == 2
    assert document.entries[0].content[1].kind == "emote"
    assert document.entries[0].first_reply.content[0].kind == "emote"


def test_renderer_emits_images_and_literal_unknown_fallback():
    from astrbot_plugin_parser_x.core.comment_render_base import CommentCanvasBase
    parts = kuaishou_comment_parts("老师[汗][不存在的快手表情]")
    html = CommentCanvasBase()._render_rich_text(parts)
    assert html.count('<img class="emote"') == 1
    assert 'alt="[汗]"' in html
    assert 'document.createTextNode(this.alt)' in html
    assert "[不存在的快手表情]" in html


def test_rest_v2_entry_also_tokenized():
    parser = SimpleNamespace(headers={}, config={}, comment_settings=CommentSettings.from_config({}, "kuaishou"))
    entry = KuaishouCommentFeed(parser)._entry({"content": "[亲亲]老师敢不敢结婚？[憨笑哪吒]"}, "")
    assert [p.kind for p in entry.content] == ["emote", "text", "emote"]
