"""Hide only a heading that duplicates the complete visible caption."""

import asyncio
from copy import deepcopy

import pytest

from astrbot_plugin_parser_x.core.parsers.douyin import DouyinParser
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


def render(**kwargs):
    return UnifiedCardRenderer(None).build_html(
        platform_name="抖音", platform_key="douyin", author_name="作者", **kwargs
    )


def test_real_douyin_builder_caption_is_rendered_once_without_mutating_result(tmp_path):
    caption = "抖音作者的完整描述\n第二行仍然保留"
    parser = DouyinParser(
        {"cache_dir": str(tmp_path), "comments": {"douyin": False}}, object()
    )
    result = asyncio.run(parser._build_result_from_aweme(
        {
            "aweme_id": "123",
            "desc": caption,
            "create_time": 1_700_000_000,
            "author": {"nickname": "作者"},
        },
        "123",
    ))
    original_extra = deepcopy(result.extra)
    assert result.title == caption
    assert result.text is None
    assert result.extra["text_card_flow"] == [{"type": "text", "text": caption}]

    html = render(
        title=result.title,
        text=result.text or "",
        content_blocks=result.extra["text_card_flow"],
    )

    assert "<h1>" not in html
    assert html.count(caption) == 1
    assert f'<div class="flow-text">{caption}</div>' in html
    assert result.title == caption
    assert result.text is None
    assert result.extra == original_extra


def test_equal_plain_caption_hides_heading_but_keeps_body_and_metrics():
    html = render(title="  完整描述  ", text="\n完整描述\t", metrics={"点赞": 12})

    assert "<h1>" not in html
    assert html.count("完整描述") == 1
    assert '<section class="copy-block"><div class="text">\n完整描述\t</div></section>' in html
    assert '<section class="primary-block"><div class="metrics">' in html
    assert "<span>点赞</span><strong>12</strong>" in html


def test_complete_flow_caption_hides_heading_preserves_images_order_and_input():
    blocks = [
        {"type": "text", "text": "  第一段  "},
        {"type": "image", "url": "https://example.com/first.jpg?size=large&v=1"},
        ("text", "\n第二段\n"),
        {"kind": "image", "src": "https://example.com/second.jpg"},
    ]
    before = deepcopy(blocks)
    html = render(title=" 第一段\n\n第二段 ", text="未渲染的原始备用正文", content_blocks=blocks)

    assert "<h1>" not in html
    # Preserve source whitespace while comparing trimmed copies for title dedup.
    assert '<div class="flow-text">  第一段  </div>' in html
    assert '<div class="flow-text">\n第二段\n</div>' in html
    assert html.count('class="flow-image"') == 2
    assert html.index("第一段") < html.index("first.jpg?size=large&amp;v=1") < html.index("第二段") < html.index("second.jpg")
    assert "未渲染的原始备用正文" not in html
    assert blocks == before


@pytest.mark.parametrize(
    ("title", "text", "blocks"),
    [
        ("独立标题", "作者正文", None),
        ("前缀", "前缀还有更多正文", None),
        ("完整正文和额外标题文字", "完整正文", None),
        ("第一段", "", [("text", "第一段"), ("text", "额外正文")]),
        ("第一段 第二段", "", [("text", "第一段"), ("text", "第二段")]),
        ("备用正文", "备用正文", [("text", "真正渲染的正文")]),
    ],
    ids=["distinct", "title-prefix", "body-prefix", "extra-block", "nonexact-whitespace", "raw-text-not-body"],
)
def test_distinct_or_partial_titles_are_not_removed(title, text, blocks):
    html = render(title=title, text=text, content_blocks=blocks)
    assert f"<h1>{title}</h1>" in html


def test_image_only_flow_does_not_compare_title_with_unused_raw_text():
    html = render(
        title="唯一的说明", text="唯一的说明",
        content_blocks=[{"type": "image", "url": "https://example.com/image.jpg"}],
    )
    assert "<h1>唯一的说明</h1>" in html
    assert html.count("唯一的说明") == 1
    assert 'class="flow-image"' in html
    assert 'class="copy-block"' not in html


@pytest.mark.parametrize(
    "blocks",
    [None, [], (), "invalid", {"type": "text", "text": "完整描述"},
     [None, {}, ("unknown", "完整描述"), ("text", "  "), ("image", "")]],
    ids=["none", "empty-list", "empty-tuple", "invalid-string", "invalid-mapping", "invalid-entries"],
)
def test_empty_or_malformed_flow_uses_plain_body_for_equality(blocks):
    html = render(title="完整描述", text="完整描述", content_blocks=blocks)
    assert "<h1>" not in html
    assert '<div class="text">完整描述</div>' in html
    assert 'class="content-flow"' not in html


def test_rich_multiline_caption_keeps_url_emote_mentions_and_escaped_text():
    caption = "第一行 <原文>\n@读者 https://example.com/path?a=1&b=2 [开心]\n最后一行"
    html = render(
        title=caption, text="", content_blocks=[("text", caption)],
        emotes={"[开心]": "https://example.com/happy.png"},
    )

    assert "<h1>" not in html
    assert html.count("第一行 &lt;原文&gt;\n") == 1
    assert '<span class="text-link text-mention">@读者</span>' in html
    assert '<span class="text-link">https://example.com/path?a=1&amp;b=2</span>' in html
    assert html.count('class="inline-emote"') == 1
    assert 'src="https://example.com/happy.png" alt="[开心]"' in html
    assert "\n最后一行</div>" in html


@pytest.mark.parametrize("text,blocks", [("", None), ("  ", []), ("", [("image", "image.jpg")])])
def test_title_without_a_visible_text_body_is_kept(text, blocks):
    html = render(title="仅有标题", text=text, content_blocks=blocks)
    assert "<h1>仅有标题</h1>" in html
