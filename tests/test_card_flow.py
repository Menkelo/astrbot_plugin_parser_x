"""全平台通用正文卡编排（小黑盒规则）的单元测试。"""

from astrbot_plugin_parser_x.core.card_flow import (
    build_card_extra,
    flow_is_interleaved,
    flow_to_blocks,
)


def test_single_image_becomes_hero_and_leaves_flow():
    """单图帖：那张图升为头图，正文流里不再重复出现。"""
    extra = build_card_extra([("text", "正文"), ("image", "a.jpg")])
    assert extra["text_card_media"] == "a.jpg"
    # 头图统一按 900x480 裁切，单图/视频/封面都走 cover
    assert extra["text_card_media_fit"] == "cover"
    assert [block["type"] for block in extra["text_card_flow"]] == ["text"]
    assert not extra.get("text_card_detach_images")


def test_stacked_multi_image_uses_first_as_hero_and_detaches():
    """多图堆叠：第一张裁成头图进卡片，全部图片仍然单独发送。"""
    extra = build_card_extra(
        [("text", "正文"), ("image", "a.jpg"), ("image", "b.jpg")]
    )
    assert extra["text_card_media"] == "a.jpg"
    assert extra["text_card_media_fit"] == "cover"
    assert extra["text_card_detach_images"] is True
    assert [block["type"] for block in extra["text_card_flow"]] == ["text"]


def test_interleaved_multi_image_stays_in_card():
    """图文交错：图片留在卡片里，按原始顺序穿插。"""
    extra = build_card_extra(
        [
            ("text", "第一段"),
            ("image", "a.jpg"),
            ("text", "第二段"),
            ("image", "b.jpg"),
        ]
    )
    assert not extra.get("text_card_detach_images")
    assert [block["type"] for block in extra["text_card_flow"]] == [
        "text",
        "image",
        "text",
        "image",
    ]


def test_cover_wins_over_first_image():
    """有封面时封面做头图，堆叠的多图仍然拆出去。"""
    extra = build_card_extra(
        [("text", "正文"), ("image", "a.jpg"), ("image", "b.jpg")],
        cover="cover.jpg",
    )
    assert extra["text_card_media"] == "cover.jpg"
    assert extra["text_card_detach_images"] is True


def test_video_cover_also_uses_cover_fit():
    """视频封面和其它头图一样裁切到 900x480，不再单独限高。"""
    extra = build_card_extra([], cover="c.jpg", is_video=True)
    assert extra["text_card_media_fit"] == "cover"


def test_text_only_card_has_no_flow_images():
    extra = build_card_extra([("text", "只有文字")])
    assert extra["render_text_card"] is True
    assert not extra.get("text_card_media")
    assert not extra.get("text_card_detach_images")


def test_flow_is_interleaved_detection():
    assert flow_is_interleaved([("image", "a"), ("text", "x")]) is True
    assert flow_is_interleaved([("text", "x"), ("image", "a")]) is False
    assert flow_is_interleaved(
        [("text", "x"), ("image", "a"), ("image", "b")]
    ) is False


def test_flow_to_blocks_drops_empty_text():
    blocks = flow_to_blocks([("text", ""), ("image", "a.jpg")])
    assert blocks == [{"type": "image", "url": "a.jpg"}]
