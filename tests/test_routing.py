from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image as MessageImage
from astrbot.api.message_components import Node, Nodes, Plain, Reply
from astrbot.api.message_components import Video as MessageVideo
from astrbot.api.star import StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from PIL import Image, ImageDraw

from core.comment_canvas import (
    DOUYIN_THEME,
    CommentAuthor,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from core.comment_settings import CommentSettings
from core.constants import COMMENT_FOOTER_BRAND
from core.data import ImageContent, VideoContent
from core.download import Downloader, VideoInfo
from core.html_renderer import HtmlRenderService
from core.parsers import (
    BaseParser,
    BilibiliParser,
    DouyinParser,
    KuaiShouParser,
    MiyousheParser,
    WeiboParser,
    XiaoheiheParser,
    XiaoHongShuParser,
)
from core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge,
    BiliCommentCanvas,
    BiliCommentDocument,
    BiliCommentEntry,
    BiliRichPart,
)
from core.parsers.bilibili.comment_feed import BiliCommentFeed
from core.parsers.bilibili.dynamic_service import BiliDynamicService
from core.parsers.douyin.a_bogus import _sm3_fallback, generate_a_bogus
from core.parsers.douyin.comment_feed import DouyinCommentFeed
from core.parsers.miyoushe_comment import MiyousheCommentFeed
from core.parsers.weibo_comment import WeiboCommentFeed
from core.parsers.xiaoheihe_comment import XiaoheiheCommentFeed
from core.parsers.ytdlp import AcFunParser, NeteaseMusicParser
from core.platform_emotes import (
    build_bilibili_emote_map,
    build_miyoushe_emote_map,
    build_xiaoheihe_emote_map,
    load_platform_emotes,
)
from core.rendered_image import save_rendered_image
from core.text_renderer import TextCardRenderer
from core.utils import extract_json_url, generate_file_name


def _save_render_fixture(
    path: Path,
    *,
    size: tuple[int, int] = (1280, 220),
    background: str = "white",
) -> None:
    with Image.new("RGB", size, background) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((24, 24, min(size[0] - 24, 700), 150), fill="#303744")
        image.save(path)


def _contrast_with_white(color: str) -> float:
    values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in values
    ]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 1.05 / (luminance + 0.05)


def test_comment_settings_normalize_bool_and_clamp_values():
    settings = CommentSettings.from_config(
        {
            "comments": {
                "bilibili": "false",
                "display_count": "999",
                "timeout": "invalid",
            }
        },
        "bilibili",
    )
    assert settings.enabled is False
    assert settings.display_count == 20
    assert settings.timeout == 90

    legacy = CommentSettings.from_config(
        {"comments": "invalid", "bili_comment": "false"},
        "bilibili",
        legacy_enabled="false",
    )
    assert legacy.enabled is False


def test_comment_timeout_values_are_bounded_and_invalid_values_fall_back():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    assert ParserXPlugin._bounded_timeout("invalid", 15, 15) == 15
    assert ParserXPlugin._bounded_timeout(float("nan"), 15, 15) == 15
    assert ParserXPlugin._bounded_timeout(0, 15, 15) == 1
    assert ParserXPlugin._bounded_timeout(999, 90, 180) == 180


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


def test_tieba_is_fully_removed_from_routes_and_configuration():
    keywords = {
        keyword
        for parser in BaseParser.get_all_subclass()
        for keyword, _ in parser._key_patterns
    }
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )

    assert "tieba.baidu.com" not in keywords
    assert "tieba" not in schema["platforms"]["items"]
    assert "integrations" not in schema
    assert not (Path(__file__).parents[1] / "core" / "parsers" / "tieba.py").exists()


def test_config_schema_uses_latest_astrbot_panel_features():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert schema["rendering"]["obvious_hint"] is True
    assert schema["cookies"]["obvious_hint"] is True
    assert schema["performance"]["items"]["video_codec"]["labels"] == [
        "自动选择",
        "优先 HEVC / H.265",
        "优先 AVC / H.264",
    ]
    assert schema["cookies"]["items"]["ytdlp_cookie_file"]["type"] == "file"
    assert schema["cookies"]["items"]["ytdlp_cookie_file"]["file_types"] == ["txt"]
    assert schema["behavior"]["items"]["disabled_sessions"]["collapsed"] is True
    assert schema["comments"]["items"]["xiaoheihe"]["default"] is True
    assert schema["comments"]["items"]["miyoushe"]["default"] is True


def test_runtime_dependencies_do_not_require_local_browser():
    repo_root = Path(__file__).parents[1]
    requirements = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    assert "playwright" not in requirements.lower()
    source_files = [repo_root / "main.py", *(repo_root / "core").rglob("*.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    assert "playwright" not in source.lower()
    assert "chromium" not in source.lower()
    assert not (repo_root / "core" / "live_renderer.py").exists()
    assert "launch_persistent_context" not in source.lower()


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


def test_ytdlp_audio_parser_also_uses_shared_card(tmp_path):
    class FakeDownloader:
        async def ytdlp_extract_info(self, *_args, **_kwargs):
            return VideoInfo(
                title="示例歌曲",
                uploader="音乐人",
                duration=180,
                thumbnail="https://img.example.com/music.jpg",
                description="歌曲简介",
            )

        async def download_audio(self, *_args, **_kwargs):
            output = tmp_path / "music.mp3"
            output.write_bytes(b"audio")
            return output

    async def parse():
        parser = NeteaseMusicParser(
            {"cache_dir": str(tmp_path), "performance": {}},
            FakeDownloader(),
        )
        keyword, searched = parser.search_url("https://music.163.com/song?id=123456")
        result = await parser.parse(keyword, searched)
        await result.contents[0].get_path()
        return result

    result = asyncio.run(parse())

    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_media"].endswith("music.jpg")
    assert result.extra["card_kind"] == "音频"
    assert result.extra["card_info"] == ["音频文件独立发送"]


def test_domestic_parser_helpers_cover_new_routes():
    assert (
        MiyousheParser.extract_post_id("https://www.miyoushe.com/ys/article/69857339")
        == "69857339"
    )
    assert XiaoheiheParser.extract_identity(
        "https://www.xiaoheihe.cn/app/game/pc/730"
    ) == ("pc", "730")
    assert XiaoheiheParser.extract_identity(
        "https://www.xiaoheihe.cn/games/detail/730"
    ) == ("pc", "730")
    assert XiaoheiheParser.extract_identity(
        "https://www.xiaoheihe.cn/community/42/list/123456789"
    ) == ("bbs", "123456789")
    share_url = "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123456789"
    assert XiaoheiheParser.extract_identity(share_url) == ("bbs", "123456789")
    keyword, searched = XiaoheiheParser.search_url(share_url)
    assert keyword == "xiaoheihe.cn"
    assert searched.group(0) == share_url
    for subdomain in ("www", "share", "bbs"):
        route_url = f"https://{subdomain}.xiaoheihe.cn/app/bbs/link/abc123"
        keyword, searched = XiaoheiheParser.search_url(route_url)
        assert keyword == "xiaoheihe.cn"
        assert XiaoheiheParser.extract_identity(searched.group(0)) == (
            "bbs",
            "abc123",
        )
    assert XiaoheiheParser._parse_redirect_metadata(
        "https://www.xiaoheihe.cn/app/bbs/link/123?"
        "redirect_data=%7B%22link%22%3A%7B%22title%22%3A%22Demo%22%2C"
        "%22description%22%3A%22Body%22%7D%7D"
    ) == {"title": "Demo", "description": "Body"}
    assert (
        XiaoheiheParser.build_hkey(
            "bbs/app/link/tree",
            1700000001,
            "ABCDEF0123456789ABCDEF0123456789",
        )
        == "V2V1Z67"
    )
    assert WeiboParser._mid_to_bid("4461526582968019") == "IpOAqcs7h"
    keyword, searched = WeiboParser.search_url(
        "https://weibo.com/tv/show/1034:4461526582968019?mid=4461526582968019"
    )
    assert keyword == "weibo.com/tv/show"
    assert searched.group(1) == "4461526582968019"
    keyword, searched = WeiboParser.search_url("https://m.weibo.cn/detail/IpOAqcs7h")
    assert keyword == "weibo.cn"
    assert searched.group(1) == "IpOAqcs7h"
    assert (
        WeiboParser._extract_body_text(
            {
                "text": "截断正文",
                "longText": {"longTextContent": "<p>完整正文</p>"},
            }
        )
        == "完整正文"
    )
    assert WeiboParser._extract_body_text({"text_raw": "备用正文"}) == "备用正文"
    assert (
        WeiboParser._extract_body_text({"longText": "<p>第一段</p><p>第二段</p>"})
        == "第一段\n第二段"
    )
    assert (
        WeiboParser._delivery_summary(
            {"status_title": "完整正文..."}, "完整正文还有后续"
        )
        == "识别：微博\n完整正文还有后续"
    )
    assert WeiboParser._extract_detail_status(
        '<script>$render_data = [{"status":{"id":"1",'
        '"text":"<p>详情页正文</p>"}}][0]</script>'
    ) == {"id": "1", "text": "<p>详情页正文</p>"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1_700_000_000, 1_700_000_000),
        (1_700_000_000_000, 1_700_000_000),
        ("1700000000000", 1_700_000_000),
        ("invalid", None),
        (0, None),
    ],
)
def test_xiaohongshu_timestamp_accepts_seconds_and_milliseconds(raw, expected):
    assert XiaoHongShuParser._normalize_timestamp(raw) == expected


def test_xiaohongshu_removes_internal_topic_markers_only_inside_hashtags():
    assert (
        XiaoHongShuParser._normalize_note_text(
            "#性能车[话题]# #机车跑山[话题]# 正文[话题]"
        )
        == "#性能车# #机车跑山# 正文[话题]"
    )


