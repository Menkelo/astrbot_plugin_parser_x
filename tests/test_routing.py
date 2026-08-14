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
from astrbot.api.message_components import Node, Plain, Reply
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
from core.data import ImageContent
from core.download import Downloader, VideoInfo
from core.html_renderer import HtmlRenderService
from core.live_renderer import LiveCardRenderer
from core.parsers import (
    BaseParser,
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
from core.parsers.weibo_comment import WeiboCommentFeed
from core.parsers.ytdlp import AcFunParser
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


def test_runtime_dependencies_do_not_require_local_browser():
    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "playwright" not in requirements.lower()
    source_files = [
        Path(__file__).parents[1] / "core" / "text_renderer.py",
        Path(__file__).parents[1] / "core" / "live_renderer.py",
        Path(__file__).parents[1] / "core" / "comment_canvas.py",
        Path(__file__).parents[1]
        / "core"
        / "parsers"
        / "bilibili"
        / "comment_canvas.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    assert "playwright" not in source.lower()
    assert "chromium" not in source.lower()


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
                "desc": "小红书正文",
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
    for result in (kuaishou_result, douyin_result, xhs_result):
        assert result.contents
        assert result.extra["render_text_card"] is True


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
    assert calls == [share_url]
    assert result.title == "Demo"
    assert result.text == "Body"
    assert result.extra["render_text_card"] is True


def test_xiaoheihe_api_uses_link_text_and_shared_card(tmp_path):
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
                                        "<p>正文内容</p>"
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
                    }
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
    assert result.text == "帖子简介\n\n正文内容"
    assert len(result.contents) == 2
    assert result.author is not None and result.author.name == "作者"
    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_avatar"].endswith("avatar.jpg")


def test_xiaoheihe_game_uses_public_api_without_cookie(tmp_path):
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
    assert calls == [
        ("https://api.xiaoheihe.cn/game/web/get_game_detail/", {"appid": "730"})
    ]
    assert result.title == "CS2"
    assert "游戏简介" in (result.text or "")
    assert "Valve" in (result.text or "")
    assert len(result.contents) == 1
    assert result.extra["render_text_card"] is True


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


def test_text_and_live_cards_share_html_render_service(tmp_path):
    calls = []

    async def fake_html_render(template, data, *, return_url, options):
        calls.append((template, data, return_url, options))
        rendered = tmp_path / f"rendered-{len(calls)}.png"
        _save_render_fixture(rendered)
        return str(rendered)

    service = HtmlRenderService(fake_html_render)
    text_output = tmp_path / "text.png"
    live_output = tmp_path / "live.png"

    async def run():
        await TextCardRenderer(service).render_text_card(
            text_output,
            platform_name="微博",
            author_name="用户",
            text="正文 #话题#",
        )
        await LiveCardRenderer(service).render_live_card(
            live_output,
            platform_name="Bilibili",
            title="直播标题",
            streamer_name="主播",
            cover=None,
            avatar=None,
            status_text="直播中",
            area_text="游戏",
        )

    asyncio.run(run())
    with Image.open(text_output) as image:
        assert image.size == (760, 177)
    with Image.open(live_output) as image:
        assert image.size == (748, 175)
    assert len(calls) == 2
    assert all(return_url is False for _, _, return_url, _ in calls)
    assert all(options["scale"] == "css" for _, _, _, options in calls)
    assert all(options["timeout"] == 45_000 for _, _, _, options in calls)
    assert all(COMMENT_FOOTER_BRAND in template for template, *_ in calls)


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
        assert '<svg class="reply-icon"' in html
        assert "◌" not in html


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


def test_bilibili_single_image_dynamic_skips_summary_card(tmp_path):
    card = tmp_path / "card.png"
    first = ImageContent(tmp_path / "first.jpg")
    second = ImageContent(tmp_path / "second.jpg")

    single_contents, single = BiliDynamicService._select_delivery_contents(
        card,
        [first],
    )
    multi_contents, multi = BiliDynamicService._select_delivery_contents(
        card,
        [first, second],
    )

    assert single is True
    assert single_contents == [first]
    assert multi is False
    assert isinstance(multi_contents[0], ImageContent)
    assert multi_contents[0].path_task == card
    assert multi_contents[1:] == [first, second]


def test_bilibili_single_image_dynamic_does_not_render_summary_card(tmp_path):
    class Renderer:
        async def render_dynamic_card(self, **_kwargs):
            raise AssertionError("single image should not render a summary card")

    parser = SimpleNamespace(cache_dir=tmp_path, dynamic_renderer=Renderer())
    service = BiliDynamicService(parser)
    image = ImageContent(tmp_path / "first.jpg")

    contents, single = asyncio.run(
        service._build_delivery_contents(
            dynamic_id=123,
            author_name="作者",
            author_avatar=None,
            dynamic_title="标题",
            full_text="正文",
            time_text=None,
            image_urls=["https://i0.hdslb.com/first.jpg"],
            full_images=[image],
        )
    )

    assert single is True
    assert contents == [image]


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


def test_weibo_media_result_preserves_body_for_shared_text_card(tmp_path):
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
                    "text": "<p>微博正文</p>",
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

        async def fake_avatar(_url, max_bytes=0):
            return "data:image/jpeg;base64,YXZhdGFy"

        parser._img_to_data_uri = fake_avatar
        try:
            keyword, searched = parser.search_url(
                "https://weibo.com/1088413295/IpOAqcs7h"
            )
            return await parser.parse(keyword, searched)
        finally:
            await parser.close_session()

    result = asyncio.run(run())
    assert result.text == "微博正文"
    assert len(result.contents) == 1
    assert result.extra["render_text_card"] is True
    assert result.extra["text_card_avatar"].startswith("data:image/jpeg;base64,")


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

        results = await asyncio.gather(
            bili_feed.build_images(2, 1, video_title="标题", video_cover=""),
            douyin_feed.build_images("3", work_title="标题", cover=""),
            weibo_feed.build_images("4", work_title="标题", cover="", owner_id=""),
        )
        await asyncio.gather(
            *(content.get_path() for contents in results for content in contents)
        )
        return bili_canvas, douyin_canvas, weibo_canvas

    canvases = asyncio.run(run())
    for canvas in canvases:
        assert COMMENT_FOOTER_BRAND in canvas.documents[0].footer_text
        assert "Parser X" not in canvas.documents[0].footer_text


def test_comment_layout_cache_versions_invalidate_pre_fix_images():
    assert BiliCommentFeed.CACHE_VERSION == "bili_comment_v8_official_render_crop"
    assert DouyinCommentFeed.CACHE_VERSION == "douyin_comment_v7_official_render_crop"
    assert WeiboCommentFeed.CACHE_VERSION == "weibo_comment_v7_official_render_crop"


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
    from astrbot_plugin_parser_x.core.data import ImageContent as PluginImageContent
    from astrbot_plugin_parser_x.main import ParserXPlugin

    image_path = tmp_path / "single.jpg"
    image_path.write_bytes(b"image")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"show_download_fail_tip": True}
    plugin._background_tasks = set()

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
    result = SimpleNamespace(
        contents=[content],
        comment_contents=[],
        extra={"reply_original_for_single_image": True},
        platform=SimpleNamespace(display_name="测试"),
        text="",
        extra_info=None,
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
    assert isinstance(event.sent[0][1], MessageImage)


def test_single_image_shared_card_keeps_body_in_direct_reply(tmp_path):
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
    plugin._background_tasks = set()

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
        extra={"render_text_card": True},
    )

    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Reply)
    assert event.sent[0][0].id == 1357
    assert len(event.sent[0]) == 3
    assert isinstance(event.sent[0][1], MessageImage)
    assert isinstance(event.sent[0][2], MessageImage)


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
    plugin._background_tasks = set()

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
                assert bili.render_service is plugin.render_service
                assert bili.comment_canvas.render_service is plugin.render_service
                assert bili.live_renderer.render_service is plugin.render_service
                assert bili.dynamic_renderer.render_service is plugin.render_service
                assert douyin.render_service is plugin.render_service
                assert douyin.comment_canvas.render_service is plugin.render_service
                assert weibo.render_service is plugin.render_service
                assert weibo.comment_canvas.render_service is plugin.render_service
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())
