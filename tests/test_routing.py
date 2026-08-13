from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from core.comment_canvas import (
    DOUYIN_THEME,
    CommentAuthor,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from core.comment_settings import CommentSettings
from core.download import Downloader, VideoInfo
from core.parsers import (
    BaseParser,
    DouyinParser,
    MiyousheParser,
    TiebaParser,
    WeiboParser,
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
from core.parsers.bilibili.dynamic_service import BiliDynamicService
from core.parsers.douyin.a_bogus import _sm3_fallback, generate_a_bogus
from core.parsers.douyin.comment_feed import DouyinCommentFeed
from core.parsers.weibo_comment import WeiboCommentFeed
from core.parsers.ytdlp import AcFunParser
from core.utils import extract_json_url, generate_file_name


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
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" in calls["template"]
    assert "#parser-x-comment-root{transform:scale(1.5)" in calls["template"]
    assert "Parser X" in calls["template"]


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
        rendered.write_bytes(b"social-canvas")
        return str(rendered)

    output = tmp_path / "social-comments.jpg"
    renderer = SocialCommentCanvas(canvas_render=fake_canvas)
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

    assert output.read_bytes() == b"social-canvas"
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" in calls["template"]
    assert "#parser-x-comment-root{transform:scale(1.5)" in calls["template"]


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
        output.write_bytes(b"social-canvas")
        return str(output)

    renderer = SocialCommentCanvas(fake_canvas)
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

    assert output.read_bytes() == b"social-canvas"
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
            "sticker": {
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
                "desc": "作品",
                "create_time": 1700000000,
                "author": {"uid": "42", "nickname": "作者"},
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
    assert callable(douyin_extra["comment_task_factory"])
    assert douyin_extra["comment_timeout"] == 45
    assert callable(weibo_extra["comment_task_factory"])
    assert weibo_extra["comment_timeout"] == 45


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


def test_comment_layout_cache_versions_invalidate_pre_fix_images():
    assert BiliCommentFeed.CACHE_VERSION == "bili_comment_v4_layout"
    assert DouyinCommentFeed.CACHE_VERSION == "douyin_comment_v3_layout"
    assert WeiboCommentFeed.CACHE_VERSION == "weibo_comment_v3_layout"


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
    config["bili_comment"] = "false"

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
                assert plugin.config["comment_settings_migrated"] is True
                assert plugin.config["comments"]["bilibili"] is False
                assert plugin.parser_map["b23.tv"].enable_comment_card is False
                assert plugin.parser_map["b23.tv"].comment_canvas._canvas_render
                assert plugin.parser_map["douyin"].comment_canvas._canvas_render
                assert plugin.parser_map["weibo.com"].comment_canvas._canvas_render
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