def test_image_post_parsers_use_shared_cards_and_keep_canonical_urls(tmp_path):
    from core.parsers.kuaishou import CdnUrl, Photo

    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "image.jpg"

            return asyncio.create_task(done())

    async def run():
        downloader = FakeDownloader()
        kuaishou = KuaiShouParser({"cache_dir": str(tmp_path)}, downloader)
        kuaishou_result = kuaishou._build_result_from_photo(
            Photo(
                caption="快手图文",
                timestamp=1_700_000_000_000,
                user_name="作者",
                single_picture=True,
                cover_urls=[
                    CdnUrl(cdn="img.example.com", url="https://img.example.com/a.jpg")
                ],
            ),
            "https://www.kuaishou.com/short-video/demo",
        )

        douyin = DouyinParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"douyin": False},
            },
            downloader,
        )
        douyin_result = douyin._build_result_from_aweme(
            {
                "aweme_id": "123",
                "desc": "抖音图文",
                "create_time": 1_700_000_000,
                "author": {"nickname": "作者"},
                "images": [{"url_list": ["https://img.example.com/b.jpg"]}],
            },
            "123",
        )

        xhs = XiaoHongShuParser({"cache_dir": str(tmp_path)}, downloader)
        xhs_result = xhs._process_explore_data(
            {
                "type": "normal",
                "title": "小红书标题",
                "desc": "小红书正文 #性能车[话题]#",
                "time": 1_700_000_000_000,
                "user": {
                    "nickname": "作者",
                    "avatar": "https://img.example.com/avatar.jpg",
                },
                "imageList": [{"urlDefault": "https://img.example.com/c.jpg"}],
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        results = (kuaishou_result, douyin_result, xhs_result)
        await asyncio.gather(
            *(content.get_path() for result in results for content in result.contents)
        )
        await asyncio.gather(
            douyin.close_session(),
            kuaishou.close_session(),
            xhs.close_session(),
        )
        return results

    kuaishou_result, douyin_result, xhs_result = asyncio.run(run())
    assert kuaishou_result.url == "https://www.kuaishou.com/short-video/demo"
    assert douyin_result.url == "https://www.douyin.com/video/123"
    assert xhs_result.url == "https://www.xiaohongshu.com/explore/demo"
    assert xhs_result.timestamp == 1_700_000_000
    assert xhs_result.text == "小红书正文 #性能车#"
    for result in (kuaishou_result, douyin_result, xhs_result):
        assert result.contents
        assert result.extra["render_text_card"] is True
        assert result.extra["text_card_media"]


def test_video_post_parsers_and_ytdlp_use_shared_cards(tmp_path):
    from core.parsers.kuaishou import CdnUrl, Photo

    class FakeDownloader:
        def download_video(self, *_args, **_kwargs):
            async def done():
                output = tmp_path / "video.mp4"
                output.write_bytes(b"video")
                return output

            return asyncio.create_task(done())

        async def ytdlp_extract_info(self, *_args, **_kwargs):
            return VideoInfo(
                title="兼容层视频",
                uploader="作者",
                duration=12,
                thumbnail="https://img.example.com/ytdlp.jpg",
                description="视频正文",
            )

    async def run():
        downloader = FakeDownloader()
        kuaishou = KuaiShouParser({"cache_dir": str(tmp_path)}, downloader)
        kuaishou_result = kuaishou._build_result_from_photo(
            Photo(
                caption="快手视频",
                timestamp=1_700_000_000_000,
                user_name="作者",
                cover_urls=[
                    CdnUrl(cdn="img.example.com", url="https://img.example.com/k.jpg")
                ],
                main_mv_urls=[
                    CdnUrl(
                        cdn="video.example.com",
                        url="https://video.example.com/k.mp4",
                    )
                ],
            ),
            "https://www.kuaishou.com/short-video/demo",
        )

        douyin = DouyinParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"douyin": False},
            },
            downloader,
        )
        douyin_result = douyin._build_result_from_aweme(
            {
                "aweme_id": "123",
                "desc": "抖音视频",
                "create_time": 1_700_000_000,
                "author": {"nickname": "作者"},
                "video": {
                    "play_addr": {"url_list": ["https://video.example.com/d.mp4"]},
                    "cover": {"url_list": ["https://img.example.com/d.jpg"]},
                    "duration": 12,
                },
            },
            "123",
        )

        xhs = XiaoHongShuParser({"cache_dir": str(tmp_path)}, downloader)
        xhs_result = xhs._process_explore_data(
            {
                "type": "video",
                "title": "小红书视频",
                "desc": "小红书正文",
                "time": 1_700_000_000_000,
                "user": {
                    "nickname": "作者",
                    "avatar": "https://img.example.com/avatar.jpg",
                },
                "imageList": [{"urlDefault": "https://img.example.com/x.jpg"}],
                "video": {
                    "media": {
                        "stream": {"h264": [{"url": "https://video.example.com/x.mp4"}]}
                    }
                },
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        acfun = AcFunParser({"cache_dir": str(tmp_path)}, downloader)
        keyword, searched = acfun.search_url("https://www.acfun.cn/v/ac123")
        ytdlp_result = await acfun.parse(keyword, searched)

        results = (kuaishou_result, douyin_result, xhs_result, ytdlp_result)
        await asyncio.gather(
            *(content.get_path() for result in results for content in result.contents)
        )
        await asyncio.gather(
            douyin.close_session(),
            kuaishou.close_session(),
            xhs.close_session(),
            acfun.close_session(),
        )
        return results

    results = asyncio.run(run())
    for result in results:
        assert result.extra["render_text_card"] is True
        assert result.extra["video_separate_from_card"] is True
        assert "视频" in result.extra["card_kind"]


def test_xiaoheihe_api_share_route_uses_redirect_metadata(tmp_path):
    share_url = "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123456789"

    class FakeResponse:
        status_code = 200
        url = (
            "https://www.xiaoheihe.cn/app/bbs/link/123456789?"
            "redirect_data=%7B%22link%22%3A%7B%22title%22%3A%22Demo%22%2C"
            "%22description%22%3A%22Body%22%7D%7D"
        )
        text = "<html></html>"

    async def run():
        parser = XiaoheiheParser(
            {"cache_dir": str(tmp_path), "cookies": {}},
            object(),
        )

        async def fake_http_get(url, **_kwargs):
            if url.endswith("/bbs/app/link/tree"):
                raise RuntimeError("signed api unavailable")
            assert url == share_url
            return FakeResponse()

        parser.http_get = fake_http_get
        keyword, searched = parser.search_url(share_url)
        return await parser.parse(keyword, searched)

    result = asyncio.run(run())
    assert result.platform.name == "xiaoheihe"
    assert result.title == "Demo"
    assert result.text == "Body"
    assert result.url == share_url


def test_xiaoheihe_rich_text_extracts_html_body_and_inline_images():
    text, images = XiaoheiheParser.extract_rich_content(
        json.dumps(
            [
                {
                    "type": "html",
                    "text": (
                        '<p>第一段 <a href="https://example.com">链接</a></p>'
                        '<p>第二段</p><img src="https://img.example.com/demo.jpg">'
                    ),
                }
            ]
        )
    )

    assert text == "第一段 链接 (https://example.com)\n第二段"
    assert images == ["https://img.example.com/demo.jpg"]
    assert XiaoheiheParser.extract_rich_blocks(
        '<p>前文</p><img src="https://img.example.com/a.jpg"><p>后文</p>'
    ) == [
        ("text", "前文"),
        ("image", "https://img.example.com/a.jpg"),
        ("text", "后文"),
    ]


def test_xiaoheihe_rich_text_ignores_android_cache_paths():
    local_path = (
        "/storage/emulated/0/Android/data/com.max.xiaoheihe/cache/"
        "optimizer_output/298def8347f203c8599056f548bdd91116acba1a27a72b2313cc434ae9079599"
        "@1280.0x1280.0.jpeg"
    )
    blocks = XiaoheiheParser.extract_rich_blocks(
        [
            {
                "type": "text",
                "text": "小黑盒学的，便宜了好多[cube_开心][cube_开心]",
                "optimizer_output": local_path,
            },
            {"content": f"补充正文\n{local_path}", "local_path": local_path},
            {"html": f"<p>HTML 正文</p><p>{local_path}</p>"},
        ]
    )

    assert blocks == [
        (
            "text",
            ("小黑盒学的，便宜了好多[cube_开心][cube_开心]\n\n补充正文\n\nHTML 正文"),
        )
    ]
    assert local_path not in repr(blocks)


def test_platform_emotes_render_in_cards_and_comment_rich_text():
    bilibili_map = build_bilibili_emote_map(
        {
            "data": {
                "packages": [
                    {
                        "emote": [
                            {
                                "text": "[doge]",
                                "url": "https://i0.hdslb.com/bfs/emote/doge.png",
                            }
                        ]
                    }
                ]
            }
        }
    )
    xiaoheihe_map = build_xiaoheihe_emote_map(
        {
            "result": {
                "emoji_groups": [
                    {
                        "group_code": "cube",
                        "emojis": [
                            {
                                "code": "开心",
                                "img": "https://imgheybox.max-c.com/heybox/emoji/cube_21.png",
                            }
                        ],
                    }
                ]
            }
        }
    )
    miyoushe_map = build_miyoushe_emote_map(
        {
            "data": {
                "list": [
                    {
                        "list": [
                            {
                                "name": "米游姬-期待",
                                "icon": "https://img.example.com/miyoushe.png",
                            }
                        ]
                    }
                ]
            }
        }
    )

    html = TextCardRenderer(HtmlRenderService()).build_html(
        platform_key="xiaoheihe",
        platform_name="小黑盒",
        author_name="作者",
        title="标题",
        text="便宜了好多[cube_开心]",
        emotes=xiaoheihe_map,
    )
    assert 'class="inline-emote"' in html
    assert "cube_21.png" in html

    bilibili_html = TextCardRenderer(HtmlRenderService()).build_html(
        platform_key="bilibili",
        platform_name="B站动态",
        author_name="UP主",
        title="标题[doge]",
        text="正文[doge]",
        emotes=bilibili_map,
    )
    assert bilibili_html.count('class="inline-emote"') == 2
    assert "bfs/emote/doge.png" in bilibili_html

    xiaoheihe_parts = XiaoheiheCommentFeed._rich_text(
        "评论[cube_开心]",
        xiaoheihe_map,
    )
    assert any(part.kind == "emote" and part.url for part in xiaoheihe_parts)

    miyoushe_parts = MiyousheCommentFeed._rich_text(
        {
            "content": "正文_(米游姬 期待)",
            "struct_content": json.dumps(
                [
                    {"insert": "正文_(米游姬 期待)"},
                    {
                        "insert": {
                            "backup_text": "[自定义表情]",
                            "custom_emoticon": {
                                "url": "https://img.example.com/custom.gif"
                            },
                        }
                    },
                ],
                ensure_ascii=False,
            ),
        },
        miyoushe_map,
    )
    emotes = [part for part in miyoushe_parts if part.kind == "emote"]
    assert [part.url for part in emotes] == ["https://img.example.com/miyoushe.png"]
    assert (
        MiyousheCommentFeed._custom_sticker(
            {
                "struct_content": json.dumps(
                    [
                        {
                            "insert": {
                                "backup_text": "[自定义表情]",
                                "custom_emoticon": {
                                    "url": "https://img.example.com/custom.gif"
                                },
                            }
                        }
                    ]
                )
            }
        )
        == "https://img.example.com/custom.gif"
    )


def test_bilibili_emote_loader_merges_public_and_owned_packages():
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeParser:
        headers = {"Referer": "https://www.bilibili.com/"}
        bili_ck = "SESSDATA=test"

        async def http_get(self, url, *, params, **_kwargs):
            calls.append((url, params))
            if "panel/web" in url:
                return FakeResponse({"data": {"packages": [{"id": 2, "emote": []}]}})
            token = "[doge]" if params["ids"] == "1" else "[已购表情]"
            return FakeResponse(
                {
                    "data": {
                        "packages": [
                            {
                                "emote": [
                                    {
                                        "text": token,
                                        "url": f"https://i0.hdslb.com/{params['ids']}.png",
                                    }
                                ]
                            }
                        ]
                    }
                }
            )

    catalog = asyncio.run(load_platform_emotes(FakeParser(), "bilibili"))

    assert catalog["[doge]"].endswith("/1.png")
    assert catalog["[已购表情]"].endswith("/2.png")
    assert [params.get("ids") for _url, params in calls] == ["1", None, "2"]


def test_xiaoheihe_app_link_retries_official_share_endpoint(tmp_path):
    app_url = "https://www.xiaoheihe.cn/app/bbs/link/123456789"
    share_url = XiaoheiheParser._canonical_share_url("bbs", "123456789")
    calls = []

    class FakeResponse:
        def __init__(self, status_code, url, text=""):
            self.status_code = status_code
            self.url = url
            self.text = text

    async def run():
        parser = XiaoheiheParser(
            {"cache_dir": str(tmp_path), "cookies": {}},
            object(),
        )

        async def fake_http_get(url, **_kwargs):
            calls.append(url)
            if url.endswith("/bbs/app/link/tree"):
                raise RuntimeError("signed api unavailable")
            if url == app_url:
                return FakeResponse(404, url)
            return FakeResponse(
                200,
                share_url
                + "&redirect_data=%7B%22link%22%3A%7B%22title%22%3A%22Demo%22%2C"
                "%22description%22%3A%22Body%22%7D%7D",
            )

        parser.http_get = fake_http_get
        keyword, searched = parser.search_url(app_url)
        return await parser.parse(keyword, searched)

    result = asyncio.run(run())
    assert calls == [
        "https://api.xiaoheihe.cn/bbs/app/link/tree",
        share_url,
    ]
    assert result.title == "Demo"
    assert result.text == "Body"
    assert result.delivery is not None


def test_xiaoheihe_uses_embedded_redirect_metadata_even_when_page_is_gone(tmp_path):
    url = (
        "https://www.xiaoheihe.cn/app/bbs/link/123?"
        "redirect_data=%7B%22link%22%3A%7B%22title%22%3A%22Demo%22%2C"
        "%22description%22%3A%22Body%22%7D%7D"
    )

    class FakeResponse:
        status_code = 404
        text = ""

        def __init__(self, response_url):
            self.url = response_url

    async def run():
        parser = XiaoheiheParser(
            {"cache_dir": str(tmp_path), "cookies": {}},
            object(),
        )

        async def fake_http_get(request_url, **_kwargs):
            if request_url.endswith("/bbs/app/link/tree"):
                raise RuntimeError("signed api unavailable")
            return FakeResponse(request_url)

        parser.http_get = fake_http_get
        keyword, searched = parser.search_url(url)
        return await parser.parse(keyword, searched)

    result = asyncio.run(run())
    assert result.title == "Demo"
    assert result.text == "Body"
    assert result.delivery is not None


def test_xiaoheihe_api_preserves_rich_body_order_and_native_delivery(tmp_path):
    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "image.jpg"

            return asyncio.create_task(done())

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "ok",
                "result": {
                    "link": {
                        "title": "帖子标题",
                        "description": "帖子简介",
                        "text": json.dumps(
                            [
                                {
                                    "type": "html",
                                    "text": (
                                        "<p>正文内容[普通括号][cube_开心]</p>"
                                        '<img src="https://img.example.com/body.jpg">'
                                    ),
                                }
                            ]
                        ),
                        "thumb": "https://img.example.com/cover.jpg",
                        "user": {
                            "username": "作者",
                            "avatar_url": "https://img.example.com/avatar.jpg",
                        },
                    },
                    "comments": [
                        {
                            "comment": [
                                {
                                    "floor_num": 1,
                                    "text": "评论内容",
                                    "user": {"username": "评论用户"},
                                }
                            ]
                        }
                    ],
                },
            }

    async def run():
        parser = XiaoheiheParser(
            {"cache_dir": str(tmp_path), "cookies": {}, "performance": {}},
            FakeDownloader(),
        )

        async def fake_http_get(_url, **_kwargs):
            return FakeResponse()

        parser.http_get = fake_http_get
        return await parser._parse_api(
            "https://www.xiaoheihe.cn/app/bbs/link/1", "bbs", "1"
        )

    result = asyncio.run(run())
    assert result.text == "帖子简介\n\n正文内容[普通括号][cube_开心]"
    assert len(result.contents) == 2
    assert result.author is not None and result.author.name == "作者"
    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_text"] == "帖子简介\n\n正文内容[普通括号][cube_开心]"
    assert result.extra["card_emotes"]["[cube_开心]"].endswith("cube_21.png")
    assert result.delivery is not None
    assert len(result.delivery.batches) == 2
    overview, body = result.delivery.batches
    assert overview.mode == "direct"
    assert isinstance(overview.parts[0], ImageContent)
    assert "帖子标题" in overview.parts[1]
    assert body.mode == "forward"
    assert body.parts[0] == "正文内容[普通括号]"
    assert isinstance(body.parts[1], ImageContent)
    assert isinstance(body.parts[2], ImageContent)
    assert "comment_document_task_factory" not in result.extra
    assert callable(result.extra["comment_image_task_factory"])


def test_xiaoheihe_game_uses_signed_api_without_cookie(tmp_path):
    calls = []

    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "game.jpg"

            return asyncio.create_task(done())

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "ok",
                "result": {
                    "name": "CS2",
                    "name_en": "Counter-Strike 2",
                    "about_the_game": "<p>游戏简介</p>",
                    "score": 9.1,
                    "release_date": "2023-09-28",
                    "developers": [{"value": "Valve"}],
                    "publishers": [{"value": "Valve"}],
                    "image": "https://img.example.com/game.jpg",
                    "screenshots": [],
                },
            }

    async def run():
        parser = XiaoheiheParser(
            {"cache_dir": str(tmp_path), "cookies": {}, "performance": {}},
            FakeDownloader(),
        )

        async def fake_http_get(url, **kwargs):
            calls.append((url, kwargs.get("params")))
            return FakeResponse()

        parser.http_get = fake_http_get
        keyword, searched = parser.search_url(
            "https://www.xiaoheihe.cn/games/detail/730"
        )
        return await parser.parse(keyword, searched)

    result = asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][0] == "https://api.xiaoheihe.cn/game/get_game_detail"
    assert calls[0][1]["steam_appid"] == "730"
    assert calls[0][1]["hkey"]
    assert result.title == "CS2"
    assert "游戏简介" in (result.text or "")
    assert "Valve" in (result.text or "")
    assert len(result.contents) == 1
    assert result.delivery is not None
    assert result.extra["render_text_card"] is True
    assert "游戏简介" in result.extra["text_card_text"]


