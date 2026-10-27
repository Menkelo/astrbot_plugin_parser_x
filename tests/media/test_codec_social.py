"""Offline source-codec selection through social parser models and result paths."""
import asyncio
from types import SimpleNamespace

import msgspec
import pytest

from astrbot_plugin_parser_x.core.parsers.xiaohongshu import XiaoHongShuParser, Video
from astrbot_plugin_parser_x.core.parsers.gallery import motion_url
from astrbot_plugin_parser_x.core.parsers.douyin import DouyinParser
from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo
from astrbot_plugin_parser_x.core.parsers.gallery import preferred_motion_url


def xhs_parser(tmp_path, preference):
    parser = XiaoHongShuParser(
        {"cache_dir": str(tmp_path), "performance": {"video_codec": preference}}, None
    )
    parser.create_video_content = lambda url, *a, **kw: SimpleNamespace(url=url)
    return parser


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("preference, expected", [("auto", "360"), ("avc", "360"), ("hevc", "720")])
def test_xhs_prefers_existing_hevc_in_both_note_paths(tmp_path, discovery, preference, expected):
    parser = xhs_parser(tmp_path, preference)
    note = {
        "type": "video", "user": {"nickName" if discovery else "nickname": "author"},
        "video": {"media": {"stream": {
            "h264": [{"masterUrl": "https://cdn.example/360.mp4"}],
            "h265": [{"masterUrl": "https://cdn.example/720.mp4"}],
        }}},
    }
    result = (parser._process_discovery_data(note, {}) if discovery
              else parser._process_explore_data(note))
    assert result.contents[0].url == f"https://cdn.example/{expected}.mp4"


@pytest.mark.parametrize("preference, expected", [("auto", "default"), ("hevc", "hevc"), ("avc", "avc")])
def test_gallery_explicit_codec_fields(preference, expected):
    raw = {"play_addr": {"url_list": ["https://cdn.example/default.mp4"]},
           "play_addr_h264": {"url_list": ["https://cdn.example/avc.mp4"]},
           "play_addr_265": {"url_list": ["https://cdn.example/hevc.mp4"]}}
    # Existing gallery default is h264 before generic play_addr.
    if preference == "auto":
        expected = "avc"
    assert motion_url(raw, preference) == f"https://cdn.example/{expected}.mp4"


@pytest.mark.parametrize("preference, expected", [("auto", "default"), ("avc", "default"), ("hevc", "hevc")])
def test_douyin_result_prefers_existing_hevc_without_request(tmp_path, preference, expected):
    class Downloader:
        def download_video(self, url, **kwargs):
            return url
    parser = DouyinParser({"cache_dir": str(tmp_path), "performance": {"video_codec": preference}}, Downloader())
    parser.create_video_content = lambda url, *args: SimpleNamespace(url=url)
    raw = {"desc": "post", "create_time": 0, "author": {"nickname": "author"},
           "aweme_id": "123", "video": {
               "play_addr": {"url_list": ["https://cdn.example/default.mp4"]},
               "bit_rate": [{"is_h265": 1, "play_addr": {"url_list": ["https://cdn.example/hevc.mp4"]}}],
           }}
    result = asyncio.run(parser._build_result_from_aweme(raw, "123"))
    assert result.contents[0].url == f"https://cdn.example/{expected}.mp4"


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
def test_xhs_absent_target_keeps_original_quality_order(preference):
    video = msgspec.convert({"media": {"stream": {
        "h264": [{"url": "https://cdn.example/720.mp4"},
                 {"url": "https://cdn.example/360.mp4"}],
        "h265": [{"url": ""}],
    }}}, Video)
    assert video.select_video_url(preference) == "https://cdn.example/360.mp4"
    assert video.video_url == "https://cdn.example/360.mp4"


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
def test_unknown_gallery_never_infers_codec_from_url(preference):
    raw = {"play_addr": {"url_list": ["https://hevc.example/h265.mp4"]},
           "bit_rate": [{"play_addr": {"url_list": ["https://avc.example/h264.mp4"]}}]}
    assert preferred_motion_url(raw, preference) is None
    assert motion_url(raw, preference) == "https://hevc.example/h265.mp4"


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
def test_kuaishou_single_existing_source_is_unchanged(tmp_path, preference):
    photo = msgspec.convert({"caption": "post", "timestamp": 0,
        "mainMvUrls": [{"cdn": "cdn", "url": "https://cdn.example/only.mp4"}]}, Photo)
    parser = KuaiShouParser({"cache_dir": str(tmp_path), "performance": {"video_codec": preference}}, None)
    parser.create_video_content = lambda url, *a, **kw: SimpleNamespace(url=url)
    result = parser._build_result_from_photo(photo)
    assert result.contents[0].url == "https://cdn.example/only.mp4"


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
def test_gallery_static_live_photo_does_not_download_motion(tmp_path, preference):
    calls = []
    downloader = SimpleNamespace(download_img=lambda url, **kw: calls.append(url) or url,
                                 download_video=lambda *a, **kw: pytest.fail("motion downloaded"))
    parser = XiaoHongShuParser({"cache_dir": str(tmp_path), "performance": {"video_codec": preference}}, downloader)
    parser._gallery_contents({"imageList": [{"urlDefault": "https://cdn.example/still.jpg",
        "stream": {"h264": [{"masterUrl": "https://cdn.example/avc.mp4"}],
                   "h265": [{"masterUrl": "https://cdn.example/hevc.mp4"}]}}]})
    assert calls == ["https://cdn.example/still.jpg"]
