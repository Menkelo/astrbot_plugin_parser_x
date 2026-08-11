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
from core.parsers import (
    BaseParser,
    MiyousheParser,
    TiebaParser,
    XiaoheiheParser,
)
from core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge,
    BiliCommentCanvas,
    BiliCommentDocument,
    BiliCommentEntry,
    BiliRichPart,
)
from core.parsers.bilibili.comment_feed import BiliCommentFeed
from core.parsers.ytdlp import AcFunParser
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
    assert AcFunParser in classes
    assert MiyousheParser in classes
    assert TiebaParser in classes


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
    assert (
        not {
            "weixin_channel",
            "qq_music",
            "kugou_music",
            "qishui_music",
        }
        & platform_keys
    )
    assert "web_summary" not in schema


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


def test_bilibili_comment_renderer_prefers_astrbot_canvas(tmp_path):
    calls = {}

    async def fake_canvas(template, data, *, return_url, options):
        calls.update(
            {
                "template": template,
                "data": data,
                "return_url": return_url,
                "options": options,
            }
        )
        rendered = tmp_path / "canvas.jpg"
        rendered.write_bytes(b"canvas-image")
        return str(rendered)

    output = tmp_path / "comments.png"
    renderer = BiliCommentCanvas(canvas_render=fake_canvas)
    document = BiliCommentDocument(
        work_title="视频标题",
        cover="",
        total_text="1 条评论",
        entries=[
            BiliCommentEntry(
                author=BiliAuthorBadge(nickname="用户"),
                content=[BiliRichPart("text", text="评论内容")],
            )
        ],
    )
    asyncio.run(renderer.render(output, document))

    assert output.read_bytes() == b"canvas-image"
    assert calls["return_url"] is False
    assert calls["options"]["full_page"] is True
    assert "Parser X" in calls["template"]


def test_bilibili_comment_normalization_covers_rconsole_visible_fields(tmp_path):
    class FakeParser:
        headers = {}
        cache_dir = tmp_path
        client = None

        @staticmethod
        def norm_bili_img(url):
            return url

    feed = BiliCommentFeed(FakeParser(), BiliCommentCanvas())
    normalized = feed.adapt_comment(
        {
            "member": {
                "mid": "42",
                "uname": "UP主",
                "avatar": "https://i0.hdslb.com/avatar.jpg",
                "level_info": {"current_level": 6},
                "vip": {"nickname_color": "#fb7299"},
            },
            "content": {
                "message": "主评论[doge] @朋友 https://b23.tv/demo",
                "emote": {"[doge]": {"url": "https://i0.hdslb.com/doge.png"}},
                "at_name_to_mid": {"朋友": "7"},
                "jump_url": {"https://b23.tv/demo": {"title": "相关视频"}},
                "pictures": [{"img_src": "https://i0.hdslb.com/comment.jpg"}],
            },
            "like": 100000000,
            "rcount": 8,
            "ctime": 1700000000,
            "reply_control": {
                "location": "IP属地：上海",
                "is_up_top": True,
                "up_like": True,
            },
            "replies": [
                {
                    "member": {
                        "mid": "7",
                        "uname": "回复者",
                        "avatar": "",
                        "fans_detail": {
                            "medal_name": "应援团",
                            "level": 12,
                            "medal_color": 16737945,
                        },
                    },
                    "content": {"message": "楼中楼"},
                    "like": 2,
                }
            ],
        },
        owner_mid="42",
    )

    assert normalized is not None
    assert normalized.author.is_up is True
    assert normalized.author.level == 6
    assert normalized.author.nickname_color == "#fb7299"
    assert normalized.location == "上海"
    assert normalized.like_text == "1亿"
    assert normalized.reply_text == "回复 8"
    assert normalized.pinned is True
    assert normalized.up_liked is True
    assert {part.kind for part in normalized.content} >= {"emote", "highlight"}
    assert normalized.images == ["https://i0.hdslb.com/comment.jpg"]
    assert normalized.first_reply is not None
    assert normalized.first_reply.author.fan_medal is not None
    assert normalized.first_reply.author.fan_medal.name == "应援团"


def test_bilibili_comment_feed_prioritizes_hot_and_pinned_replies():
    raw = BiliCommentFeed._to_raw_feed(
        {
            "data": {
                "hots": [{"rpid": 1}],
                "top_replies": [{"rpid": 2}],
                "replies": [{"rpid": 1}, {"rpid": 3}],
                "upper": {"mid": 42},
                "cursor": {"all_count": 99},
            }
        }
    )

    assert [item["rpid"] for item in raw.items] == [1, 2, 3]
    assert raw.owner_mid == "42"
    assert raw.total == 99


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
                assert "miyoushe.com" in plugin.parser_map
                assert "tieba.baidu.com" in plugin.parser_map
                assert "y.qq.com" not in plugin.parser_map
                assert "kugou.com" not in plugin.parser_map
                assert "qishui.douyin.com" not in plugin.parser_map
                assert "channels.weixin.qq.com" not in plugin.parser_map
                assert "tiktok.com" not in plugin.parser_map
                assert "youtube.com" not in plugin.parser_map
                assert plugin.key_pattern_list
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