def test_miyoushe_uses_native_delivery_and_enables_comments(tmp_path):
    calls = []

    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "miyoushe.jpg"

            return asyncio.create_task(done())

        def download_video(self, _url, **_kwargs):
            async def done():
                return tmp_path / "miyoushe.mp4"

            return asyncio.create_task(done())

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "retcode": 0,
                "data": {
                    "post": {
                        "post": {
                            "post_id": "75247726",
                            "uid": "42",
                            "subject": "文章标题",
                            "content": "<p>第一段</p><p>第二段</p>",
                            "cover": "https://img.example.com/cover.jpg",
                            "images": ["https://img.example.com/body.jpg"],
                            "created_at": 1_700_000_000,
                        },
                        "user": {
                            "uid": "42",
                            "nickname": "作者",
                            "avatar_url": "https://img.example.com/avatar.jpg",
                        },
                        "vod_list": [
                            {
                                "duration": 12,
                                "resolutions": [
                                    {
                                        "width": 640,
                                        "height": 360,
                                        "url": "https://video.example.com/demo.mp4",
                                    }
                                ],
                            }
                        ],
                    }
                },
            }

    async def run():
        parser = MiyousheParser(
            {
                "cache_dir": str(tmp_path),
                "comments": {"miyoushe": True},
                "performance": {},
            },
            FakeDownloader(),
        )

        async def fake_http_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        parser.http_get = fake_http_get
        keyword, searched = parser.search_url(
            "https://www.miyoushe.com/ys/article/75247726"
        )
        return await parser.parse(keyword, searched)

    result = asyncio.run(run())
    assert calls[0][0] == MiyousheParser.api_url
    assert calls[0][1]["params"] == {"post_id": "75247726"}
    assert calls[0][1]["headers"]["DS"]
    assert result.text == "第一段\n第二段"
    assert len(result.contents) == 3
    assert result.delivery is not None
    assert len(result.delivery.batches) == 3
    overview, images, video = result.delivery.batches
    assert isinstance(overview.parts[0], ImageContent)
    assert "文章标题" in overview.parts[1]
    assert images.reply_original is True
    assert isinstance(images.parts[0], ImageContent)
    assert isinstance(video.parts[0], VideoContent)
    assert "comment_document_task_factory" not in result.extra
    assert callable(result.extra["comment_image_task_factory"])
    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_avatar"].endswith("avatar.jpg")


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
        _save_render_fixture(rendered)
        return str(rendered)

    output = tmp_path / "comments.png"
    renderer = BiliCommentCanvas(HtmlRenderService(fake_canvas))
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

    with Image.open(output) as image:
        assert image.width == 1140
        assert 170 <= image.height <= 173
    assert calls["return_url"] is False
    assert calls["options"]["full_page"] is True
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" in calls["template"]
    assert "#parser-x-comment-root{transform:scale(1.5)" in calls["template"]
    assert "scrollbar-width:none" in calls["template"]
    assert COMMENT_FOOTER_BRAND in calls["template"]


def test_social_comment_renderer_scales_astrbot_canvas(tmp_path):
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
        rendered = tmp_path / "social-canvas.jpg"
        _save_render_fixture(rendered, background="#161823")
        return str(rendered)

    output = tmp_path / "social-comments.jpg"
    renderer = SocialCommentCanvas(HtmlRenderService(fake_canvas))
    document = CommentDocument(
        theme=DOUYIN_THEME,
        work_title="作品",
        cover="",
        total_text="1 条评论",
        entries=[
            CommentEntry(
                author=CommentAuthor("用户"),
                content=[CommentRichPart("text", "评论内容")],
            )
        ],
    )
    asyncio.run(renderer.render(output, document))

    with Image.open(output) as image:
        assert image.width == 1140
        assert 170 <= image.height <= 173
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" in calls["template"]
    assert "#parser-x-comment-root{transform:scale(1.5)" in calls["template"]
    assert "scrollbar-width:none" in calls["template"]


def test_html_render_service_requires_official_renderer(tmp_path):
    renderer = HtmlRenderService()
    with pytest.raises(RuntimeError, match="html_render"):
        asyncio.run(renderer.render(tmp_path / "missing.png", "<p>test</p>"))


def test_rendered_image_trims_unused_right_canvas(tmp_path):
    rendered = tmp_path / "rendered.jpg"
    output = tmp_path / "cropped.jpg"
    Image.new("RGB", (1280, 123), "white").save(rendered)

    save_rendered_image(rendered, output, target_width=1140)

    with Image.open(output) as image:
        assert image.size == (1140, 123)


def test_rendered_image_uses_base_width_for_unscaled_canvas(tmp_path):
    rendered = tmp_path / "rendered.jpg"
    output = tmp_path / "cropped.jpg"
    Image.new("RGB", (800, 123), "white").save(rendered)

    save_rendered_image(
        rendered,
        output,
        target_width=1140,
        fallback_width=760,
    )

    with Image.open(output) as image:
        assert image.size == (760, 123)


@pytest.mark.parametrize("background", ["#f3f5f8", "#161823"])
def test_rendered_image_trims_bottom_canvas_for_light_and_dark_pages(
    tmp_path,
    background,
):
    rendered = tmp_path / "rendered.png"
    output = tmp_path / "cropped.png"
    with Image.new("RGB", (760, 720), background) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 52, 718, 335), fill="#ffffff")
        image.save(rendered)

    save_rendered_image(
        rendered,
        output,
        target_width=760,
        bottom_padding=26,
    )

    with Image.open(output) as image:
        assert image.size == (760, 362)


def test_rendered_image_keeps_content_that_already_reaches_bottom(tmp_path):
    rendered = tmp_path / "rendered.png"
    output = tmp_path / "cropped.png"
    with Image.new("RGB", (760, 240), "#161823") as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 24, 729, 232), fill="#252632")
        image.save(rendered)

    save_rendered_image(
        rendered,
        output,
        target_width=760,
        bottom_padding=20,
    )

    with Image.open(output) as image:
        assert image.size == (760, 240)


def test_text_card_uses_html_render_service(tmp_path):
    calls = []

    async def fake_html_render(template, data, *, return_url, options):
        calls.append((template, data, return_url, options))
        rendered = tmp_path / f"rendered-{len(calls)}.png"
        _save_render_fixture(rendered)
        return str(rendered)

    service = HtmlRenderService(fake_html_render)
    text_output = tmp_path / "text.png"

    async def run():
        await TextCardRenderer(service).render_text_card(
            text_output,
            platform_name="微博",
            author_name="用户",
            text="正文 #话题#",
        )

    asyncio.run(run())
    with Image.open(text_output) as image:
        assert image.size == (760, 171)
    assert len(calls) == 1
    assert all(return_url is False for _, _, return_url, _ in calls)
    assert all(options["scale"] == "css" for _, _, _, options in calls)
    assert all(options["timeout"] == 45_000 for _, _, _, options in calls)
    assert all(COMMENT_FOOTER_BRAND in template for template, *_ in calls)
    assert 'data-card-style="minimal-feed"' in calls[0][0]
    assert "#ff8200" in calls[0][0]
    assert "platform-mark" not in calls[0][0]
    assert "backdrop-filter" not in calls[0][0]
    assert "radial-gradient" not in calls[0][0]


def test_media_only_card_does_not_use_share_url_as_body(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_text_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 320))

    renderer = FakeTextRenderer()
    plugin.text_renderer = renderer
    source = tmp_path / "source.jpg"
    result = ParseResult(
        platform=Platform(name="xiaohongshu", display_name="小红书"),
        contents=[PluginImageContent(source)],
        url="https://www.xiaohongshu.com/explore/demo?xsec_token=secret",
        extra={
            "render_text_card": True,
            "text_card_media": "https://img.example.com/note.jpg",
            "card_kind": "笔记 · 图文",
        },
    )

    card = asyncio.run(plugin._build_text_card_content(result))

    assert card is not None
    assert renderer.kwargs["title"] is None
    assert renderer.kwargs["text"] == ""
    assert renderer.kwargs["media_fit"] == "contain"
    assert "xiaohongshu.com" not in repr(renderer.kwargs)


@pytest.mark.parametrize(
    ("key", "name", "accent"),
    [
        ("bilibili", "B站动态", "#fb7299"),
        ("douyin", "抖音", "#fe2c55"),
        ("kuaishou", "快手", "#ff4906"),
        ("miyoushe", "米游社", "#00b8e6"),
        ("xiaoheihe", "小黑盒", "#ff6a00"),
        ("xiaohongshu", "小红书", "#ff2442"),
        ("weibo", "微博", "#ff8200"),
        ("acfun", "AcFun", "#fd4c5b"),
        ("xigua", "西瓜视频", "#ff4a45"),
        ("pipixia", "皮皮虾", "#ff5a36"),
        ("weishi", "微视", "#ff5a65"),
    ],
)
def test_unified_content_card_keeps_only_platform_brand_identity(
    key,
    name,
    accent,
):
    html = TextCardRenderer(HtmlRenderService()).build_html(
        platform_key=key,
        platform_name=name,
        author_name="作者",
        title="标题",
        text="正文",
    )

    assert 'data-card-style="minimal-feed"' in html
    assert "platform-mark" not in html
    assert accent in html
    assert "border-radius:14px" in html
    assert "backdrop-filter" not in html
    assert ".brand-bar::before" not in html
    assert "radial-gradient" not in html


def test_unified_content_card_accepts_media_accent_without_monogram():
    html = TextCardRenderer(HtmlRenderService()).build_html(
        platform_key="weibo",
        platform_name="微博",
        author_name="作者",
        title="标题",
        text="正文",
        accent_color="#184f7a",
        accent_soft="#e8f1f7",
        accent_source="media",
    )

    assert 'data-accent-source="media"' in html
    assert "#184f7a" in html
    assert "backdrop-filter" not in html
    assert "platform-mark" not in html
    assert ">微<" not in html


