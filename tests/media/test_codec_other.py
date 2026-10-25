"""Offline source-codec coverage through the actual platform selectors."""

import json
import re
from types import SimpleNamespace

import pytest

from core.parsers.weibo import WeiboParser
from core.parsers.xiaoheihe import XiaoHeiHeParser
from core.parsers.ytdlp import AcFunParser


def test_weibo_explicit_preference_uses_playback_codec_evidence():
    page = {
        "urls": {"mp4_720p_mp4": "https://cdn.example/default.mp4"},
        "media_info": {
            "playback_list": [
                {"play_info": {"url": "https://cdn.example/avc.mp4", "video_codec": "avc1.640028"}},
                {"play_info": {"url": "https://cdn.example/hevc.mp4", "video_codec": "hvc1.1.6.L120"}},
            ],
        },
    }
    assert WeiboParser._pick_video_url(page, "hevc") == "https://cdn.example/hevc.mp4"
    assert WeiboParser._pick_video_url(page, "avc") == "https://cdn.example/avc.mp4"
    assert WeiboParser._pick_video_url(page) == "https://cdn.example/default.mp4"


@pytest.mark.parametrize("layout", ["page_info", "mixed", "url_objects"])
def test_weibo_media_selectors_propagate_preference(layout):
    page = {"type": "video", "media_info": {
        "stream_url_hd": "https://cdn.example/default.mp4",
        "playback_list": [{"play_info": {
            "url": "https://cdn.example/hevc.mp4", "video_codec": "hevc",
        }}],
    }}
    if layout == "mixed":
        data = {"mix_media_info": {"items": [{"type": "video", "data": page}]}}
    elif layout == "url_objects":
        data = {"url_objects": [page]}
    else:
        data = {"page_info": page}
    assert WeiboParser._media_specs(data, "hevc")[0][1] == "https://cdn.example/hevc.mp4"
    assert WeiboParser._media_specs(data)[0][1] == "https://cdn.example/default.mp4"


def test_weibo_fid_entry_uses_configured_preference(tmp_path):
    parser = WeiboParser({"cache_dir": str(tmp_path), "performance": {"video_codec": "hevc"}}, None)
    parser.create_video_content = lambda url, **kwargs: url
    parser.create_author = lambda *args: None
    parser.result = lambda **kwargs: kwargs
    info = {"urls": {"mp4_720p_mp4": "https://cdn.example/default.mp4"},
            "playback_list": [{"play_info": {"url": "https://cdn.example/target.mp4", "video_codec": "hevc"}}]}
    assert parser._fid_result(info, "1:2")["contents"] == ["https://cdn.example/target.mp4"]


@pytest.mark.asyncio
async def test_weibo_status_entry_uses_configured_preference_without_network(tmp_path):
    calls = []
    class Downloads:
        def download_video(self, url, **kwargs):
            calls.append(url)
            return tmp_path / "video.mp4"
    parser = WeiboParser({"cache_dir": str(tmp_path), "performance": {"video_codec": "hevc"}}, Downloads())
    async def source(_bid):
        return {"id": "1", "text_raw": "body", "user": {}, "page_info": {
            "type": "video", "media_info": {"stream_url_hd": "https://cdn.example/default.mp4",
                "playback_list": [{"play_info": {"url": "https://cdn.example/target.mp4", "video_codec": "hevc"}}]},
        }}
    parser._fetch_status_data = source
    await parser._parse_weibo(re.search(r"weibo.com/1/(\w+)", "weibo.com/1/Abc"))
    assert calls == ["https://cdn.example/target.mp4"]


