"""抖音图文作品里的「视频幻灯片」必须按视频交付。

背景：`https://www.douyin.com/note/<id>` 这类地址不会走 parse_slides，
拿到的是降级后的 SSR 回流数据 —— video 对象被整个剥掉，视频幻灯片只剩
一张封面静图，用户看到的结果就是"本该是视频的被解析成了图片"。
slidesinfo 接口保留完整 video 对象，解析器必须据此补回视频。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.data import ImageContent, VideoContent
from astrbot_plugin_parser_x.core.parsers.douyin import DouyinParser
from astrbot_plugin_parser_x.core.parsers.douyin import _is_video_slide, _slide_duration


class _Task:
    """最小化的 awaitable 桩，避免依赖事件循环状态。"""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _run():
            return self._value

        return _run().__await__()


class _Downloader:
    def __call__(self, *args, **kwargs):
        return self

    def download_img(self, url, **kwargs):
        return _Task(Path("/tmp/douyin_slide.jpg"))

    def download_video(self, url, **kwargs):
        return _Task(Path("/tmp/douyin_slide.mp4"))

    async def ytdlp_extract_info(self, *args, **kwargs):
        raise RuntimeError("ytdlp 未参与本用例")


def _still_item(url: str) -> dict:
    return {
        "clip_type": 2,
        "url_list": [url],
        "width": 1080,
        "height": 1440,
    }


def _video_item(cover: str, play: str, duration: int) -> dict:
    return {
        "clip_type": 4,
        "url_list": [cover],
        "width": 720,
        "height": 1312,
        "video": {
            "duration": duration,
            "width": 1312,
            "height": 720,
            "cover": {"url_list": [cover]},
            "play_addr": {"url_list": [play], "uri": "v0300fg10000dbg5kuvog65hq6686m70"},
        },
    }


def _aweme(items: list[dict]) -> dict:
    return {
        "aweme_id": "7701254865620172362",
        "desc": "#申鹤#试衣试妆",
        "create_time": 1793088129,
        "aweme_type": 0,
        "author": {"nickname": "醒点十"},
        "images": items,
        "video": {
            "duration": 0,
            "play_addr": {"uri": "https://sf3-cdn-tos.douyinstatic.com/obj/ies-music/bgm.mp3"},
        },
    }


def _parser() -> DouyinParser:
    return DouyinParser({"cookies": {}, "performance": {}}, _Downloader())


@pytest.mark.parametrize(
    ("duration", "clip_type", "expected"),
    [
        (18400, 4, True),    # 真视频幻灯片
        (1500, 4, False),    # 实况图：clip_type 也是 4，但时长短，必须按静态图
        (1500, 2, False),    # clip_type=2 静态图
        (0, 4, False),       # 没有时长信息，按静态图处理
        (5000, 0, True),     # 无 clip_type 时按时长推断
        (1200, 0, False),    # 太短，视为实况图
    ],
)
def test_is_video_slide_discriminates_live_photo(duration, clip_type, expected):
    item = {
        "clip_type": clip_type,
        "video": {"duration": duration},
    }
    assert _is_video_slide(item) is expected


def test_is_video_slide_requires_video_object():
    assert _is_video_slide({"clip_type": 4, "url_list": ["https://x/a.jpg"]}) is False
    assert _slide_duration({"video": {"duration": "18400"}}) == 18400
    assert _slide_duration({"video": None}) == 0


def test_gallery_keeps_video_slide_as_video():
    parser = _parser()
    aweme = _aweme(
        [
            _still_item("https://p3-pc-sign.douyinpic.com/still-1.jpg"),
            _video_item(
                "https://p3-pc-sign.douyinpic.com/cover-2.jpg",
                "https://v5-se-ex-mc-default.365yg.com/video/slide-2.mp4",
                18400,
            ),
        ]
    )

    contents = parser._gallery_contents(aweme, parser.android_headers)

    assert [type(c) for c in contents] == [ImageContent, VideoContent]
    assert contents[1].duration == pytest.approx(18.4)


def test_gallery_live_photo_stays_still_image():
    """实况图（时长很短）仍按静态图交付，不能被改成视频。"""
    parser = _parser()
    aweme = _aweme(
        [
            _video_item(
                "https://p3-pc-sign.douyinpic.com/live-cover.jpg",
                "https://v5-se-ex-mc-default.365yg.com/video/live-short.mp4",
                1500,
            )
        ]
    )

    contents = parser._gallery_contents(aweme, parser.android_headers)

    assert [type(c) for c in contents] == [ImageContent]


def test_enrichment_is_skipped_when_result_already_has_video():
    parser = _parser()
    called = {"n": 0}

    async def _fail(_vid):
        called["n"] += 1
        return None

    parser._fetch_slides_aweme = _fail
    result = SimpleNamespace(has_video=True, contents=[ImageContent(_Task(Path("/tmp/a.jpg")))], url="u")

    out = asyncio.run(parser._enrich_gallery_video_slides(result, "7701254865620172362"))

    assert out is result
    assert called["n"] == 0


def test_enrichment_is_skipped_for_pure_video_posts():
    parser = _parser()
    called = {"n": 0}

    async def _fail(_vid):
        called["n"] += 1
        return None

    parser._fetch_slides_aweme = _fail
    result = SimpleNamespace(has_video=False, contents=[VideoContent(_Task(Path("/tmp/v.mp4")))], url="u")

    out = asyncio.run(parser._enrich_gallery_video_slides(result, "7701254865620172362"))

    assert out is result
    assert called["n"] == 0


def test_enrichment_replaces_result_when_slidesinfo_has_video_slide():
    parser = _parser()
    parser._fetch_slides_aweme = lambda vid: _return(_aweme_slidesinfo())

    degraded = SimpleNamespace(
        has_video=False,
        contents=[ImageContent(_Task(Path("/tmp/cover.jpg")))],
        url="https://www.douyin.com/note/7701254865620172362",
    )

    out = asyncio.run(parser._enrich_gallery_video_slides(degraded, "7701254865620172362"))

    assert out is not degraded
    assert out.has_video is True
    assert out.url == degraded.url


def _aweme_slidesinfo() -> dict:
    return _aweme(
        [
            _still_item("https://p3-pc-sign.douyinpic.com/still-1.jpg"),
            _video_item(
                "https://p3-pc-sign.douyinpic.com/cover-2.jpg",
                "https://v5-se-ex-mc-default.365yg.com/video/slide-2.mp4",
                18400,
            ),
        ]
    )


def _return(value):
    async def _coro():
        return value

    return _coro()


def _mixed_aweme_with_video_slide() -> dict:
    return _aweme(
        [
            _still_item("https://p3-pc-sign.douyinpic.com/still-1.jpg"),
            _video_item(
                "https://p3-pc-sign.douyinpic.com/cover-2.jpg",
                "https://v5-se-ex-mc-default.365yg.com/video/slide-2.mp4",
                18400,
            ),
        ]
    )


def test_mixed_media_is_marked_for_forward_delivery():
    """图 + 视频的作品必须打 mixed_post_body_card，否则投递层会丢掉全部原图。"""
    from astrbot_plugin_parser_x.core.data import VideoContent as _Video

    parser = _parser()
    result = asyncio.run(
        parser._build_result_from_aweme(
            _mixed_aweme_with_video_slide(),
            "7701254865620172362",
            media_headers=parser.android_headers,
        )
    )

    assert [type(c) for c in result.contents] == [ImageContent, _Video]
    assert result.has_video is True
    assert result.extra.get("mixed_post_body_card") is True


def test_pure_image_note_is_not_marked_mixed():
    parser = _parser()
    aweme = _aweme(
        [
            _still_item("https://p3-pc-sign.douyinpic.com/still-1.jpg"),
            _still_item("https://p3-pc-sign.douyinpic.com/still-2.jpg"),
        ]
    )

    result = asyncio.run(
        parser._build_result_from_aweme(
            aweme, "7701254865620172362", media_headers=parser.android_headers
        )
    )

    assert result.has_video is False
    assert "mixed_post_body_card" not in result.extra