def test_card_palette_is_fixed_to_platform_identity(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    media_path = tmp_path / "dominant.png"
    invalid_path = tmp_path / "invalid.jpg"
    with Image.new("RGB", (120, 80), "#ef5364") as image:
        image.save(media_path)
    invalid_path.write_bytes(b"not-an-image")

    plugin = object.__new__(ParserXPlugin)
    media_result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        contents=[PluginImageContent(media_path)],
    )
    invalid_result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        contents=[PluginImageContent(invalid_path)],
    )

    media_palette = asyncio.run(plugin._resolve_card_palette(media_result))
    fallback_palette = asyncio.run(plugin._resolve_card_palette(invalid_result))

    assert media_palette == ("#ff8200", "#fff7e7", "platform")
    assert fallback_palette == media_palette


def test_unified_long_card_orders_body_media_metrics_and_comments():
    reply = CommentEntry(
        author=CommentAuthor(nickname="回复者"),
        content=[CommentRichPart(kind="text", text="楼中楼回复")],
        reply_text="回复",
    )
    document = CommentDocument(
        theme=DOUYIN_THEME,
        work_title="作品",
        cover="",
        total_text="128 条评论",
        entries=[
            CommentEntry(
                author=CommentAuthor(nickname="评论者"),
                content=[CommentRichPart(kind="text", text="热门评论正文")],
                like_text="88",
                reply_text="3",
                first_reply=reply,
            )
        ],
    )

    html = TextCardRenderer(HtmlRenderService()).build_html(
        platform_key="douyin",
        platform_name="抖音",
        author_name="作者",
        author_avatar="https://img.example.com/avatar.jpg",
        author_badge="作者",
        title="统一长卡标题",
        timestamp_text="2026-08-15 12:00:00",
        content_kind="图文作品",
        text="完整正文第一段\n完整正文第二段",
        media_url="https://img.example.com/cover.jpg",
        metrics=[("评论", 128), ("点赞", 12500)],
        info_items=["正文完整保留", "图片 3 张"],
        comment_document=document,
    )

    ordered_sections = [
        'class="brand-bar"',
        'class="hero"',
        'class="primary-block"',
        'class="profile-block"',
        'class="copy-block"',
        'class="info-block"',
        'class="comments-block"',
        'class="footer"',
    ]
    positions = [html.index(section) for section in ordered_sections]
    assert positions == sorted(positions)
    assert "完整正文第一段" in html
    assert "热门评论正文" in html
    assert "楼中楼回复" in html
    assert "1.2万" in html
    assert "展示 1 / 128 条评论" in html
    assert COMMENT_FOOTER_BRAND in html
    assert "stroke-dasharray" not in html
    assert 'd="M21 15a4 4 0 0 1-4 4H8l-5 3V7' in html
    assert "html,body{margin:0;width:760px" in html
    assert "scrollbar-width:none" in html
    assert ".card{width:716px" in html


def test_bilibili_video_builds_unified_card_metadata_and_comment_factories(tmp_path):
    class FakeDownloader:
        async def streamd(self, _url, *, file_name, **_kwargs):
            output = tmp_path / file_name
            output.write_bytes(b"video")
            return output

    raw_info = {
        "bvid": "BV1xx411c7mD",
        "aid": 170001,
        "videos": 2,
        "tid": 17,
        "tname": "单机游戏",
        "copyright": 1,
        "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
        "title": "测试视频",
        "pubdate": 1_700_000_000,
        "ctime": 1_700_000_000,
        "desc": "完整简介",
        "state": 0,
        "duration": 125,
        "owner": {
            "mid": 42,
            "name": "UP主",
            "face": "//i0.hdslb.com/bfs/face/avatar.jpg",
        },
        "pages": [
            {
                "cid": 100,
                "page": 1,
                "from": "vupload",
                "part": "第一集",
                "duration": 125,
                "vid": "",
                "weblink": "",
                "dimension": {"width": 1920, "height": 1080, "rotate": 0},
            },
            {
                "cid": 101,
                "page": 2,
                "from": "vupload",
                "part": "第二集",
                "duration": 130,
                "vid": "",
                "weblink": "",
                "dimension": {"width": 1920, "height": 1080, "rotate": 0},
            },
        ],
        "stat": {
            "view": 123456,
            "reply": 88,
            "like": 999,
            "favorite": 77,
            "coin": 66,
            "share": 55,
        },
    }

    async def run():
        parser = BilibiliParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"bilibili": True, "timeout": 45},
                "performance": {"source_max_size": 90},
            },
            FakeDownloader(),
        )

        async def fake_get_video(**_kwargs):
            return object()

        async def fake_get_info(_video, _key):
            return raw_info

        async def fake_get_streams(_video, _index, _key):
            return [(64, ["https://example.com/video.m4s"])], [], {}

        parser._get_video = fake_get_video
        parser._get_video_info_cached = fake_get_info
        parser._get_stream_ladders_with_qn_fallback = fake_get_streams
        result = await parser.parse_video(bvid="BV1xx411c7mD", page_num=2)
        await result.contents[0].get_path()
        return result

    result = asyncio.run(run())
    assert result.title == "测试视频 - 第二集"
    assert result.text == "简介: 完整简介"
    assert result.extra["render_text_card"] is True
    assert result.extra["video_separate_from_card"] is True
    assert result.extra["card_platform_name"] == "B站视频"
    assert result.extra["text_card_media"].endswith("cover.jpg")
    assert result.extra["text_card_avatar"].endswith("avatar.jpg")
    assert ("播放", 123456) in result.extra["card_metrics"]
    assert "时长 2:10" in result.extra["card_info"]
    assert "分P 2/2" in result.extra["card_info"]
    assert "单机游戏" in result.extra["card_info"]
    assert "comment_document_task_factory" not in result.extra
    assert callable(result.extra["comment_image_task_factory"])
    assert result.extra["comment_timeout"] == 45


def test_bilibili_live_uses_native_images_and_text_without_render_card(tmp_path):
    downloaded = []

    class FakeDownloader:
        def download_img(self, url, **kwargs):
            downloaded.append((url, kwargs))

            async def done():
                return tmp_path / f"live-{len(downloaded)}.jpg"

            return asyncio.create_task(done())

    async def run():
        parser = BilibiliParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"bilibili": False},
                "performance": {},
                "rendering": {},
            },
            FakeDownloader(),
        )

        async def fake_get_json(url, _params, _room_id, retry=3):
            del retry
            if url.endswith("room_init"):
                return {
                    "code": 0,
                    "data": {
                        "room_id": 123,
                        "live_status": 1,
                        "uid": 42,
                    },
                }
            if url.endswith("getInfoByRoom"):
                return {
                    "code": 0,
                    "data": {
                        "room_info": {
                            "title": "直播标题",
                            "description": "<p>直播简介</p>",
                            "tags": "游戏,攻略",
                            "user_cover": "https://img.example.com/cover.jpg",
                            "keyframe": "https://img.example.com/keyframe.jpg",
                            "parent_area_name": "游戏",
                            "area_name": "原神",
                            "live_start_time": 1_700_000_000,
                        },
                        "anchor_info": {"base_info": {"uid": 42, "uname": "主播"}},
                    },
                }
            raise AssertionError(url)

        parser.live_service._get_json = fake_get_json
        try:
            result = await parser.live_service.parse_live(123)
            assert not hasattr(parser, "live_renderer")
            return result
        finally:
            await parser.close_session()

    result = asyncio.run(run())
    assert result.title == "直播标题"
    assert len(result.contents) == 2
    assert result.delivery is not None
    assert len(result.delivery.batches) == 1
    batch = result.delivery.batches[0]
    assert batch.parts[:2] == result.contents
    assert "识别：哔哩哔哩直播" in batch.parts[2]
    assert "直播简介" in batch.parts[2]
    assert "独立播放器" in batch.parts[2]
    assert [item[0] for item in downloaded] == [
        "https://img.example.com/cover.jpg",
        "https://img.example.com/keyframe.jpg",
    ]
    assert all(
        item[1]["ext_headers"]["Referer"] == "https://live.bilibili.com/123"
        for item in downloaded
    )


def test_text_card_without_author_does_not_render_empty_avatar(tmp_path):
    calls = []

    async def fake_html_render(template, data, *, return_url, options):
        calls.append(template)
        rendered = tmp_path / "rendered.png"
        _save_render_fixture(rendered)
        return str(rendered)

    asyncio.run(
        TextCardRenderer(HtmlRenderService(fake_html_render)).render_text_card(
            tmp_path / "game.png",
            platform_name="小黑盒",
            author_name=None,
            title="游戏标题",
            text="游戏简介",
        )
    )

    assert '<div class="profile">' not in calls[0]


def test_comment_images_keep_their_aspect_ratio_without_height_clipping():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户"),
                    content=[],
                    images=["https://i0.hdslb.com/tall.png"],
                )
            ],
        )
    )
    assert ".comment-image-wrap{display:block;width:fit-content" in bili_html
    assert (
        ".comment-image{display:block;width:auto;height:auto;max-width:540px"
        in bili_html
    )
    assert "comment-image{display:block;max-width:270px;max-height" not in bili_html

    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("用户"),
                    content=[],
                    images=["https://p3.douyinpic.com/tall.png"],
                    sticker_image="https://p3.douyinpic.com/sticker.gif",
                )
            ],
        )
    )
    assert 'class="comment-image-wrap"' in social_html
    assert 'class="sticker-image-wrap"' in social_html
    assert (
        ".comment-image{display:block;width:auto;height:auto;max-width:540px"
        in social_html
    )
    assert (
        ".sticker-image{display:block;width:auto;height:auto;max-width:180px;max-height:180px"
        in social_html
    )


def test_comment_reply_action_uses_svg_icon_instead_of_dotted_circle():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户"),
                    content=[],
                )
            ],
        )
    )
    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[CommentEntry(author=CommentAuthor("用户"), content=[])],
        )
    )

    for html in (bili_html, social_html):
        assert '<svg class="reply-icon action-icon"' in html
        assert "◌" not in html
        assert "fill:none;stroke:currentColor" in html
        assert '<svg class="like-icon action-icon"' in html


def test_image_download_preserves_detected_animated_format(tmp_path):
    async def run_download():
        downloader = object.__new__(Downloader)
        downloader.cache_dir = tmp_path
        requested_names = []

        async def fake_streamd(url, *, file_name, ext_headers=None):
            requested_names.append(file_name)
            output = tmp_path / file_name
            output.write_bytes(b"GIF89a" + b"\x00" * 128)
            return output

        downloader.streamd = fake_streamd
        output = await downloader.download_img("https://example.com/animated-image")
        return output, requested_names

    output, requested_names = asyncio.run(run_download())
    assert requested_names[0].endswith(".jpg")
    assert output.suffix == ".gif"
    assert output.read_bytes().startswith(b"GIF89a")
    assert not output.with_suffix(".jpg").exists()


def test_image_download_repairs_legacy_jpg_cache(tmp_path):
    async def run_download():
        downloader = object.__new__(Downloader)
        downloader.cache_dir = tmp_path
        legacy_name = generate_file_name("https://example.com/animated-image", ".jpg")
        legacy_path = tmp_path / legacy_name
        legacy_path.write_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 128)

        async def fail_streamd(*_args, **_kwargs):
            raise AssertionError("legacy cache should be reused")

        downloader.streamd = fail_streamd
        return await downloader.download_img("https://example.com/animated-image")

    output = asyncio.run(run_download())
    assert output.suffix == ".webp"
    assert output.exists()
    assert not output.with_suffix(".jpg").exists()


def test_bilibili_dynamic_image_cap_preserves_animated_formats():
    gif_url = "https://i0.hdslb.com/bfs/new_dyn/animated.gif"
    webp_url = "https://i0.hdslb.com/bfs/new_dyn/animated.webp?token=1"
    jpg_url = "https://i0.hdslb.com/bfs/new_dyn/photo.jpg?token=1"

    assert BiliDynamicService.cap_bili_image_url(gif_url) == gif_url
    assert BiliDynamicService.cap_bili_image_url(webp_url) == webp_url
    assert (
        BiliDynamicService.cap_bili_image_url(jpg_url)
        == "https://i0.hdslb.com/bfs/new_dyn/photo.jpg@1280w_85q.jpg?token=1"
    )


def test_bilibili_dynamic_defers_shared_card_to_sender(tmp_path):
    first = ImageContent(tmp_path / "first.jpg")
    second = ImageContent(tmp_path / "second.jpg")

    single_contents, single = BiliDynamicService._build_delivery_contents([first])
    multi_contents, multi = BiliDynamicService._build_delivery_contents([first, second])
    text_only_contents, text_only = BiliDynamicService._build_delivery_contents([])

    assert single is True
    assert single_contents == [first]
    assert multi is False
    assert multi_contents == [first, second]
    assert text_only is False
    assert text_only_contents == []