@pytest.mark.asyncio
async def test_ytdlp_video_entry_passes_preference_to_metadata_and_download(tmp_path):
    calls = []
    class Downloads:
        async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
            calls.append(("metadata", kwargs))
            return SimpleNamespace(author_name="A", uploader="A", channel="A", description="", duration=1, title="T", timestamp=None)
        def download_video(self, url, **kwargs):
            calls.append(("download", kwargs))
            return tmp_path / "video.mp4"
    parser = AcFunParser({"cache_dir": str(tmp_path), "performance": {"video_codec": "hevc"}}, Downloads())
    await parser._parse_ytdlp(re.search(r"https://.*", "https://www.acfun.cn/v/ac1"))
    assert [kwargs.get("video_codec") for _, kwargs in calls] == ["hevc", "hevc"]
    assert calls[0][1]["force_generic_extractor"] is False
    from core.config_policy import SOURCE_MAX_SIZE_MB
    assert calls[1][1]["max_size_mb"] == SOURCE_MAX_SIZE_MB


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
def test_weibo_unknown_or_unusable_codec_candidates_keep_existing_fallback(preference):
    page = {"urls": {"mp4_720p_mp4": "//cdn.example/legacy.mp4"}, "media_info": {
        "playback_list": [
            {"play_info": {"url": "https://cdn.example/hevc.mp4", "video_codec": "unknown"}},
            {"play_info": {"url": "file:///target.mp4", "video_codec": preference}},
        ],
    }}
    assert WeiboParser._pick_video_url(page, preference) == "https://cdn.example/legacy.mp4"


def test_weibo_video_picture_prefers_known_target_over_single_url():
    pic = {"type": "video", "videoSrc": "https://cdn.example/default.mp4", "media_info": {
        "playback_list": [{"play_info": {"url": "https://cdn.example/target.mp4", "video_codec": "hevc"}}],
    }}
    selected = WeiboParser._media_specs({"pics": [pic]}, "hevc")
    assert selected[0][1] == "https://cdn.example/target.mp4"
    assert len(selected) == 1
    assert WeiboParser._media_specs({"pics": [pic]})[0][1] == pic["videoSrc"]


def test_weibo_preferred_candidates_keep_original_source_order():
    page = {"media_info": {"playback_list": [
        {"play_info": {"url": "https://cdn.example/first.mp4", "video_codec": "hevc"}},
        {"play_info": {"url": "https://cdn.example/second.mp4", "video_codec": "hevc"}},
    ]}}
    assert WeiboParser._pick_video_url(page, "hevc") == "https://cdn.example/first.mp4"


@pytest.mark.parametrize("preference", ["auto", "avc", "hevc"])
@pytest.mark.asyncio
async def test_xiaoheihe_single_url_fallback_is_honest_noop(tmp_path, preference):
    calls = []
    class Downloads:
        def download_video(self, url, **kwargs):
            calls.append((url, kwargs))
            return tmp_path / "video.mp4"
    parser = XiaoHeiHeParser({"cache_dir": str(tmp_path), "performance": {"video_codec": preference}}, Downloads())
    link = {"linkid": "1", "video_url": "https://cdn.example/signed.mp4?sign=original", "video_duration": 2}
    content = await parser._video_contents(link)
    assert content[0].duration == 2
    assert calls[0][0] == link["video_url"]
    parser._parse_body({"linkid": "1", "text": json.dumps([{"type": "video", "url": link["video_url"]}])})
    assert calls[1][0] == link["video_url"]
    assert all("video_codec" not in kwargs for _, kwargs in calls)


@pytest.mark.asyncio
async def test_ytdlp_audio_entry_does_not_request_video_codec(tmp_path):
    calls = []
    class Downloads:
        async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(author_name="A", uploader="A", channel="A", description="", duration=1, title="T", timestamp=None)
        def download_audio(self, url, **kwargs):
            calls.append(kwargs)
            return tmp_path / "audio.m4a"
    parser = AcFunParser({"cache_dir": str(tmp_path), "performance": {"video_codec": "hevc"}}, Downloads())
    parser.media_kind = "audio"
    await parser._parse_ytdlp(re.search(r"https://.*", "https://www.acfun.cn/v/ac1"))
    assert all("video_codec" not in kwargs for kwargs in calls)
