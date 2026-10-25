"""Bilibili descriptions must not gain plugin-owned presentation labels."""

import asyncio

import pytest

from astrbot_plugin_parser_x.core.parsers.bilibili import BilibiliParser


@pytest.mark.parametrize(
    "description",
    [
        "这是原始描述。",
        "简介: 这是作者自己写的前缀",
        "简介：中文冒号也属于原文",
        "  首行\n\t简介字样留在正文里\r\n末行  ",
        " \t\n",
        "",
    ],
    ids=["plain", "author-prefix", "fullwidth-prefix", "multiline", "whitespace", "empty"],
)
def test_bilibili_video_keeps_original_description_without_plugin_prefix(
    tmp_path, description
):
    raw_info = {
        "bvid": "BV1xx411c7mD",
        "aid": 170001,
        "videos": 1,
        "tid": 17,
        "tname": "单机游戏",
        "copyright": 1,
        "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
        "title": "测试视频",
        "pubdate": 1_700_000_000,
        "ctime": 1_700_000_000,
        "desc": description,
        "state": 0,
        "duration": 125,
        "owner": {"mid": 42, "name": "UP主", "face": ""},
        "pages": [
            {
                "cid": 100,
                "page": 1,
                "from": "vupload",
                "part": "正片",
                "duration": 125,
                "vid": "",
                "weblink": "",
                "dimension": {"width": 1920, "height": 1080, "rotate": 0},
            }
        ],
    }

    class FakeDownloader:
        async def streamd(self, _url, *, file_name, **_kwargs):
            return tmp_path / file_name

    async def run():
        parser = BilibiliParser(
            {"cache_dir": str(tmp_path), "comments": {"bilibili": False}},
            FakeDownloader(),
        )

        async def fake_get_video(**_kwargs):
            return object()

        async def fake_get_info(_video, _key):
            return raw_info

        async def fake_get_streams(_video, _index, _key):
            return [(64, ["https://example.com/video.mp4"])], [], {}

        parser._get_video = fake_get_video
        parser._get_video_info_cached = fake_get_info
        parser._get_stream_ladders_with_qn_fallback = fake_get_streams
        result = await parser.parse_video(bvid=raw_info["bvid"])
        await result.contents[0].get_path()
        return result

    result = asyncio.run(run())

    assert result.text == (description or None)