def test_bilibili_dynamic_repost_recovers_original_body_and_images():
    original_modules = {
        "module_author": {"name": "原UP主"},
        "module_dynamic": {
            "major": {
                "opus": {
                    "summary": {"text": "原动态完整正文"},
                    "pics": [{"url": "https://i0.hdslb.com/original.jpg"}],
                }
            }
        },
    }
    modules = {"module_dynamic": {"desc": {"text": "转发时补充的话"}}}
    item = {
        "modules": modules,
        "orig": {"modules": original_modules},
    }

    assert BiliDynamicService.extract_dynamic_text(item, modules) == (
        "转发时补充的话\n\n转发自 @原UP主：\n原动态完整正文"
    )
    assert BiliDynamicService.extract_dynamic_image_urls(item, modules) == [
        "https://i0.hdslb.com/original.jpg"
    ]


def test_bilibili_dynamic_collects_custom_emote_urls_from_rich_nodes():
    emotes = BiliDynamicService.extract_dynamic_emotes(
        {
            "modules": {
                "module_dynamic": {
                    "desc": {
                        "rich_text_nodes": [
                            {
                                "type": "RICH_TEXT_NODE_TYPE_TEXT",
                                "orig_text": "正文",
                            },
                            {
                                "type": "RICH_TEXT_NODE_TYPE_EMOJI",
                                "orig_text": "[UP主_好耶]",
                                "emoji": {
                                    "icon_url": "https://i0.hdslb.com/custom.png"
                                },
                            },
                        ]
                    }
                }
            }
        }
    )

    assert emotes == {"[UP主_好耶]": "https://i0.hdslb.com/custom.png"}


def test_bilibili_single_image_dynamic_uses_shared_card(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )

    class FakeParser:
        headers = {}

        @staticmethod
        def create_image_contents(urls, **_kwargs):
            return [
                PluginImageContent(tmp_path / f"image-{index}.jpg")
                for index, _url in enumerate(urls)
            ]

        @staticmethod
        def create_author(name, _avatar):
            return Author(name=name)

        @staticmethod
        def result(**kwargs):
            return ParseResult(
                platform=Platform(name="bilibili", display_name="B站"),
                **kwargs,
            )

    service = BiliDynamicService(FakeParser())

    async def fake_avatar(*_args, **_kwargs):
        return "data:image/png;base64,YXZhdGFy"

    async def fake_fetch(_dynamic_id):
        return {
            "item": {
                "modules": {
                    "module_author": {
                        "name": "UP主",
                        "face": "https://i0.hdslb.com/avatar.jpg",
                        "pub_ts": 1_700_000_000,
                    },
                    "module_dynamic": {
                        "desc": {"text": "不能被漏掉的动态正文"},
                        "major": {
                            "opus": {
                                "pics": [{"url": "https://i0.hdslb.com/dynamic.jpg"}]
                            }
                        },
                    },
                }
            }
        }

    service._fetch_dynamic_raw = fake_fetch
    service.img_to_data_uri = fake_avatar
    result = asyncio.run(service.parse_dynamic(123456))

    assert result.text == "不能被漏掉的动态正文"
    assert len(result.contents) == 1
    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_media"] == "https://i0.hdslb.com/dynamic.jpg"
    assert result.extra["text_card_avatar"].startswith("data:image/png")
    assert "单图已合并至卡片" in result.extra["card_info"]
    assert "reply_original_for_single_image" not in result.extra
    assert "send_text" not in result.extra


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


def test_a_bogus_matches_the_tracked_rconsole_algorithm_vector():
    query = (
        "device_platform=webapp&aid=6383&aweme_id=7414051930047106342&cursor=0&count=20"
    )
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    value = generate_a_bogus(
        query,
        user_agent,
        now_ms=1700000000123,
        random_values=[0.1234, 0.5678, 0.9012],
    )
    assert value == (
        "E7mhBdugDifihdWk56KLfY3q65e3Y0CI0trEMD2fnxVHqL39HMTa9exoIBGvXFEj"
        "wG/-IeYjy4hbT3ohrQ2y8qwf9W0L/25gsDSkKl12so0j53inCLf/E0iE5hsAtFH8s"
        "vr4iKi8owICSYyhldAJ5kIlO62-zo0/9IR="
    )


def test_a_bogus_sm3_fallback_is_portable():
    assert _sm3_fallback(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    )


def test_social_comment_canvas_escapes_jinja_from_user_content(tmp_path):
    calls = {}

    async def fake_canvas(template, data, *, return_url, options):
        calls["template"] = template
        calls["data"] = data
        output = tmp_path / "social.jpg"
        _save_render_fixture(output)
        return str(output)

    renderer = SocialCommentCanvas(HtmlRenderService(fake_canvas))
    document = CommentDocument(
        theme=DOUYIN_THEME,
        work_title="{{ 7 * 7 }}",
        cover="",
        total_text="1 条评论",
        entries=[
            CommentEntry(
                author=CommentAuthor("用户"),
                content=[CommentRichPart("text", "{% unsafe %}")],
            )
        ],
    )
    output = tmp_path / "result.jpg"
    asyncio.run(renderer.render(output, document))

    with Image.open(output) as image:
        assert image.width == 1140
        assert 170 <= image.height <= 173
    assert "{{ 7 * 7 }}" not in calls["template"]
    assert "{% unsafe %}" not in calls["template"]
    assert "&#123;" in calls["template"]


def test_douyin_comment_normalization_covers_rconsole_visible_fields(tmp_path):
    class FakeParser:
        headers = {"User-Agent": "Mozilla/5.0"}
        cookies = "sessionid=test"
        cache_dir = tmp_path

    feed = DouyinCommentFeed(FakeParser(), SocialCommentCanvas())
    normalized = feed.adapt_comment(
        {
            "user": {
                "uid": "42",
                "nickname": "作者",
                "avatar_thumb": {"url_list": ["http://p3.douyinpic.com/avatar"]},
            },
            "text": "你好[比心] @朋友 #话题#",
            "emoji": [
                {
                    "display_name": "比心",
                    "emoji_url": {"url_list": ["https://p3.douyinpic.com/emote"]},
                }
            ],
            "image_list": [
                {"origin_url": {"url_list": ["https://p3.douyinpic.com/pic"]}}
            ],
            "sticker_detail": {
                "static_url": {"url_list": ["https://p3.douyinpic.com/sticker"]}
            },
            "digg_count": 10000,
            "reply_comment_total": 2,
            "ip_label": "上海",
            "is_stick": True,
            "is_author_digged": True,
            "reply_comment": [
                {
                    "user": {"uid": "7", "nickname": "回复者"},
                    "text": "楼中楼",
                }
            ],
        },
        {"uid": "42", "nickname": "作者"},
        {},
    )

    assert normalized is not None
    assert normalized.author.badges[0].text == "作者"
    assert normalized.author.avatar.startswith("https://")
    assert {part.kind for part in normalized.content} >= {"emote", "highlight"}
    assert normalized.images == ["https://p3.douyinpic.com/pic"]
    assert normalized.sticker_image == "https://p3.douyinpic.com/sticker"
    assert normalized.like_text == "1万"
    assert normalized.reply_text == "回复 2"
    assert normalized.location == "上海"
    assert normalized.pinned is True
    assert normalized.creator_liked is True
    assert normalized.first_reply is not None


def test_native_parsers_attach_comment_factories(tmp_path):
    class FakeDownloader:
        def download_img(self, *_args, **_kwargs):
            async def finish():
                output = tmp_path / "image.jpg"
                output.write_bytes(b"image")
                return output

            return asyncio.create_task(finish())

    config = {
        "cache_dir": str(tmp_path),
        "cookies": {"douyin_ck": "sessionid=test", "weibo_cookie": ""},
        "comments": {
            "douyin": True,
            "weibo": True,
            "display_count": 6,
            "timeout": 45,
        },
    }

    async def run():
        downloader = FakeDownloader()
        douyin = DouyinParser(config, downloader)
        result = douyin._build_result_from_aweme(
            {
                "aweme_id": "7414051930047106342",
                "desc": "作品[比心]",
                "create_time": 1700000000,
                "author": {"uid": "42", "nickname": "作者"},
                "text_extra": [
                    {
                        "text": "[比心]",
                        "emoji_url": {
                            "url_list": ["https://p3.douyinpic.com/bixin.png"]
                        },
                    }
                ],
                "images": [{"url_list": ["https://p3.douyinpic.com/image"]}],
            },
            "7414051930047106342",
        )
        weibo = WeiboParser(config, downloader)
        weibo_extra = weibo._comment_extra(
            "4461526582968019",
            title="微博",
            cover="",
            owner_id="42",
        )
        await asyncio.gather(
            *(content.get_path() for content in result.contents),
            return_exceptions=True,
        )
        await douyin.close_session()
        await weibo.close_session()
        return result.extra, weibo_extra

    douyin_extra, weibo_extra = asyncio.run(run())
    assert "comment_document_task_factory" not in douyin_extra
    assert callable(douyin_extra["comment_image_task_factory"])
    assert douyin_extra["comment_timeout"] == 45
    assert douyin_extra["card_emotes"] == {
        "[比心]": "https://p3.douyinpic.com/bixin.png",
        "比心": "https://p3.douyinpic.com/bixin.png",
    }
    assert "comment_document_task_factory" not in weibo_extra
    assert callable(weibo_extra["comment_image_task_factory"])
    assert weibo_extra["comment_timeout"] == 45


def test_weibo_media_result_keeps_body_before_single_image_reply(tmp_path):
    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "weibo.jpg"

            return asyncio.create_task(done())

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": 1,
                "data": {
                    "id": "4461526582968019",
                    "mid": "4461526582968019",
                    "created_at": "Fri Jan 17 01:04:51 +0800 2020",
                    "text": (
                        "<p>微博正文"
                        '<img alt="[笑cry]" src="https://h5.sinaimg.cn/emote.png">'
                        "</p>"
                    ),
                    "user": {
                        "id": "1088413295",
                        "screen_name": "Easy",
                        "avatar_large": "https://wx1.sinaimg.cn/avatar.jpg",
                    },
                    "pics": [
                        {"large": {"url": "https://wx1.sinaimg.cn/large/demo.jpg"}}
                    ],
                },
            }

    class FakeClient:
        async def get(self, _url, **_kwargs):
            return FakeResponse()

        async def close(self):
            return None

    async def run():
        parser = WeiboParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"weibo": False},
            },
            FakeDownloader(),
        )
        parser._session = FakeClient()

        try:
            keyword, searched = parser.search_url(
                "https://weibo.com/1088413295/IpOAqcs7h"
            )
            return await parser.parse(keyword, searched)
        finally:
            await parser.close_session()

    result = asyncio.run(run())
    assert result.text == "微博正文[笑cry]"
    assert result.extra["card_emotes"] == {
        "[笑cry]": "https://h5.sinaimg.cn/emote.png",
        "笑cry": "https://h5.sinaimg.cn/emote.png",
    }
    assert len(result.contents) == 1
    assert result.delivery is not None
    assert len(result.delivery.batches) == 2
    assert result.delivery.batches[0].parts == ["识别：微博\n微博正文[笑cry]"]
    assert result.delivery.batches[0].reply_original is False
    assert result.delivery.batches[1].parts == result.contents
    assert result.delivery.batches[1].reply_original is True


def test_weibo_long_post_fetches_complete_body(tmp_path):
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeClient:
        async def get(self, url, **_kwargs):
            calls.append(url)
            if "/statuses/extend" in url:
                return FakeResponse(
                    {
                        "ok": 1,
                        "data": {"longTextContent": "<p>完整长微博正文</p>"},
                    }
                )
            return FakeResponse(
                {
                    "ok": 1,
                    "data": {
                        "id": "4910316167105260",
                        "isLongText": True,
                        "text": "<p>截断正文...</p>",
                        "user": {
                            "id": "42",
                            "screen_name": "作者",
                            "avatar_large": "https://wx1.sinaimg.cn/avatar.jpg",
                        },
                    },
                }
            )

        async def close(self):
            return None

    async def run():
        parser = WeiboParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"weibo": False},
            },
            object(),
        )
        parser._session = FakeClient()
        try:
            keyword, searched = parser.search_url(
                "https://m.weibo.cn/detail/4910316167105260"
            )
            return await parser.parse(keyword, searched)
        finally:
            await parser.close_session()

    result = asyncio.run(run())
    assert calls == [
        "https://m.weibo.cn/statuses/show?id=4910316167105260",
        "https://m.weibo.cn/statuses/extend?id=4910316167105260",
    ]
    assert result.text == "完整长微博正文"
    assert result.delivery is not None
    assert result.delivery.batches[0].parts == ["识别：微博\n完整长微博正文"]
    assert result.extra["text_card_media"] == ""


