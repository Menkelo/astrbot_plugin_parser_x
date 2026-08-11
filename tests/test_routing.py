from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from core.download import VideoInfo
from core.exception import ParseException
from core.parsers import (
    BaseParser,
    KugouMusicParser,
    MiyousheParser,
    QQMusicParser,
    TiebaParser,
    WeixinChannelParser,
    XiaoheiheParser,
)
from core.parsers.ytdlp import AcFunParser
from core.utils import extract_json_url
from core.web_summary import (
    extract_readable_text,
    is_blocked_platform_host,
    validate_public_url,
)


def test_json_share_url_is_extracted_from_onebot_payload():
    payload = {
        "app": "com.tencent.mobileqq",
        "meta": {"detail_1": {"qqdocurl": "https://b23.tv/abc123"}},
    }
    assert extract_json_url(payload) == "https://b23.tv/abc123"


def test_ytdlp_parsers_are_registered_once():
    classes = BaseParser.get_all_subclass()
    names = [(item.__module__, item.__qualname__) for item in classes]
    assert len(names) == len(set(names))
    assert AcFunParser in classes
    assert MiyousheParser in classes
    assert QQMusicParser in classes


def test_foreign_platform_routes_are_not_registered():
    keywords = {
        keyword
        for parser in BaseParser.get_all_subclass()
        for keyword, _ in parser._key_patterns
    }
    assert (
        not {
            "tiktok.com",
            "twitter.com",
            "x.com",
            "instagram.com",
            "youtube.com",
            "youtu.be",
            "music.apple.com",
            "open.spotify.com",
        }
        & keywords
    )
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    platform_keys = set(schema["platforms"]["items"])
    assert (
        not {
            "tiktok",
            "twitter",
            "instagram",
            "youtube",
            "apple_music",
            "spotify",
        }
        & platform_keys
    )


def test_ytdlp_parser_builds_a_downloadable_media_result(tmp_path):
    class FakeDownloader:
        extract_args = None
        download_args = None

        async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
            self.extract_args = (url, cookiefile, kwargs)
            return VideoInfo(
                title="Demo",
                uploader="Uploader",
                duration=12,
                description="Description",
            )

        async def download_video(self, url, **kwargs):
            self.download_args = (url, kwargs)
            output = tmp_path / "demo.mp4"
            output.write_bytes(b"video")
            return output

    async def parse():
        downloader = FakeDownloader()
        parser = AcFunParser(
            {
                "cache_dir": str(tmp_path),
                "performance": {"source_max_size": 42},
            },
            downloader,
        )
        keyword, searched = parser.search_url("https://www.acfun.cn/v/ac123456")
        result = await parser.parse(keyword, searched)
        path = await result.contents[0].get_path()
        return downloader, result, path

    downloader, result, path = asyncio.run(parse())
    assert result.platform.name == "acfun"
    assert result.extra["adapter"] == "yt-dlp"
    assert path.name == "demo.mp4"
    assert downloader.extract_args[2]["force_generic_extractor"] is False
    assert downloader.download_args[1]["max_size_mb"] == 42


def test_domestic_parser_helpers_cover_new_routes():
    assert (
        MiyousheParser.extract_post_id("https://www.miyoushe.com/ys/article/69857339")
        == "69857339"
    )
    assert QQMusicParser.extract_song_identity(
        "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV"
    ) == ("0039MnYb0qxYhV", None)
    assert (
        QQMusicParser.search_url(
            "https://i.y.qq.com/n2/m/share/details/taoge.html?songmid=ABC123"
        )[0]
        == "i.y.qq.com"
    )
    assert XiaoheiheParser.extract_identity(
        "https://www.xiaoheihe.cn/app/game/pc/730"
    ) == ("pc", "730")
    assert (
        XiaoheiheParser.build_hkey(
            "bbs/app/link/tree",
            1700000001,
            "ABCDEF0123456789ABCDEF0123456789",
        )
        == "V2V1Z67"
    )
    assert WeixinChannelParser.extract_feed_credentials(
        "https://channels.weixin.qq.com/feed?token=token123&eid=export456"
    ) == ("token123", "export456")
    assert (
        WeixinChannelParser.generate_rid(1700000000, "abcdef01") == "6553f100-abcdef01"
    )

    share = KugouMusicParser.parse_share_data(
        'var dataFromSmarty = [{"author_name":"歌手","song_name":"歌名",'
        '"hash":"ABC","album_id":1,"mixsongid":2}], //当前页面歌曲信息'
    )
    assert share == {
        "author": "歌手",
        "title": "歌名",
        "hash": "ABC",
        "album_id": "1",
        "album_audio_id": "2",
    }
    assert (
        KugouMusicParser._merge_cookies("token=old; userid=1", "token=new; dfid=device")
        == "token=new; userid=1; dfid=device"
    )

    tieba = TiebaParser._parse_api_post(
        {
            "post_list": [
                {
                    "title": "主题",
                    "content": [
                        {"text": "正文"},
                        {"cdn_src": "https://tiebapic.baidu.com/forum/demo.jpg"},
                    ],
                }
            ]
        }
    )
    assert tieba["title"] == "主题"
    assert tieba["text"] == "正文"
    assert len(tieba["images"]) == 1


def test_manifest_has_a_reviewable_upstream_baseline():
    manifest_path = Path(__file__).parents[1] / "upstream" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["repository"].endswith("rconsole-plugin.git")
    assert len(manifest["commit"]) == 40
    assert manifest["strategy"] == "semantic-port"


def test_web_summary_extracts_text_and_blocks_unsafe_or_removed_hosts():
    title, text = extract_readable_text(
        "<html><head><title>标题</title><script>ignore()</script></head>"
        "<body><article><h1>正文</h1><p>第一段</p></article></body></html>"
    )
    assert title == "标题"
    assert "正文" in text and "第一段" in text
    assert "ignore" not in text
    assert is_blocked_platform_host("www.youtube.com")
    assert is_blocked_platform_host("open.spotify.com")
    with pytest.raises(ParseException, match="内网|本机|保留"):
        asyncio.run(validate_public_url("http://127.0.0.1/secret"))


def test_onebot_message_chain_uses_public_aiocqhttp_conversion():
    async def convert_chain():
        return await AiocqhttpMessageEvent._parse_onebot_json(
            MessageChain(
                [Plain("hello"), Node(uin="1", name="Parser X", content=[Plain("ok")])]
            )
        )

    converted = asyncio.run(convert_chain())
    assert converted[0] == {"type": "text", "data": {"text": "hello"}}
    assert converted[1]["type"] == "node"


def test_plugin_initializes_and_registers_aiocqhttp_parsers(tmp_path):
    from astrbot_plugin_parser_x.main import ParserXPlugin

    from core.text_renderer import TextCardRenderer

    class Context:
        def get_config(self):
            return {}

    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    config = AstrBotConfig(
        config_path=str(tmp_path / "parser_x_config.json"),
        schema=schema,
    )

    async def run_lifecycle():
        with (
            patch.object(StarTools, "get_data_dir", return_value=tmp_path / "data"),
            patch.object(TextCardRenderer, "check_available", return_value=False),
        ):
            plugin = ParserXPlugin(Context(), config)
            await plugin.initialize()
            try:
                assert "b23.tv" in plugin.parser_map
                assert "miyoushe.com" in plugin.parser_map
                assert "tieba.baidu.com" in plugin.parser_map
                assert "y.qq.com" in plugin.parser_map
                assert "tiktok.com" not in plugin.parser_map
                assert "youtube.com" not in plugin.parser_map
                assert plugin.key_pattern_list
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
