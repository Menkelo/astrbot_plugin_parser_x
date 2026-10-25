"""Only post media, not Douyin's surrounding commerce/UI assets, are images."""

import pytest

from core.parsers.douyin.extractor import extract_static_image_urls_deep


# Minimal raw-aweme projection captured through the parser for
# https://v.douyin.com/s8J0hp1hjRs/ (7698773948968827049).
COMMERCE_ICON = "https://p3-sign.douyinpic.com/obj/33026000f376a8c24d040"


def test_video_without_gallery_does_not_extract_commerce_anchor_icon():
    aweme = {
        "aweme_id": "7698773948968827049",
        "aweme_type": 4,
        "images": None,
        "image_infos": None,
        "image_post_info": None,
        "image_list": None,
        "video": {"play_addr": {"url_list": ["https://example.com/video/post.mp4"]}},
        "aweme_anchor_info": {"icon": {"url_list": [COMMERCE_ICON]}},
    }

    assert extract_static_image_urls_deep(aweme) == []


@pytest.mark.parametrize("root", ["images", "image_infos", "image_post_info", "image_list"])
def test_declared_gallery_roots_preserve_real_images(root):
    # Small images are still valid post media; eligibility is semantic, not size.
    first = "https://example.com/gallery/first.png"
    second = "https://example.com/gallery/second.png"
    items = [{"url_list": [first]}, {"url_list": [second]}]
    value = {"images": items} if root == "image_post_info" else items
    aweme = {root: value, "aweme_anchor_info": {"icon": {"url_list": [COMMERCE_ICON]}}}

    assert extract_static_image_urls_deep(aweme) == [first, second]


@pytest.mark.parametrize("empty_root", [None, [], {}, [{"url_list": []}]])
def test_empty_media_roots_never_fall_back_to_other_aweme_assets(empty_root):
    aweme = {
        "images": empty_root,
        "aweme_anchor_info": {"icon": {"url_list": [COMMERCE_ICON]}},
        "new_ui_widget": {"image": "https://example.com/widget.png"},
    }

    assert extract_static_image_urls_deep(aweme) == []


def test_nested_gallery_keeps_static_images_but_not_live_video_or_avatar():
    still = "https://example.com/gallery/still.png"
    aweme = {
        "image_post_info": {
            "image_list": [{
                "display_image": {"url_list": [still, still]},
                "video": {"play_addr": {"url_list": ["https://example.com/video/live.mp4"]}},
                "author": {"avatar": "https://example.com/avatar.png"},
            }],
        },
    }

    assert extract_static_image_urls_deep(aweme) == [still]