def test_weibo_recovers_missing_api_body_from_authenticated_detail(tmp_path):
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, *, payload=None, text=""):
            self.payload = payload
            self.text = text

        def json(self):
            return self.payload

    class FakeClient:
        async def get(self, url, **kwargs):
            calls.append((url, kwargs["headers"].get("Cookie")))
            if "/statuses/show" in url:
                return FakeResponse(
                    payload={
                        "ok": 1,
                        "data": {
                            "id": "123",
                            "text": "",
                            "user": {"id": "42", "screen_name": "作者"},
                        },
                    }
                )
            return FakeResponse(
                text=(
                    '<script>$render_data = [{"status":{"id":"123",'
                    '"text":"<p>详情页补回的正文</p>",'
                    '"user":{"id":"42","screen_name":"作者"}}}][0]</script>'
                )
            )

        async def close(self):
            return None

    async def run():
        parser = WeiboParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {"weibo_cookie": "SUB=test"},
                "comments": {"weibo": False},
            },
            object(),
        )
        assert "Cookie" not in parser.headers
        assert parser._request_headers()["Cookie"] == "SUB=test"
        parser._session = FakeClient()
        try:
            keyword, searched = parser.search_url("https://m.weibo.cn/detail/123")
            return await parser.parse(keyword, searched)
        finally:
            await parser.close_session()

    result = asyncio.run(run())
    assert result.text == "详情页补回的正文"
    assert result.delivery is not None
    assert result.delivery.batches[0].parts == ["识别：微博\n详情页补回的正文"]
    assert calls == [
        ("https://m.weibo.cn/statuses/show?id=123", "SUB=test"),
        ("https://m.weibo.cn/detail/123", "SUB=test"),
    ]


def test_weibo_keeps_original_body_for_reposts(tmp_path):
    async def run():
        parser = WeiboParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"weibo": False},
            },
            object(),
        )
        try:
            return await parser._resolve_body_text(
                {
                    "text": "转发微博",
                    "retweeted_status": {
                        "id": "456",
                        "text": "<p>不能被吞掉的原微博正文</p>",
                        "user": {"screen_name": "原作者"},
                    },
                },
                "123",
            )
        finally:
            await parser.close_session()

    assert asyncio.run(run()) == "转发自 @原作者：不能被吞掉的原微博正文"


def test_weibo_comment_normalization_preserves_html_and_nested_reply(tmp_path):
    class FakeParser:
        headers = {}
        cookie = ""
        cache_dir = tmp_path

    feed = WeiboCommentFeed(FakeParser(), SocialCommentCanvas())
    normalized = feed.adapt_comment(
        {
            "user": {
                "id": 42,
                "screen_name": "原博",
                "verified": True,
                "mbrank": 3,
                "avatar_hd": "https://wx1.sinaimg.cn/avatar.jpg",
            },
            "text": (
                '回复<a href="/n/demo">@朋友</a>：正文'
                '<img alt="[笑cry]" src="https://h5.sinaimg.cn/emote.png">'
            ),
            "like_count": 100000000,
            "total_number": 1,
            "isLikedByMblogAuthor": True,
            "comments": [
                {
                    "user": {"id": 7, "screen_name": "回复者"},
                    "text": "楼中楼",
                }
            ],
        },
        "42",
    )

    assert normalized is not None
    assert {badge.text for badge in normalized.author.badges} >= {"原博", "V", "VIP3"}
    assert {part.kind for part in normalized.content} >= {"highlight", "emote"}
    assert normalized.like_text == "1亿"
    assert normalized.creator_liked is True
    assert normalized.first_reply is not None


def test_weibo_comment_string_zero_cursor_marks_feed_complete(tmp_path):
    class FakeResponse:
        status_code = 200
        content = json.dumps(
            {
                "ok": 1,
                "data": {
                    "data": [{"id": "1", "text": "评论"}],
                    "total_number": 1,
                    "max_id": "0",
                    "max_id_type": 0,
                },
            }
        ).encode()

    class FakeParser:
        headers = {}
        cookie = ""
        cache_dir = tmp_path
        calls = 0

        async def http_get(self, *_args, **_kwargs):
            self.calls += 1
            return FakeResponse()

    parser = FakeParser()
    feed = WeiboCommentFeed(parser, SocialCommentCanvas())
    result = asyncio.run(feed.fetch("4461526582968019"))

    assert parser.calls == 1
    assert result.has_more is False


def test_bilibili_comment_feed_renders_all_selected_comments_in_one_image(tmp_path):
    class FakeParser:
        headers = {}
        bili_ck = ""
        cache_dir = tmp_path

        @staticmethod
        def norm_bili_img(url):
            return url

    class FakeCanvas:
        documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        canvas = FakeCanvas()
        feed = BiliCommentFeed(FakeParser(), canvas, limit=5)

        async def fake_fetch(_oid, _type):
            items = [
                {
                    "rpid": index,
                    "member": {"uname": f"用户{index}"},
                    "content": {"message": f"评论{index}"},
                }
                for index in range(5)
            ]
            return SimpleNamespace(items=items, owner_mid="", total=99)

        feed.fetch = fake_fetch
        contents = await feed.build_images(
            2,
            1,
            video_title="标题",
            video_cover="",
        )
        paths = await asyncio.gather(*(content.get_path() for content in contents))
        return canvas, paths

    canvas, paths = asyncio.run(run())
    assert len(paths) == 1
    assert len(canvas.documents) == 1
    assert len(canvas.documents[0].entries) == 5


def test_douyin_comment_feed_renders_all_selected_comments_in_one_image(tmp_path):
    class FakeParser:
        headers = {}
        cookies = "sessionid=test"
        cache_dir = tmp_path

    class FakeCanvas:
        documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        canvas = FakeCanvas()
        feed = DouyinCommentFeed(FakeParser(), canvas, limit=5)

        async def fake_fetch(_aweme_id):
            items = [
                {
                    "cid": str(index),
                    "user": {"uid": str(index), "nickname": f"用户{index}"},
                    "text": f"评论{index}",
                    "digg_count": 5 - index,
                }
                for index in range(5)
            ]
            return SimpleNamespace(items=items, total=99, has_more=False)

        async def fake_emoji_map():
            return {}

        feed.fetch = fake_fetch
        feed._load_emoji_map = fake_emoji_map
        contents = await feed.build_images(
            "7414051930047106342",
            work_title="标题",
            cover="",
        )
        paths = await asyncio.gather(*(content.get_path() for content in contents))
        return canvas, paths

    canvas, paths = asyncio.run(run())
    assert len(paths) == 1
    assert len(canvas.documents) == 1
    assert len(canvas.documents[0].entries) == 5


def test_weibo_comment_feed_renders_all_selected_comments_in_one_image(tmp_path):
    class FakeParser:
        headers = {}
        cookie = ""
        cache_dir = tmp_path

    class FakeCanvas:
        documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        canvas = FakeCanvas()
        feed = WeiboCommentFeed(FakeParser(), canvas, limit=5)

        async def fake_fetch(_mid):
            items = [
                {
                    "id": str(index),
                    "user": {"id": index, "screen_name": f"用户{index}"},
                    "text": f"评论{index}",
                }
                for index in range(5)
            ]
            return SimpleNamespace(items=items, total=99, has_more=False)

        feed.fetch = fake_fetch
        contents = await feed.build_images(
            "4461526582968019",
            work_title="标题",
            cover="",
            owner_id="42",
        )
        paths = await asyncio.gather(*(content.get_path() for content in contents))
        return canvas, paths

    canvas, paths = asyncio.run(run())
    assert len(paths) == 1
    assert len(canvas.documents) == 1
    assert len(canvas.documents[0].entries) == 5


def test_weibo_comment_feed_embeds_main_and_reply_avatars(tmp_path):
    avatar_bytes = b"weibo-avatar"

    class FakeResponse:
        status_code = 200
        content = avatar_bytes
        headers = {"Content-Type": "image/jpeg"}

    class FakeParser:
        headers = {"User-Agent": "test"}
        cookie = "SUB=test"
        cache_dir = tmp_path
        requests = []

        async def http_get(self, url, **kwargs):
            self.requests.append((url, kwargs))
            return FakeResponse()

    class FakeCanvas:
        documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        parser = FakeParser()
        canvas = FakeCanvas()
        feed = WeiboCommentFeed(parser, canvas, limit=1)

        async def fake_fetch(_mid):
            return SimpleNamespace(
                items=[
                    {
                        "id": "1",
                        "user": {
                            "id": "1",
                            "screen_name": "主评论",
                            "avatar_large": "https://tvax1.sinaimg.cn/avatar.jpg",
                        },
                        "text": "评论",
                        "comments": [
                            {
                                "id": "2",
                                "user": {
                                    "id": "2",
                                    "screen_name": "回复",
                                    "avatar_large": (
                                        "https://tvax2.sinaimg.cn/reply.jpg"
                                    ),
                                },
                                "text": "回复内容",
                            }
                        ],
                    }
                ],
                total=1,
                has_more=False,
            )

        feed.fetch = fake_fetch
        contents = await feed.build_images(
            "4461526582968019",
            work_title="标题",
            cover="",
            owner_id="42",
        )
        await asyncio.gather(*(content.get_path() for content in contents))
        return parser, canvas

    parser, canvas = asyncio.run(run())
    document = canvas.documents[0]
    assert document.entries[0].author.avatar.startswith("data:image/jpeg;base64,")
    assert document.entries[0].first_reply is not None
    assert document.entries[0].first_reply.author.avatar.startswith(
        "data:image/jpeg;base64,"
    )
    assert len(parser.requests) == 2
    for _, kwargs in parser.requests:
        assert kwargs["headers"]["Referer"] == "https://m.weibo.cn/"
        assert kwargs["headers"]["Cookie"] == "SUB=test"


def test_comment_feed_footers_use_repository_brand(tmp_path):
    class BiliParser:
        headers = {}
        bili_ck = ""
        cache_dir = tmp_path

        @staticmethod
        def norm_bili_img(url):
            return url

    class DouyinParserStub:
        headers = {}
        cookies = "sessionid=test"
        cache_dir = tmp_path

    class WeiboParserStub:
        headers = {}
        cookie = ""
        cache_dir = tmp_path

    class XiaoheiheParserStub:
        headers = {}
        cache_dir = tmp_path

    class MiyousheParserStub:
        headers = {}
        cache_dir = tmp_path

    class FakeCanvas:
        def __init__(self):
            self.documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        bili_canvas = FakeCanvas()
        bili_feed = BiliCommentFeed(BiliParser(), bili_canvas, limit=1)

        async def bili_fetch(_oid, _type):
            return SimpleNamespace(
                items=[
                    {
                        "rpid": 1,
                        "member": {"uname": "用户"},
                        "content": {"message": "评论"},
                    }
                ],
                owner_mid="",
                total=2,
            )

        bili_feed.fetch = bili_fetch

        douyin_canvas = FakeCanvas()
        douyin_feed = DouyinCommentFeed(DouyinParserStub(), douyin_canvas, limit=1)

        async def douyin_fetch(_aweme_id):
            return SimpleNamespace(
                items=[
                    {
                        "cid": "1",
                        "user": {"uid": "1", "nickname": "用户"},
                        "text": "评论",
                    }
                ],
                total=2,
                has_more=False,
            )

        async def fake_emoji_map():
            return {}

        douyin_feed.fetch = douyin_fetch
        douyin_feed._load_emoji_map = fake_emoji_map

        weibo_canvas = FakeCanvas()
        weibo_feed = WeiboCommentFeed(WeiboParserStub(), weibo_canvas, limit=1)

        async def weibo_fetch(_mid):
            return SimpleNamespace(
                items=[
                    {
                        "id": "1",
                        "user": {"id": "1", "screen_name": "用户"},
                        "text": "评论",
                    }
                ],
                total=2,
                has_more=False,
            )

        weibo_feed.fetch = weibo_fetch

        xiaoheihe_canvas = FakeCanvas()
        xiaoheihe_feed = XiaoheiheCommentFeed(
            XiaoheiheParserStub(),
            xiaoheihe_canvas,
            limit=1,
        )
        xiaoheihe_contents = await xiaoheihe_feed.build_images(
            "5",
            [
                {"comment": "malformed"},
                {
                    "comment": [
                        {
                            "floor_num": 2,
                            "text": "评论",
                            "user": {"username": "用户"},
                        }
                    ]
                },
            ],
            work_title="标题",
            cover="",
            owner_id="",
            total=2,
        )

        miyoushe_canvas = FakeCanvas()
        miyoushe_feed = MiyousheCommentFeed(
            MiyousheParserStub(),
            miyoushe_canvas,
            limit=1,
        )

        async def miyoushe_fetch(_post_id):
            return SimpleNamespace(
                items=[
                    {
                        "reply": {"content": "评论", "created_at": 1},
                        "user": {"uid": "1", "nickname": "用户"},
                        "stat": {"like_num": 2},
                    }
                ],
                total=2,
                has_more=False,
            )

        miyoushe_feed.fetch = miyoushe_fetch

        results = await asyncio.gather(
            bili_feed.build_images(2, 1, video_title="标题", video_cover=""),
            douyin_feed.build_images("3", work_title="标题", cover=""),
            weibo_feed.build_images("4", work_title="标题", cover="", owner_id=""),
            asyncio.sleep(0, result=xiaoheihe_contents),
            miyoushe_feed.build_images(
                "6",
                work_title="标题",
                cover="",
                owner_id="",
            ),
        )
        await asyncio.gather(
            *(content.get_path() for contents in results for content in contents)
        )
        return (
            bili_canvas,
            douyin_canvas,
            weibo_canvas,
            xiaoheihe_canvas,
            miyoushe_canvas,
        )

    canvases = asyncio.run(run())
    for canvas in canvases:
        assert COMMENT_FOOTER_BRAND in canvas.documents[0].footer_text
        assert "Parser X" not in canvas.documents[0].footer_text


