from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from core.download import VideoInfo
from core.parsers import BaseParser
from core.parsers.ytdlp import TikTokParser, YouTubeParser
from core.utils import extract_json_url


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
    assert TikTokParser in classes
    assert YouTubeParser in classes


def test_ytdlp_patterns_keep_platform_boundaries():
    assert (
        TikTokParser.search_url("https://www.tiktok.com/@demo/video/1")[0]
        == "tiktok.com"
    )
    assert YouTubeParser.search_url("https://youtu.be/dQw4w9WgXcQ")[0] == "youtu.be"


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
        parser = YouTubeParser(
            {
                "cache_dir": str(tmp_path),
                "performance": {"source_max_size": 42},
            },
            downloader,
        )
        keyword, searched = parser.search_url("https://youtu.be/dQw4w9WgXcQ")
        result = await parser.parse(keyword, searched)
        path = await result.contents[0].get_path()
        return downloader, result, path

    downloader, result, path = asyncio.run(parse())
    assert result.platform.name == "youtube"
    assert result.extra["adapter"] == "yt-dlp"
    assert path.name == "demo.mp4"
    assert downloader.extract_args[2]["force_generic_extractor"] is False
    assert downloader.download_args[1]["max_size_mb"] == 42


def test_manifest_has_a_reviewable_upstream_baseline():
    manifest_path = Path(__file__).parents[1] / "upstream" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["repository"].endswith("rconsole-plugin.git")
    assert len(manifest["commit"]) == 40
    assert manifest["strategy"] == "semantic-port"


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
                assert "tiktok.com" in plugin.parser_map
                assert plugin.key_pattern_list
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