def test_comment_layout_cache_versions_invalidate_pre_fix_images():
    assert BiliCommentFeed.CACHE_VERSION == "bili_comment_v10_unified_minimal"
    assert DouyinCommentFeed.CACHE_VERSION == "douyin_comment_v9_unified_minimal"
    assert WeiboCommentFeed.CACHE_VERSION == "weibo_comment_v9_unified_minimal"
    assert XiaoheiheCommentFeed.CACHE_VERSION == "xiaoheihe_comment_v3_platform_emotes"
    assert MiyousheCommentFeed.CACHE_VERSION == "miyoushe_comment_v3_platform_emotes"


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


def test_single_image_result_replies_to_original_message(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "single.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"show_download_fail_tip": True}

    async def fake_download_content(_self, _cont):
        return _cont, image_path, None

    class Event:
        message_obj = SimpleNamespace(message_id=2468)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    event = Event()
    content = PluginImageContent(image_path)
    result = ParseResult(
        platform=Platform(name="bilibili", display_name="B站"),
        author=Author(name="UP主"),
        title="动态标题",
        contents=[content],
        extra={
            "reply_original_for_single_image": True,
            "send_text": True,
        },
        text="不能被漏掉的动态正文",
        url="https://example.com",
    )

    with (
        patch.object(ParserXPlugin, "_download_content", fake_download_content),
        patch.object(
            ParserXPlugin,
            "_convert_to_seg",
            return_value=MessageImage(str(image_path)),
        ),
    ):
        asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Reply)
    assert event.sent[0][0].id == 2468
    assert isinstance(event.sent[0][1], Plain)
    assert "不能被漏掉的动态正文" in event.sent[0][1].text
    assert isinstance(event.sent[0][2], MessageImage)


def test_delivery_plan_sends_weibo_body_before_single_image_reply(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "single.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    async def fake_download_content(_self, content):
        return content, image_path, None

    class Event:
        message_obj = SimpleNamespace(message_id=9753)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    event = Event()
    content = PluginImageContent(image_path)
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        author=Author(name="作者"),
        text="不能被吞掉的正文",
        contents=[content],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["识别：微博\n不能被吞掉的正文"]),
                DeliveryBatch([content], reply_original=True),
            ]
        ),
        url="https://weibo.com/example",
    )

    with (
        patch.object(ParserXPlugin, "_download_content", fake_download_content),
        patch.object(
            ParserXPlugin,
            "_convert_to_seg",
            return_value=MessageImage(str(image_path)),
        ),
    ):
        asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 2
    assert isinstance(event.sent[0][0], Plain)
    assert "不能被吞掉的正文" in event.sent[0][0].text
    assert isinstance(event.sent[1][0], Reply)
    assert event.sent[1][0].id == 9753
    assert isinstance(event.sent[1][1], MessageImage)


def test_delivery_plan_replaces_summary_with_unified_card(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    source_path = tmp_path / "source.jpg"
    source_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_text_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=9755)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    content = PluginImageContent(source_path)
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        author=Author(name="作者"),
        title="微博标题",
        text="正文必须进入统一卡片",
        contents=[content],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["识别：微博\n正文必须进入统一卡片"]),
                DeliveryBatch([content], reply_original=True),
            ]
        ),
        url="https://weibo.com/example",
        extra={
            "render_text_card": True,
            "text_card_media": "https://img.example.com/source.jpg",
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.kwargs["platform_key"] == "weibo"
    assert plugin.text_renderer.kwargs["text"] == "正文必须进入统一卡片"
    assert plugin.text_renderer.kwargs["media_fit"] == "contain"
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 2
    assert isinstance(event.sent[0][0], Reply)
    assert isinstance(event.sent[0][1], MessageImage)


def test_delivery_card_stays_separate_from_rich_text_images_and_video(
    tmp_path,
):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    cover_path = tmp_path / "cover.png"
    body_path = tmp_path / "body.png"
    video_path = tmp_path / "video.mp4"
    _save_render_fixture(cover_path)
    _save_render_fixture(body_path, background="#c83a47")
    video_path.write_bytes(b"video")

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        async def render_text_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 320))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2807)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    cover = PluginImageContent(cover_path)
    body_image = PluginImageContent(body_path)
    video = PluginVideoContent(video_path)
    result = ParseResult(
        platform=Platform(name="xiaoheihe", display_name="小黑盒"),
        title="帖子标题",
        text="富文本正文",
        contents=[cover, body_image, video],
        delivery=DeliveryPlan(
            [
                DeliveryBatch([cover, "帖子概要"]),
                DeliveryBatch(["富文本正文", body_image], mode="forward"),
                DeliveryBatch([video]),
            ]
        ),
        extra={"render_text_card": True},
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 4
    card_chains = [
        chain
        for chain in event.sent
        if len(chain) == 2
        and isinstance(chain[0], Reply)
        and isinstance(chain[1], MessageImage)
    ]
    assert len(card_chains) == 1
    assert card_chains[0][1].file != str(cover_path)
    assert any(
        len(chain) == 1
        and isinstance(chain[0], MessageImage)
        and chain[0].file == str(cover_path)
        for chain in event.sent
    )
    forward = next(chain[0] for chain in event.sent if isinstance(chain[0], Nodes))
    assert len(forward.nodes) == 2
    assert all(len(node.content) == 1 for node in forward.nodes)
    assert isinstance(forward.nodes[0].content[0], Plain)
    assert isinstance(forward.nodes[1].content[0], MessageImage)
    assert any(
        len(chain) == 1 and isinstance(chain[0], MessageVideo) for chain in event.sent
    )


def test_comments_are_forwarded_separately_from_body_card(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    comment_path = tmp_path / "comment.jpg"
    comment_path.write_bytes(b"comment")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    async def build_comment_images():
        return [PluginImageContent(comment_path)]

    class FakeTextRenderer:
        def __init__(self):
            self.documents = []

        async def render_text_card(self, out_path, **kwargs):
            self.documents.append(kwargs["comment_document"])
            _save_render_fixture(out_path, size=(760, 420))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=9757)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    async def fake_download_content(_self, content):
        return content, await content.get_path(), None

    result = ParseResult(
        platform=Platform(name="douyin", display_name="抖音"),
        title="作品标题",
        text="正文",
        extra={
            "render_text_card": True,
            "comment_image_task_factory": build_comment_images,
            "comment_timeout": 5,
        },
    )
    event = Event()
    with patch.object(ParserXPlugin, "_download_content", fake_download_content):
        asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.documents == [None]
    assert len(event.sent) == 2
    assert isinstance(event.sent[0][0], Reply)
    assert isinstance(event.sent[0][1], MessageImage)
    assert isinstance(event.sent[1][0], Nodes)
    nodes = event.sent[1][0].nodes
    assert len(nodes) == 2
    assert isinstance(nodes[0].content[0], Plain)
    assert nodes[0].content[0].text == "抖音 · 热门评论"
    assert isinstance(nodes[1].content[0], MessageImage)
    assert nodes[1].content[0].file == str(comment_path)


def test_comment_failure_does_not_block_body_card_or_video(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ParseResult,
        Platform,
        VideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    async def build_comment_images():
        raise RuntimeError("comment unavailable")

    class FakeTextRenderer:
        def __init__(self):
            self.documents = []

        async def render_text_card(self, out_path, **kwargs):
            self.documents.append(kwargs["comment_document"])
            _save_render_fixture(out_path, size=(760, 360))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=9758)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    async def fake_download_content(_self, content):
        return content, await content.get_path(), None

    result = ParseResult(
        platform=Platform(name="douyin", display_name="抖音"),
        title="作品标题",
        text="评论失败也不能吞掉的正文",
        contents=[VideoContent(video_path)],
        extra={
            "render_text_card": True,
            "video_separate_from_card": True,
            "comment_image_task_factory": build_comment_images,
            "comment_timeout": 5,
        },
    )
    event = Event()

    with patch.object(ParserXPlugin, "_download_content", fake_download_content):
        asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.documents == [None]
    assert len(event.sent) == 2
    assert any(
        len(chain) == 2
        and isinstance(chain[0], Reply)
        and isinstance(chain[1], MessageImage)
        for chain in event.sent
    )
    assert any(
        len(chain) == 1 and isinstance(chain[0], MessageVideo) for chain in event.sent
    )
    assert not any(isinstance(chain[0], Nodes) for chain in event.sent)
    assert "评论失败也不能吞掉的正文" in result.text


def test_comment_forward_failure_falls_back_to_title_and_images(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    comment_path = tmp_path / "comment.jpg"
    comment_path.write_bytes(b"comment")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    async def build_comment_images():
        return [PluginImageContent(comment_path)]

    class Event:
        message_obj = SimpleNamespace(message_id=9758)

        def __init__(self):
            self.sent = []
            self.forward_attempts = 0

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            if result and isinstance(result[0], Nodes):
                self.forward_attempts += 1
                raise RuntimeError("forward unavailable")
            self.sent.append(result)

    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        text="正文",
        extra={
            "plain_text_only": True,
            "comment_image_task_factory": build_comment_images,
            "comment_timeout": 5,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert event.forward_attempts == 1
    assert len(event.sent) == 3
    assert event.sent[0][0].text == "正文"
    assert event.sent[1][0].text == "微博 · 热门评论"
    assert isinstance(event.sent[2][0], MessageImage)


def test_delivery_card_failure_keeps_plain_summary(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FailingTextRenderer:
        async def render_text_card(self, *_args, **_kwargs):
            raise RuntimeError("renderer unavailable")

    plugin.text_renderer = FailingTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=9756)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        text="渲染失败仍要保留正文",
        delivery=DeliveryPlan([DeliveryBatch(["识别：微博\n渲染失败仍要保留正文"])]),
        extra={"render_text_card": True},
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Plain)
    assert "渲染失败仍要保留正文" in event.sent[0][0].text


def test_delivery_plan_repairs_missing_weibo_body():
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    class Event:
        message_obj = SimpleNamespace(message_id=9754)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        text="必须补回的微博正文",
        delivery=DeliveryPlan([DeliveryBatch(["识别：微博"])]),
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Plain)
    assert event.sent[0][0].text == "识别：微博\n必须补回的微博正文"


def test_delivery_plan_sends_body_before_waiting_for_media(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "delayed.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    class Event:
        message_obj = SimpleNamespace(message_id=8642)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    event = Event()
    content = PluginImageContent(image_path)
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        text="正文先发",
        contents=[content],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["识别：微博\n正文先发"]),
                DeliveryBatch([content], reply_original=True),
            ]
        ),
        url="https://weibo.com/example",
    )

    async def run():
        download_started = asyncio.Event()
        release_download = asyncio.Event()

        async def delayed_download(_self, media):
            download_started.set()
            await release_download.wait()
            return media, image_path, None

        with patch.object(ParserXPlugin, "_download_content", delayed_download):
            send_task = asyncio.create_task(plugin._send_parse_result(event, result))
            await asyncio.wait_for(download_started.wait(), timeout=1)
            assert len(event.sent) == 1
            assert isinstance(event.sent[0][0], Plain)
            assert "正文先发" in event.sent[0][0].text
            release_download.set()
            await send_task

    asyncio.run(run())
    assert len(event.sent) == 2
    assert isinstance(event.sent[1][0], Reply)
    assert isinstance(event.sent[1][1], MessageImage)


def test_delivery_plan_keeps_images_and_video_in_separate_messages(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    video_path = tmp_path / "video.mp4"
    for path in (first_path, second_path, video_path):
        path.write_bytes(b"media")

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    class Event:
        message_obj = SimpleNamespace(message_id=1)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    first = PluginImageContent(first_path)
    second = PluginImageContent(second_path)
    video = PluginVideoContent(video_path)
    result = ParseResult(
        platform=Platform(name="test", display_name="测试"),
        contents=[first, second, video],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["正文"]),
                DeliveryBatch([first, second]),
                DeliveryBatch([video]),
            ]
        ),
        url="https://example.com",
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 3
    assert isinstance(event.sent[0][0], Plain)
    assert all(isinstance(item, MessageImage) for item in event.sent[1])
    assert len(event.sent[1]) == 2
    assert isinstance(event.sent[2][0], MessageVideo)


def test_delivery_plan_forward_failure_falls_back_in_original_order(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "body.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}

    class Event:
        message_obj = SimpleNamespace(message_id=1)

        def __init__(self):
            self.sent = []
            self.forward_attempts = 0

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            if result and isinstance(result[0], Nodes):
                self.forward_attempts += 1
                raise RuntimeError("forward unavailable")
            self.sent.append(result)

    image = PluginImageContent(image_path)
    result = ParseResult(
        platform=Platform(name="test", display_name="测试"),
        contents=[image],
        delivery=DeliveryPlan([DeliveryBatch(["前文", image, "后文"], mode="forward")]),
        url="https://example.com",
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert event.forward_attempts == 1
    assert len(event.sent) == 3
    assert isinstance(event.sent[0][0], Plain)
    assert event.sent[0][0].text == "前文"
    assert isinstance(event.sent[1][0], MessageImage)
    assert isinstance(event.sent[2][0], Plain)
    assert event.sent[2][0].text == "后文"


def test_single_image_card_embeds_source_without_resending_it(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "single.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        async def render_text_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=1357)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    event = Event()
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        author=Author(name="作者"),
        text="不能被吞掉的正文",
        contents=[PluginImageContent(image_path)],
        url="https://weibo.com/example",
        extra={
            "render_text_card": True,
            "text_card_media": "https://img.example.com/source.jpg",
        },
    )

    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Reply)
    assert event.sent[0][0].id == 1357
    assert len(event.sent[0]) == 2
    assert isinstance(event.sent[0][1], MessageImage)


def test_unified_card_is_not_sent_standalone_without_original_message_id(tmp_path):
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        async def render_text_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=None)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        text="缺少引用目标时必须保留的正文",
        extra={"render_text_card": True},
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Plain)
    assert "缺少引用目标时必须保留的正文" in event.sent[0][0].text


def test_multi_image_card_replies_before_source_image_forward(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import (
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        async def render_text_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 320))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=1359)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    async def fake_download_content(_self, content):
        return content, await content.get_path(), None

    result = ParseResult(
        platform=Platform(name="xiaohongshu", display_name="小红书"),
        text="图文正文",
        contents=[
            PluginImageContent(first_path),
            PluginImageContent(second_path),
        ],
        extra={"render_text_card": True},
    )
    event = Event()
    with patch.object(ParserXPlugin, "_download_content", fake_download_content):
        asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 2
    assert isinstance(event.sent[0][0], Reply)
    assert isinstance(event.sent[0][1], MessageImage)
    assert isinstance(event.sent[1][0], Nodes)
    assert len(event.sent[1][0].nodes) == 2
    assert all(len(node.content) == 1 for node in event.sent[1][0].nodes)
    assert all(
        isinstance(node.content[0], MessageImage) for node in event.sent[1][0].nodes
    )


def test_video_shared_card_and_video_are_sent_as_separate_messages(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import (
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeTextRenderer:
        async def render_text_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 320))

    plugin.text_renderer = FakeTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=1358)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    async def fake_download_content(_self, content):
        return content, await content.get_path(), None

    def fake_convert_to_seg(_self, content, path):
        if isinstance(content, PluginVideoContent):
            return MessageVideo(str(path))
        assert isinstance(content, PluginImageContent)
        return MessageImage(str(path))

    result = ParseResult(
        platform=Platform(name="bilibili", display_name="B站"),
        title="视频标题",
        text="视频简介",
        contents=[PluginVideoContent(video_path)],
        extra={
            "render_text_card": True,
            "video_separate_from_card": True,
        },
    )
    event = Event()
    with (
        patch.object(ParserXPlugin, "_download_content", fake_download_content),
        patch.object(ParserXPlugin, "_convert_to_seg", fake_convert_to_seg),
    ):
        asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 2
    assert any(
        len(chain) == 2
        and isinstance(chain[0], Reply)
        and isinstance(chain[1], MessageImage)
        for chain in event.sent
    )
    assert any(
        len(chain) == 1 and isinstance(chain[0], MessageVideo) for chain in event.sent
    )
    assert not any(
        isinstance(segment, Nodes) for chain in event.sent for segment in chain
    )


@pytest.mark.parametrize("first_ready", ["video", "card"])
def test_video_and_card_send_in_completion_order(tmp_path, first_ready):
    from astrbot_plugin_parser_x.core.data import (
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    async def run():
        card_started = asyncio.Event()
        video_started = asyncio.Event()
        release_card = asyncio.Event()
        release_video = asyncio.Event()
        first_sent = asyncio.Event()

        class FakeTextRenderer:
            async def render_text_card(self, out_path, **_kwargs):
                card_started.set()
                if first_ready == "video":
                    await release_card.wait()
                _save_render_fixture(out_path, size=(760, 320))

        plugin.text_renderer = FakeTextRenderer()

        class Event:
            message_obj = SimpleNamespace(message_id=4101)

            def __init__(self):
                self.sent = []

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "用户"

            @staticmethod
            def chain_result(chain):
                return chain

            @staticmethod
            def plain_result(text):
                return [Plain(text)]

            async def send(self, result):
                self.sent.append(result)
                first_sent.set()

        async def controlled_download(_self, content):
            if isinstance(content, PluginVideoContent):
                video_started.set()
                if first_ready == "card":
                    await release_video.wait()
            return content, await content.get_path(), None

        event = Event()
        result = ParseResult(
            platform=Platform(name="bilibili", display_name="B站"),
            title="视频标题",
            text="视频简介",
            contents=[PluginVideoContent(video_path)],
            extra={
                "render_text_card": True,
                "video_separate_from_card": True,
            },
        )
        with patch.object(ParserXPlugin, "_download_content", controlled_download):
            send_task = asyncio.create_task(plugin._send_parse_result(event, result))
            await asyncio.wait_for(card_started.wait(), timeout=1)
            await asyncio.wait_for(video_started.wait(), timeout=1)
            await asyncio.wait_for(first_sent.wait(), timeout=1)

            if first_ready == "video":
                assert isinstance(event.sent[0][0], MessageVideo)
                release_card.set()
            else:
                assert isinstance(event.sent[0][0], Reply)
                assert isinstance(event.sent[0][1], MessageImage)
                release_video.set()

            await asyncio.wait_for(send_task, timeout=1)
        return event

    event = asyncio.run(run())
    assert len(event.sent) == 2


@pytest.mark.parametrize("first_ready", ["video", "card"])
def test_delivery_video_and_card_send_in_completion_order(tmp_path, first_ready):
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch,
        DeliveryPlan,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "delivery-video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    async def run():
        card_started = asyncio.Event()
        video_started = asyncio.Event()
        release_card = asyncio.Event()
        release_video = asyncio.Event()
        first_sent = asyncio.Event()

        class FakeTextRenderer:
            async def render_text_card(self, out_path, **_kwargs):
                card_started.set()
                if first_ready == "video":
                    await release_card.wait()
                _save_render_fixture(out_path, size=(760, 320))

        plugin.text_renderer = FakeTextRenderer()

        class Event:
            message_obj = SimpleNamespace(message_id=4102)

            def __init__(self):
                self.sent = []

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "用户"

            @staticmethod
            def chain_result(chain):
                return chain

            @staticmethod
            def plain_result(text):
                return [Plain(text)]

            async def send(self, result):
                self.sent.append(result)
                first_sent.set()

        async def controlled_download(_self, content):
            if isinstance(content, PluginVideoContent):
                video_started.set()
                if first_ready == "card":
                    await release_video.wait()
            return content, await content.get_path(), None

        video = PluginVideoContent(video_path)
        result = ParseResult(
            platform=Platform(name="miyoushe", display_name="米游社"),
            title="文章标题",
            text="文章正文",
            contents=[video],
            delivery=DeliveryPlan(
                [
                    DeliveryBatch(["识别：米游社\n文章正文"]),
                    DeliveryBatch([video]),
                ]
            ),
            extra={"render_text_card": True},
        )
        event = Event()
        with patch.object(ParserXPlugin, "_download_content", controlled_download):
            send_task = asyncio.create_task(plugin._send_parse_result(event, result))
            await asyncio.wait_for(card_started.wait(), timeout=1)
            await asyncio.wait_for(video_started.wait(), timeout=1)
            await asyncio.wait_for(first_sent.wait(), timeout=1)

            if first_ready == "video":
                assert isinstance(event.sent[0][0], MessageVideo)
                release_card.set()
            else:
                assert isinstance(event.sent[0][0], Reply)
                assert isinstance(event.sent[0][1], MessageImage)
                release_video.set()

            await asyncio.wait_for(send_task, timeout=1)
        return event

    event = asyncio.run(run())
    assert len(event.sent) == 2


def test_media_card_failure_falls_back_to_plain_body(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        Author,
        ParseResult,
        Platform,
        VideoContent,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FailingTextRenderer:
        async def render_text_card(self, *_args, **_kwargs):
            raise RuntimeError("renderer unavailable")

    plugin.text_renderer = FailingTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2468)

        def __init__(self):
            self.sent = []

        @staticmethod
        def get_sender_id():
            return "42"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(text):
            return [Plain(text)]

        async def send(self, result):
            self.sent.append(result)

    event = Event()
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        author=Author(name="作者"),
        text="渲染失败也必须发送正文",
        contents=[VideoContent(video_path)],
        url="https://weibo.com/example",
        extra={"render_text_card": True},
    )

    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 2
    assert isinstance(event.sent[0][0], Plain)
    assert "渲染失败也必须发送正文" in event.sent[0][0].text
    assert isinstance(event.sent[1][0], MessageVideo)


def test_plugin_initializes_and_registers_aiocqhttp_parsers(tmp_path):
    from astrbot_plugin_parser_x.main import ParserXPlugin

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
    config["bili_comment"] = "false"
    config["platforms"] = {"tieba": True}
    config["integrations"] = {"tieba_api_base": "http://example.invalid/api"}

    async def run_lifecycle():
        with patch.object(StarTools, "get_data_dir", return_value=tmp_path / "data"):
            plugin = ParserXPlugin(Context(), config)
            await plugin.initialize()
            try:
                assert "b23.tv" in plugin.parser_map
                assert "miyoushe.com" in plugin.parser_map
                assert "tieba.baidu.com" not in plugin.parser_map
                assert "y.qq.com" not in plugin.parser_map
                assert "kugou.com" not in plugin.parser_map
                assert "qishui.douyin.com" not in plugin.parser_map
                assert "channels.weixin.qq.com" not in plugin.parser_map
                assert "tiktok.com" not in plugin.parser_map
                assert "youtube.com" not in plugin.parser_map
                assert plugin.key_pattern_list
                assert plugin.config["comment_settings_migrated"] is True
                assert plugin.config["config_v2_migrated"] is True
                assert plugin.config["comments"]["bilibili"] is False
                assert plugin.config["behavior"]["show_download_fail_tip"] is True
                assert plugin.config["behavior"]["disabled_sessions"] == []
                assert "tieba" not in plugin.config["platforms"]
                assert "integrations" not in plugin.config
                assert plugin.parser_map["b23.tv"].enable_comment_card is False
                assert plugin.render_service.available
                bili = plugin.parser_map["b23.tv"]
                douyin = plugin.parser_map["douyin"]
                weibo = plugin.parser_map["weibo.com"]
                miyoushe = plugin.parser_map["miyoushe.com"]
                xiaoheihe = plugin.parser_map["xiaoheihe.cn"]
                assert bili.render_service is plugin.render_service
                assert bili.comment_canvas.render_service is plugin.render_service
                assert not hasattr(bili, "dynamic_renderer")
                assert douyin.render_service is plugin.render_service
                assert douyin.comment_canvas.render_service is plugin.render_service
                assert weibo.render_service is plugin.render_service
                assert weibo.comment_canvas.render_service is plugin.render_service
                assert miyoushe.render_service is plugin.render_service
                assert miyoushe.comment_canvas.render_service is plugin.render_service
                assert xiaoheihe.render_service is plugin.render_service
                assert xiaoheihe.comment_canvas.render_service is plugin.render_service
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
