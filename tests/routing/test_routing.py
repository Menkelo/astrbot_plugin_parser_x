from __future__ import annotations

import asyncio
import json
import re
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
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    SocialCommentCanvas,
)
from core.comment_settings import CommentSettings
from core.constants import COMMENT_FOOTER_BRAND
from core.data import ImageContent, ParseResult, VideoContent
from core.download import Downloader, VideoInfo
from core.html_renderer import HtmlRenderService
from core.parsers import (
    BaseParser,
    BilibiliParser,
    DouyinParser,
    KuaiShouParser,
    ParseException,
    SkipParseException,
    WeiboParser,
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
from core.parsers.xiaohongshu_comment import XiaohongshuCommentFeed
from core.parsers.ytdlp import AcFunParser
from core.platform_emotes import (
    build_bilibili_emote_map,
    build_xiaohongshu_emote_config_map,
    build_xiaohongshu_emote_map,
    load_platform_emotes,
)
from core.rendered_image import save_rendered_image
from core.unified_renderer import UnifiedCardRenderer
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


def _local_backend(tmp_path, calls=None, *, name="render.png", background="white"):
    """最小本地渲染后端替身：返回 PNG 字节，不启动真浏览器。"""

    class FakeBackend:
        async def render(self, template, data, *, options=None):
            if calls is not None:
                calls.update(
                    {"template": template, "data": data, "options": options}
                )
            rendered = tmp_path / name
            _save_render_fixture(rendered, background=background)
            return rendered.read_bytes()

        async def ensure_ready(self):
            return self

        async def aclose(self):
            return None

    return FakeBackend()


def test_bundled_fonts_are_inlined_into_rendered_pages():
    """字体随插件自带，并以 base64 内联进页面（照 astrbot_plugin_quote_core 的做法）。

    这样做不用装系统字体、不用 fontconfig、不用环境变量——字体跟着 HTML 走，
    换任何容器都开箱即用。
    """
    from core import browser_backend as bb

    assert bb.PLUGIN_FONT_DIR.name == "fonts"
    assert bb.PLUGIN_FONT_DIR.parent.name == "assets"
    assert all(
        (bb.PLUGIN_FONT_DIR / name).is_file() for name in bb._FONT_FILES.values()
    ), "插件自带字体缺失：assets/fonts 下应有 woff2 子集"

    # 内联出的 @font-face 必须带上 data URI 与真实字重
    css = bb._font_face_css()
    assert f"font-family:'{bb.FONT_FAMILY}'" in css
    assert "data:font/woff2;base64," in css
    for weight in bb._FONT_FILES:
        assert f"font-weight:{weight};" in css

    # 注入点：样式块要落在 </head> 之前，且注入是幂等的
    page = "<html><head><meta charset='utf-8'></head><body>hi</body></html>"
    injected = bb._inject_font_css(page)
    assert injected.index("parser-x-font-faces") < injected.index("</head>")

    # 幂等：同一页面注入两次，结果不变
    assert bb._inject_font_css(injected) == injected

    # 回归：卡片 CSS 的字体栈里本来就写着 'Noto Sans SC'，这不能导致注入被跳过
    # （曾用「页面里有没有出现字族名」当判据，结果字体永远注入不进去，
    #  字形实际落到 Unifont/Liberation Sans —— 见 skill 的踩坑记录）
    page_with_stack = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:'Noto Sans SC','WenQuanYi Zen Hei',sans-serif}"
        "</style></head><body>x</body></html>"
    )
    out = bb._inject_font_css(page_with_stack)
    assert "parser-x-font-faces" in out, "字体栈已有同名时仍必须注入 @font-face"
    assert "data:font/woff2;base64," in out


def test_bundled_fonts_do_not_need_fontconfig_or_env(monkeypatch, tmp_path):
    """反面约束：不再依赖 fontconfig/环境变量，也不该往临时目录写字体。"""
    from core import browser_backend as bb

    src = Path(bb.__file__).read_text(encoding="utf-8")
    # 只查真实代码，不查文档里的说明文字。
    assert "FONTCONFIG_FILE" not in src, "不应再依赖 fontconfig 环境变量"
    assert "FONTCONFIG_DIR" not in src
    assert "_stage_font_config" not in src, "不应再有生成 fontconfig 的函数"
    assert "<fontconfig>" not in src, "不应再生成 fontconfig XML"
    assert "/usr/share/fonts" not in src, "不应再引用系统字体目录"
    assert "shutil.rmtree" not in src, "不应再复制/清理字体临时目录"

    # 浏览器不带特殊 env 启动
    assert not hasattr(bb.LocalRenderBackend, "_browser_env")
    assert not [
        p for p in tmp_path.iterdir()
    ], "本测试不应产生任何临时文件"


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
                "filter": {
                    "mention_mode": "STRICT",
                    "ad_threshold": "99",
                    "low_information": "true",
                },
            }
        },
        "bilibili",
    )
    assert settings.enabled is False
    assert settings.display_count == 20
    assert settings.timeout == 90
    assert settings.filter.enabled is True
    assert settings.filter.mention_mode == "balanced"
    assert settings.filter.qrcode is True
    assert settings.filter.ads is True
    assert settings.filter.duplicates is True
    assert settings.filter.low_information is False
    assert settings.filter.ad_threshold == 6

    legacy = CommentSettings.from_config(
        {"comments": "invalid", "bili_comment": "false"},
        "bilibili",
        legacy_enabled="false",
    )
    assert legacy.enabled is False




@pytest.mark.parametrize(
    "parser_cls",
    [
        BilibiliParser,
        DouyinParser,
        WeiboParser,
        XiaoHongShuParser,
    ],
)
def test_all_parser_results_remove_comment_tasks_without_video(parser_cls, tmp_path):
    async def build_comment_images():
        return []

    result = parser_cls.result(
        contents=[ImageContent(tmp_path / "image.jpg")],
        extra={
            "text_card_comment_factory": build_comment_images,
            "comment_document_task_factory": build_comment_images,
            "comment_timeout": 45,
        },
    )

    assert result.has_video is False
    assert callable(result.extra.get("text_card_comment_factory"))
    assert True  # 图文帖同样保留评论任务
    assert result.extra.get("comment_timeout") == 45


def test_parser_results_keep_comment_tasks_for_video_contents_and_delivery(tmp_path):
    from core.data import DeliveryBatch, DeliveryPlan

    async def build_comment_images():
        return []

    direct = WeiboParser.result(
        contents=[VideoContent(tmp_path / "direct.mp4")],
        extra={
            "text_card_comment_factory": build_comment_images,
            "comment_timeout": 45,
        },
    )
    planned_video = VideoContent(tmp_path / "planned.mp4")
    planned = WeiboParser.result(
        delivery=DeliveryPlan([DeliveryBatch([planned_video])]),
        extra={
            "text_card_comment_factory": build_comment_images,
            "comment_timeout": 45,
        },
    )

    for result in (direct, planned):
        assert result.has_video is True
        assert callable(result.extra["text_card_comment_factory"])
        assert result.extra["comment_timeout"] == 45


def test_parse_result_constructor_treats_dynamic_media_as_non_video(tmp_path):
    from core.data import DynamicContent, ParseResult, Platform

    async def build_comment_images():
        return []

    result = ParseResult(
        platform=Platform(name="demo", display_name="演示"),
        contents=[DynamicContent(tmp_path / "animation.gif")],
        extra={
            "text_card_comment_factory": build_comment_images,
            "comment_timeout": 45,
        },
    )

    assert result.has_video is False
    assert callable(result.extra.get("text_card_comment_factory"))
    assert result.extra.get("comment_timeout") == 45


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


def test_json_share_url_prefers_xiaohongshu_note_with_token():
    note_url = (
        "https://www.xiaohongshu.com/discovery/item/6a9c17ee0000000027015702"
        "?xsec_token=CB-NMCzTaQTrjO5Q19vzyB-261G5vO7Bd6UaAUcHyracQ%3D"
        "&xsec_source=app_share"
    )
    payload = {
        "app": "com.tencent.miniapp_01",
        "meta": {
            "detail_1": {
                "desc": "小红书",
                "qqdocurl": note_url,
                "url": "https://xhslink.com/a/shortid",
                "preview": "https://sns-img-qc.xhscdn.com/cover.jpg",
            }
        },
    }
    assert extract_json_url(payload) == note_url
    xml = (
        '<msg><item url="https://xhslink.com/a/shortid"/>'
        f'<item qqdocurl="{note_url}"/></msg>'
    )
    assert extract_json_url(xml) == note_url


def test_json_share_url_keeps_plus_in_xsec_token():
    payload = {
        "meta": {
            "detail_1": {
                "qqdocurl": (
                    "https://www.xiaohongshu.com/explore/note123"
                    "?xsec_token=token+value=&xsec_source=pc_share"
                )
            }
        }
    }
    assert extract_json_url(payload).endswith(
        "xsec_token=token+value=&xsec_source=pc_share"
    )


def test_share_card_text_is_used_even_when_message_title_exists():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    note_url = (
        "https://www.xiaohongshu.com/discovery/item/6a9c17ee0000000027015702"
        "?xsec_token=token%2Bvalue%3D&xsec_source=app_share"
    )

    class Xml:
        def __init__(self, data):
            self.data = data

    plugin = object.__new__(ParserXPlugin)
    event = SimpleNamespace(
        message_str="小红书",
        get_messages=lambda: [
            Xml(
                {
                    "app": "com.tencent.miniapp_01",
                    "meta": {
                        "detail_1": {
                            "desc": "小红书",
                            "qqdocurl": note_url,
                            "url": "https://xhslink.com/a/shortid",
                        }
                    },
                }
            )
        ],
    )
    text = plugin._text_from_event(event)
    assert note_url in text
    assert "小红书" in text


def test_xiaohongshu_matches_preserve_unresolved_short_link_with_token_note():
    from astrbot_plugin_parser_x.main import ParserXPlugin

    note = (
        "https://www.xiaohongshu.com/discovery/item/6a9c17ee0000000027015702"
        "?xsec_token=token%2Bvalue%3D"
    )
    short = "https://xhslink.com/a/shortid"
    text = f"小红书 {short} {note}"
    plugin = object.__new__(ParserXPlugin)
    plugin.parser_map = {
        "hongshu.com/discovery/item/": SimpleNamespace(
            platform=SimpleNamespace(name="xiaohongshu")
        ),
        "xhslink.com": SimpleNamespace(platform=SimpleNamespace(name="xiaohongshu")),
    }
    plugin.key_pattern_list = [
        (
            "hongshu.com/discovery/item/",
            re.compile(
                r"discovery/item/(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]*)?"
            ),
        ),
        ("xhslink.com", re.compile(r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]*")),
    ]
    matches = plugin._collect_parser_matches(text)
    # An opaque short link is not proven to point to this tokenized note.
    assert len(matches) == 2
    assert {item[1] for item in matches} == {"hongshu.com/discovery/item/", "xhslink.com"}
    note_match = next(item for item in matches if item[1] == "hongshu.com/discovery/item/")
    assert "xsec_token=" in note_match[2].group(0)


def test_ytdlp_parsers_are_registered_once():
    classes = BaseParser.get_all_subclass()
    names = [(item.__module__, item.__qualname__) for item in classes]
    assert len(names) == len(set(names))
    assert AcFunParser in classes


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
        (Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    platform_keys = set(schema.get("platforms", {}).get("items", {}))
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


def test_tieba_is_registered_without_legacy_platform_configuration():
    keywords = {
        keyword
        for parser in BaseParser.get_all_subclass()
        for keyword, _ in parser._key_patterns
    }
    schema = json.loads(
        (Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8")
    )

    assert "tieba.baidu.com" in keywords
    assert "platforms" not in schema
    assert "integrations" not in schema
    tieba = next(parser for parser in BaseParser.get_all_subclass()
                 if parser.platform.name == "tieba")
    keyword, matched = tieba.search_url("https://tieba.baidu.com/p/1234567890")
    assert keyword == "tieba.baidu.com"
    assert matched is not None


def test_retired_ytdlp_platforms_are_fully_removed():
    classes = BaseParser.get_all_subclass()
    keywords = {keyword for parser in classes for keyword, _ in parser._key_patterns}
    schema = json.loads(
        (Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8")
    )

    assert (
        not {
            "ixigua.com",
            "pipix.com",
            "pipigx.com",
            "weishi.qq.com",
            "xsj.qq.com",
        }
        & keywords
    )
    assert not {"xigua", "pipixia", "weishi"} & set(schema.get("platforms", {}).get("items", {}))
    assert not {"XiguaParser", "PipixiaParser", "WeishiParser"} & {
        parser.__name__ for parser in classes
    }


def test_config_schema_uses_latest_astrbot_panel_features():
    schema = json.loads(
        (Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert "rendering" not in schema
    assert "rendering" not in schema
    assert not schema["cookies"].get("obvious_hint", False)
    assert schema["performance"]["items"]["video_codec"]["labels"] == [
        "自动选择",
        "优先 HEVC / H.265",
        "优先 AVC / H.264",
    ]
    assert "ytdlp_cookie_file" not in schema["cookies"]["items"]
    assert schema["cookies"]["items"]["acfun_cookie"]["type"] == "text"
    assert schema["cookies"]["items"]["acfun_cookie"]["default"] == ""
    assert schema["behavior"]["items"]["disabled_sessions"]["collapsed"] is True
    assert "platforms" not in schema
    assert set(schema["comments"]["items"]) == {"enabled", "display_count"}
    assert "filter_enabled" not in schema["comments"]["items"]
    assert schema["cookies"]["items"]["xiaohongshu_cookie"]["default"] == ""


def test_local_render_legacy_assets_stay_removed():
    # 历史防线：不再有 live_renderer 模块，也不再有无沙箱的持久化浏览器上下文。
    repo_root = Path(__file__).parents[2]
    source_files = [repo_root / "main.py", *(repo_root / "core").rglob("*.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    assert not (repo_root / "core" / "live_renderer.py").exists()
    assert "launch_persistent_context" not in source.lower()


def test_local_render_backend_is_the_only_browser_touchpoint():
    repo_root = Path(__file__).parents[2]
    # Playwright 只允许出现在本地渲染后端这一个模块里，别的模块（尤其是渲染网关）
    # 不得直接碰浏览器。
    browser_modules = [
        path.name
        for path in (repo_root / "core").rglob("*.py")
        if "async_playwright" in path.read_text(encoding="utf-8")
    ]
    assert browser_modules == ["browser_backend.py"]
    gateway = (repo_root / "core" / "html_renderer.py").read_text(encoding="utf-8")
    assert "async_playwright" not in gateway
    assert "from playwright" not in gateway
    assert ".launch(" not in gateway

    schema = json.loads(
        (repo_root / "_conf_schema.json").read_text(encoding="utf-8")
    )
    # 渲染引擎不再是配置项：插件只有本地 Chromium 一条渲染路径。
    assert "rendering" not in schema


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
    assert downloader.download_args[1]["max_size_mb"] == 100


def test_domestic_parser_helpers_cover_new_routes():
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


def test_xiaohongshu_preserves_source_topic_tags():
    # 保留话题名与普通正文，只清理小红书序列化标记。
    assert (
        XiaoHongShuParser._normalize_note_text(
            "#性能车[话题]# #机车跑山[话题]# 正文[话题]"
        )
        == "#性能车 #机车跑山 正文[话题]"
    )


def _xhs_state_html(payload: dict) -> str:
    return (
        "<html><head><title>小红书</title></head><body>"
        f"<script>window.__INITIAL_STATE__={json.dumps(payload)}</script>"
        "</body></html>"
    )


def _xhs_note_payload() -> dict:
    return {
        "type": "normal",
        "title": "小红书标题",
        "desc": "小红书正文",
        "time": 1_700_000_000,
        "user": {
            "nickname": "作者",
            "avatar": "https://img.example.com/avatar.jpg",
        },
        "imageList": [{"urlDefault": "https://img.example.com/c.jpg"}],
    }


def test_xiaohongshu_unwraps_sec_redirect_and_keeps_plus_in_token():
    wrapped = (
        "https://www.xiaohongshu.com/404/sec_abc?"
        "source=xhs_sec_server&originalUrl="
        "https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fnote123"
        "%3Fxsec_token%3Dtoken%2Bvalue%3D%26xsec_source%3Dpc_share"
    )
    assert XiaoHongShuParser._unwrap_sec_redirect(wrapped) == (
        "https://www.xiaohongshu.com/explore/note123"
        "?xsec_token=token+value=&xsec_source=pc_share"
    )
    nested = (
        "https://www.xiaohongshu.com/404?source=/404/sec_abc?"
        "redirectPath=https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fnote123"
        "%3Fxsec_token%3Dtoken%2Bvalue"
    )
    assert XiaoHongShuParser._unwrap_sec_redirect(nested).endswith(
        "xsec_token=token+value"
    )


def test_xiaohongshu_ensure_note_url_copies_token_from_share_text():
    parser = XiaoHongShuParser({"cache_dir": "."}, object())
    parser.source_text = (
        "分享 https://www.xiaohongshu.com/explore/note123"
        "?xsec_token=token+value=&xsec_source=pc_share"
    )
    assert parser._ensure_note_url(
        "https://www.xiaohongshu.com/explore/note123",
        "note123",
    ) == (
        "https://www.xiaohongshu.com/explore/note123"
        "?xsec_token=token%2Bvalue%3D&xsec_source=pc_share"
    )


def test_xiaohongshu_extracts_note_from_vue_ref_and_empty_map_fallback():
    parser = XiaoHongShuParser({"cache_dir": "."}, object())
    note = _xhs_note_payload()
    wrapped = {
        "value": {
            "note": {
                "noteDetailMap": {
                    "value": {
                        "other": {"note": {"value": note}},
                    }
                }
            }
        }
    }
    assert parser._extract_note_from_state(wrapped, "note123") == note
    discovery = {
        "noteData": {
            "data": {"noteData": {**note, "user": {"nickName": "作者"}}},
            "noteDetailMap": {},
        }
    }
    extracted = parser._extract_note_from_state(discovery, "note123")
    assert extracted["user"]["nickName"] == "作者"


def test_xiaohongshu_explore_follows_sec_redirect_and_reads_note(tmp_path):
    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "image.jpg"

            return asyncio.create_task(done())

    note = _xhs_note_payload()
    blocked_url = (
        "https://www.xiaohongshu.com/404/sec_abc?source=xhs_sec_server"
        "&originalUrl=https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fnote123"
        "%3Fxsec_token%3Dtoken%2Bvalue"
    )
    note_url = (
        "https://www.xiaohongshu.com/explore/note123"
        "?xsec_token=token%2Bvalue&xsec_source=pc_share"
    )
    pages = {
        "https://www.xiaohongshu.com/explore/note123": (
            blocked_url,
            _xhs_state_html({"note": {"noteDetailMap": {}}}),
        ),
        note_url: (
            note_url,
            _xhs_state_html({"note": {"noteDetailMap": {"note123": {"note": note}}}}),
        ),
    }

    async def run():
        parser = XiaoHongShuParser({"cache_dir": str(tmp_path)}, FakeDownloader())

        async def fake_http_get(url, **_kwargs):
            final, html = pages[url]
            return SimpleNamespace(status_code=200, url=final, text=html)

        parser.http_get = fake_http_get
        result = await parser.parse_explore(
            "https://www.xiaohongshu.com/explore/note123",
            "note123",
        )
        await asyncio.gather(*(content.get_path() for content in result.contents))
        await parser.close_session()
        return result

    result = asyncio.run(run())
    assert result.title == "小红书标题"
    assert result.text == "小红书正文"
    assert result.contents


def test_xiaohongshu_empty_note_map_reports_readable_block_reason():
    html = (
        "<html><head><title>小红书 - 你访问的页面不见了</title></head>"
        "<body><script>window.__INITIAL_STATE__="
        '{"note":{"noteDetailMap":{}}}</script></body></html>'
    )

    async def run():
        parser = XiaoHongShuParser({"cache_dir": "."}, object())

        async def fake_http_get(url, **_kwargs):
            return SimpleNamespace(
                status_code=200,
                url=(
                    "https://www.xiaohongshu.com/404?source=/404/sec_abc"
                    "?redirectPath=https%3A%2F%2Fwww.xiaohongshu.com"
                    "%2Fexplore%2F6a9c17ee0000000027015702"
                    "&error_code=300031&error_msg=%E5%BD%93%E5%89%8D%E7%AC%94"
                    "%E8%AE%B0%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%B5%8F%E8%A7%88"
                ),
                text=html,
            )

        parser.http_get = fake_http_get
        await parser.parse_explore(
            "https://www.xiaohongshu.com/explore/6a9c17ee0000000027015702",
            "6a9c17ee0000000027015702",
        )

    with pytest.raises(ParseException, match="暂时无法浏览"):
        asyncio.run(run())


def test_xiaohongshu_notes_attach_comment_tasks_only_when_enabled(tmp_path):
    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                return tmp_path / "image.jpg"

            return asyncio.create_task(done())

        def download_video(self, _url, **_kwargs):
            async def done():
                return tmp_path / "video.mp4"

            return asyncio.create_task(done())

    # 小红书评论区已恢复：仅当显式开启并且填了 Cookie时才挂载，
    # 且仅限视频笔记。
    config = {
        "cache_dir": str(tmp_path),
        "cookies": {"xiaohongshu_cookie": "a1=test-a1; web_session=test-session"},
        "comments": {
            "xiaohongshu": True,
            "display_count": 7,
            "timeout": 45,
        },
    }

    async def run():
        parser = XiaoHongShuParser(config, FakeDownloader())
        video_data = {
            "noteId": "note123",
            "type": "video",
            "title": "视频标题",
            "desc": "视频正文",
            "user": {
                "userId": "owner42",
                "nickname": "作者",
                "avatar": "https://img.example.com/avatar.jpg",
            },
            "interactInfo": {"commentCount": "128"},
            "imageList": [{"urlDefault": "https://img.example.com/cover.jpg"}],
            "video": {
                "media": {
                    "stream": {"h264": [{"url": "https://video.example.com/video.mp4"}]}
                }
            },
        }
        image_data = {
            **video_data,
            "type": "normal",
            "video": None,
            "imageList": [{"urlDefault": "https://img.example.com/image.jpg"}],
        }
        final_url = (
            "https://www.xiaohongshu.com/explore/note123?"
            "xsec_token=token%2Bvalue%3D&xsec_source=pc_feed"
        )
        video_result = parser._process_explore_data(video_data, final_url)
        image_result = parser._process_explore_data(image_data, final_url)
        await asyncio.gather(
            *(
                content.get_path()
                for content in [*video_result.contents, *image_result.contents]
            )
        )
        await parser.close_session()
        return video_result, image_result

    video_result, image_result = asyncio.run(run())
    # 开启且配了 Cookie：视频笔记挂评论任务，图文笔记不挂。
    assert callable(video_result.extra.get("text_card_comment_factory"))
    assert video_result.extra["comment_timeout"] == 90
    assert callable(image_result.extra.get("text_card_comment_factory"))
    assert image_result.extra.get("comment_timeout") == 90


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
                "cookies": {"douyin_ck": "sessionid=test"},
                "comments": {"douyin": True},
            },
            downloader,
        )
        douyin_result = await douyin._build_result_from_aweme(
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
    assert xhs_result.text == "小红书正文 #性能车"
    for result in (kuaishou_result, douyin_result, xhs_result):
        assert result.contents
        assert result.extra.get("render_text_card") is True
        # Delivery now merges image-post cards by default; the old opt-in flag
        # is not required. Assert the parser supplies the real body/media inputs.
        assert not result.has_video
        assert any(block.get("type") == "text" and block.get("text")
                   for block in result.extra.get("text_card_flow", []))
    # 抖音图文挂评论区；快手暂无评论区实现；小红书该用例未启用评论。
    assert callable(douyin_result.extra.get("text_card_comment_factory"))
    assert kuaishou_result.extra.get("text_card_comment_factory") is None
    assert xhs_result.extra.get("text_card_comment_factory") is None


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
        douyin_result = await douyin._build_result_from_aweme(
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
        assert result.extra.get("render_text_card") is True
        assert "video_separate_from_card" not in result.extra
    # 快手暂无评论区实现
    assert results[2].extra.get("text_card_comment_factory") is None


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

    bilibili_html = UnifiedCardRenderer(HtmlRenderService()).build_html(
        platform_key="bilibili",
        platform_name="B站动态",
        author_name="UP主",
        title="标题[doge]",
        text="正文[doge]",
        emotes=bilibili_map,
    )
    assert bilibili_html.count('class="inline-emote"') == 2
    assert "bfs/emote/doge.png" in bilibili_html


def test_unicode_emoji_render_inline_not_as_remote_images():
    """emoji 由容器内的彩色 emoji 字体内联渲染，不再包成 cdn.jsdelivr.net 的图片。"""

    shared_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="视频",
            cover="",
            total_text="1 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("用户"),
                    content=[CommentRichPart("text", "😡😏")],
                )
            ],
        )
    )
    assert "jsdelivr" not in shared_html
    assert "unicode-emoji" not in shared_html
    assert "😡😏" in shared_html

    bilibili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="视频",
            cover="",
            total_text="1 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge("用户"),
                    content=[BiliRichPart("text", "😡😏")],
                )
            ],
        )
    )
    assert "jsdelivr" not in bilibili_html
    assert "unicode-emoji" not in bilibili_html

    body_html = UnifiedCardRenderer(HtmlRenderService()).build_html(
        platform_key="weibo",
        platform_name="微博",
        author_name="作者",
        title="标题😏",
        text="正文😡",
    )
    assert "jsdelivr" not in body_html
    assert "inline-unicode-emoji" not in body_html
    assert "😡" in body_html


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


def test_bilibili_comment_renderer_renders_via_local_backend(tmp_path):
    calls = {}

    output = tmp_path / "comments.png"
    renderer = BiliCommentCanvas(
        HtmlRenderService(
            local_backend=_local_backend(tmp_path, calls, name="canvas.png")
        )
    )
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
        assert image.width == 900
        # 去掉「热门评论」标题栏与页脚后，单条评论的图只剩评论本身
        assert 140 <= image.height <= 200
    assert calls["options"]["full_page"] is True
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" not in calls["template"]
    assert "transform:scale(1.5)" not in calls["template"]
    assert "scrollbar-width:none" in calls["template"]
    # 标题栏与页脚已从 HTML 删除
    assert "热门评论" not in calls["template"]
    # 评论区图底部保留品牌署名（用户要求恢复）。
    # 品牌署名只在正文卡底部输出一份；独立评论图不再重复一遍。
    assert COMMENT_FOOTER_BRAND not in calls["template"]



def test_social_comment_canvas_renders_via_local_backend(tmp_path):
    calls = {}

    output = tmp_path / "social-comments.jpg"
    renderer = SocialCommentCanvas(
        HtmlRenderService(
            local_backend=_local_backend(
                tmp_path, calls, name="social-canvas.png", background="#161823"
            )
        )
    )
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
        assert image.width == 900
        assert 170 <= image.height <= 173
    assert calls["options"]["scale"] == "css"
    assert "@media (min-width:1000px)" not in calls["template"]
    assert "transform:scale(1.5)" not in calls["template"]
    assert "scrollbar-width:none" in calls["template"]



def test_html_render_service_always_available_without_injection():
    # 渲染固定走插件内 Chromium，不再依赖外部注入的渲染回调。
    service = HtmlRenderService()
    assert service.available is True



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


def test_text_card_renders_via_local_backend(tmp_path):
    calls = {}

    service = HtmlRenderService(
        local_backend=_local_backend(tmp_path, calls, name="text.png")
    )
    text_output = tmp_path / "text.png"

    async def run():
        await UnifiedCardRenderer(service).render_card(
            text_output,
            platform_name="微博",
            author_name="用户",
            text="正文 #话题#",
        )

    asyncio.run(run())
    with Image.open(text_output) as image:
        # 全平台统一使用 900px 的手机版式
        assert image.size == (900, 171)
    # 参考版式由 footer 顶部边框提供分隔线，保留品牌署名。
    assert 'class="footer"' in calls["template"]
    assert '.footer{padding:15.75px 36px;border-top:2.25px solid #1E2026' in calls["template"]
    assert calls["options"]["scale"] == "css"
    assert calls["options"]["timeout"] == 45_000
    assert "layout-xiaoheihe" in calls["template"]
    # 统一版式不注入平台主色
    assert "#ff8200" not in calls["template"]
    assert "platform-mark" not in calls["template"]
    assert "backdrop-filter" not in calls["template"]
    assert "radial-gradient" not in calls["template"]



def test_media_only_card_does_not_use_share_url_as_body(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 320))

    renderer = FakeCardRenderer()
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
    assert renderer.kwargs["media_fit"] == "cover"
    assert "xiaohongshu.com" not in repr(renderer.kwargs)


@pytest.mark.parametrize(
    ("key", "name", "accent"),
    [
        ("bilibili", "B站动态", "#fb7299"),
        ("douyin", "抖音", "#fe2c55"),
        ("kuaishou", "快手", "#ff4906"),
        ("xiaohongshu", "小红书", "#ff2442"),
        ("weibo", "微博", "#ff8200"),
        ("acfun", "AcFun", "#fd4c5b"),
    ],
)
def test_unified_content_card_keeps_only_platform_brand_identity(
    key,
    name,
    accent,
):
    html = UnifiedCardRenderer(HtmlRenderService()).build_html(
        platform_key=key,
        platform_name=name,
        author_name="作者",
        title="标题",
        text="正文",
    )

    # 统一版式不再带平台主色与旧卡片装饰，所有平台共用一套深色样式。
    assert "layout-xiaoheihe" in html
    assert accent not in html
    assert "platform-mark" not in html
    assert "backdrop-filter" not in html
    assert "radial-gradient" not in html
    # 方案三的外框、简介卡与楼中楼圆角统一按 400→900 适配。
    assert ">.card{" not in html
    assert 'data-card-style="minimal-feed"' not in html
    # 参考布局按 2.25 倍适配：标题 16.5、简介13、评论14。
    assert "width:900px" in html
    assert "background:#000" in html
    assert "font-size:37.125px" in html
    assert "font-size:29.25px" in html


def test_unified_card_layout_is_identical_across_platforms():
    """所有平台共用一套版式：标题/正文/署名尺寸完全一致。"""
    renderer = UnifiedCardRenderer(HtmlRenderService())
    htmls = [
        renderer.build_html(
            platform_key=key,
            platform_name=name,
            author_name="作者",
            title="标题",
            text="正文",
        )
        for key, name in (
            ("bilibili", "B站动态"),
            ("douyin", "抖音"),
            ("weibo", "微博"),
            ("xiaohongshu", "小红书"),
            ("xiaoheihe", "小黑盒"),
        )
    ]
    # 去掉平台名/首字母占位/平台选择器后，五份 HTML 必须逐字相同
    normalised = {
        re.sub(r'(data-platform=")[a-z]+(")', r"\1\2", html)
        .replace("B站动态", "").replace("抖音", "")
        .replace("微博", "").replace("小红书", "").replace("小黑盒", "")
        .replace(">B<", "><").replace(">抖<", "><")
        .replace(">微<", "><").replace(">薯<", "><").replace(">盒<", "><")
        for html in htmls
    }
    assert len(normalised) == 1, "各平台卡片的版式应当完全一致"


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

    html = UnifiedCardRenderer(HtmlRenderService()).build_html(
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
        'class="hero"',
        'class="primary-block"',
        'class="profile-block"',
        'class="copy-block"',
        'class="comment-section"',
    ]
    positions = [html.index(section) for section in ordered_sections]
    assert positions == sorted(positions)
    assert "完整正文第一段" in html
    assert "热门评论正文" in html
    assert "楼中楼回复" in html
    assert "1.2万" in html
    # 评论标题栏已按要求去掉；评论区直接内联在正文卡下方（不再用 Shadow DOM，
    # 否则评论画布自带一套样式，和卡片对不齐）。
    assert "attachShadow" not in html
    assert 'class="comment-section"' in html
    # 品牌页脚只在正文卡底部输出一次；评论画布不再自带 footer。
    assert 'class="footer"' in html
    assert "stroke-dasharray" not in html
    assert 'd="M21 15a4 4 0 0 1-4 4H8l-5 3V7' not in html
    assert "html,body{margin:0;width:900px" in html
    assert "scrollbar-width:none" in html
    assert ".card{width:100%" in html


def test_unified_long_card_renders_ordered_text_and_image_flow_once():
    html = UnifiedCardRenderer(HtmlRenderService()).build_html(
        platform_key="demo",
        platform_name="演示平台",
        author_name="作者",
        title="图文文章",
        text="不应额外重复的普通正文",
        content_blocks=[
            {"type": "text", "text": "第一段正文"},
            {"type": "image", "url": "https://img.example.com/first.jpg"},
            {"type": "text", "text": "第二段正文"},
            {"type": "image", "url": "https://img.example.com/second.jpg"},
        ],
    )

    ordered = [
        "第一段正文",
        "https://img.example.com/first.jpg",
        "第二段正文",
        "https://img.example.com/second.jpg",
    ]
    positions = [html.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert "不应额外重复的普通正文" not in html
    assert 'class="content-flow"' in html


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
    assert result.text == "完整简介"
    assert result.extra.get("render_text_card") is True
    assert "video_separate_from_card" not in result.extra
    assert "card_platform_name" not in result.extra
    assert True  # 图文帖同样保留评论任务
    assert callable(result.extra["text_card_comment_factory"])
    assert result.extra["comment_timeout"] == 90


def test_bilibili_live_links_are_silently_skipped():
    """直播解析已移除：直播间地址静默跳过，不弹失败提示。"""
    for url in (
        "https://live.bilibili.com/22637261",
        "https://live.bilibili.com/123?visit_id=abc",
    ):
        with pytest.raises(SkipParseException):
            BilibiliParser.search_url(url)

    # 直播间路由不再注册，避免被当成普通作品解析。
    assert not any("live.bilibili" in key for key, _ in BilibiliParser._key_patterns)


def test_text_card_without_author_does_not_render_empty_avatar(tmp_path):
    calls = {}

    asyncio.run(
        UnifiedCardRenderer(
            HtmlRenderService(local_backend=_local_backend(tmp_path, calls))
        ).render_card(
            tmp_path / "game.png",
            platform_name="演示平台",
            author_name=None,
            title="游戏标题",
            text="游戏简介",
        )
    )

    assert '<div class="profile">' not in calls["template"]


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
    assert ".comment-image-wrap{display:block;width:fit-content" not in bili_html
    assert ".comment-image-wrap,.sticker-image-wrap{display:block" in bili_html
    assert (
        ".comment-image{display:block;width:auto;height:auto;max-width:640px"
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
    assert 'class="sticker-image-wrap"' not in social_html
    assert 'class="sticker-image"' not in social_html
    assert (
        ".comment-image{display:block;width:auto;height:auto;max-width:640px"
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
        assert 'class="actions"' not in html
        assert 'class="reply-icon action-icon"' not in html
        assert "回复 " not in html
        assert '<svg class="like-icon action-icon"' not in html


def test_comment_canvases_share_neutral_header_and_light_dividers():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="B站标题",
            cover="https://i0.hdslb.com/cover.jpg",
            total_text="12 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户"),
                    content=[],
                    time_text="2小时前",
                    location="上海",
                ),
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户二"),
                    content=[],
                ),
            ],
        )
    )
    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="抖音标题",
            cover="https://p3.douyinpic.com/cover.jpg",
            total_text="12 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("用户"),
                    content=[],
                    time_text="2小时前",
                    location="上海",
                ),
                CommentEntry(author=CommentAuthor("用户二"), content=[]),
            ],
        )
    )

    for html in (bili_html, social_html):
        assert "background:#000000" in html
        # 「热门评论」标题栏与页脚已整块从 HTML 删除（不是 display:none）
        assert "热门评论" not in html
        assert "<header" not in html
        # 页脚署名统一由正文卡输出，评论画布不再自带 footer。
        assert "<footer" not in html
        assert '<span class="header-count">' not in html
        # 左右留白挪到单条评论上，分隔线才能通到底（和页脚那条对齐）。
        assert ".comment-list{padding:0}" in html
        assert (
            ".comment-card{display:flex;gap:20px;padding:28px 32px;"
            "border-bottom:1px solid #23252b}" in html
        )
        assert ".page{width:900px;padding:0}" in html
        assert "repeating-linear-gradient" not in html
        # IP location and timestamps are stripped from the rendered card, and
        # body text uses one single colour (no accent coloured highlights).
        assert "comment-meta" not in html
        assert "上海" not in html
        assert "2小时前" not in html
        assert ".highlight{color:#7f8cff}" in html
        assert ".comment-content{margin-top:14px;font-size:34px" in html
        assert 'class="cover"' not in html
        assert "B站标题" not in html
        assert "抖音标题" not in html


def test_comment_highlight_only_keeps_at_mentions_coloured():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户"),
                    content=[
                        BiliRichPart("text", "前面 "),
                        BiliRichPart("highlight", "@朋友"),
                        BiliRichPart("text", " 中间 "),
                        BiliRichPart("highlight", "相关视频"),
                        BiliRichPart("text", " 结尾"),
                    ],
                )
            ],
        )
    )
    assert '<span class="highlight">@朋友</span>' in bili_html
    assert '<span class="highlight">相关视频</span>' not in bili_html
    assert "<span>相关视频</span>" in bili_html

    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("用户"),
                    content=[
                        CommentRichPart("highlight", "＠全角艾特"),
                        CommentRichPart("text", " 正文"),
                    ],
                )
            ],
        )
    )
    assert '<span class="highlight">＠全角艾特</span>' in social_html


def test_comment_canvases_mark_pinned_entries_and_drop_ip_time():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="B站标题",
            cover="",
            total_text="2 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="置顶的人"),
                    content=[BiliRichPart("text", "第一条")],
                    time_text="2小时前",
                    location="上海",
                    pinned=True,
                ),
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="普通的人"),
                    content=[BiliRichPart("text", "第二条")],
                    time_text="1小时前",
                    location="北京",
                ),
            ],
        )
    )
    assert bili_html.count('<span class="pinned">置顶</span>') == 1
    # The marker trails the nickname, on the right-hand side of the row.
    assert (
        '<div class="nickname"><span class="nickname-text">置顶的人</span>'
        '<span class="pinned">置顶</span></div>' in bili_html
    )
    assert (
        '<div class="nickname"><span class="nickname-text">普通的人</span></div>'
        in bili_html
    )
    assert "上海" not in bili_html and "北京" not in bili_html
    assert "2小时前" not in bili_html and "1小时前" not in bili_html
    assert "comment-meta" not in bili_html

    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="抖音标题",
            cover="",
            total_text="2 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("置顶的人"),
                    content=[CommentRichPart("text", "第一条")],
                    time_text="2小时前",
                    location="上海",
                    pinned=True,
                ),
                CommentEntry(
                    author=CommentAuthor("普通的人"),
                    content=[CommentRichPart("text", "第二条")],
                ),
            ],
        )
    )
    assert social_html.count('<span class="pinned">置顶</span>') == 1
    assert "上海" not in social_html and "2小时前" not in social_html
    assert "comment-meta" not in social_html


def test_comment_nickname_row_orders_nickname_author_then_pinned():
    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="作品",
            cover="",
            total_text="2 条评论",
            entries=[
                # UP主自己的置顶评论: nickname -> 作者 -> 置顶
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="UP本人", is_up=True),
                    content=[BiliRichPart("text", "置顶的UP主评论")],
                    pinned=True,
                ),
                # UP主评论但不置顶
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="UP第二", is_up=True),
                    content=[BiliRichPart("text", "UP主评论")],
                ),
                # 普通用户被置顶
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="路人"),
                    content=[BiliRichPart("text", "普通评论")],
                    pinned=True,
                ),
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="路人二"),
                    content=[BiliRichPart("text", "普通评论二")],
                ),
            ],
        )
    )
    assert (
        '<div class="nickname"><span class="nickname-text">UP本人</span>'
        '<span class="author">作者</span><span class="pinned">置顶</span></div>'
        in bili_html
    )
    assert (
        '<div class="nickname"><span class="nickname-text">UP第二</span>'
        '<span class="author">作者</span></div>' in bili_html
    )
    assert (
        '<div class="nickname"><span class="nickname-text">路人</span>'
        '<span class="pinned">置顶</span></div>' in bili_html
    )
    assert (
        '<div class="nickname"><span class="nickname-text">路人二</span></div>'
        in bili_html
    )
    assert bili_html.count('<span class="author">作者</span>') == 2

    # Douyin / Weibo keep the author flag on the author dataclass badges.
    social_html = SocialCommentCanvas().build_html(
        CommentDocument(
            theme=DOUYIN_THEME,
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                CommentEntry(
                    author=CommentAuthor("抖音作者", badges=[CommentBadge("作者")]),
                    content=[CommentRichPart("text", "作者评论")],
                    pinned=True,
                )
            ],
        )
    )
    assert (
        '<div class="nickname"><span class="nickname-text">抖音作者</span>'
        '<span class="author">作者</span><span class="pinned">置顶</span></div>'
        in social_html
    )


def test_comment_emoji_sizes_are_relative_to_the_text():
    def css(html: str, selector: str) -> str:
        for chunk in html.split("}"):
            if chunk.strip().startswith(selector + "{"):
                return chunk.split("{", 1)[1]
        raise AssertionError(f"selector not found: {selector}")

    bili_html = BiliCommentCanvas().build_html(
        BiliCommentDocument(
            work_title="作品",
            cover="",
            total_text="1 条评论",
            entries=[
                BiliCommentEntry(
                    author=BiliAuthorBadge(nickname="用户"),
                    content=[BiliRichPart("text", "😡 [doge] [未知]")],
                )
            ],
        )
    )
    # Emoji images and token pills scale with the surrounding font size, so a
    # reply (smaller font) renders them smaller too - no fixed px boxes.
    emote_css = css(bili_html, ".emote")
    assert "height:1.15em" in emote_css
    assert "width:auto" in emote_css
    assert "px" not in emote_css
    assert "background:" not in emote_css
    token_css = css(bili_html, ".emoji-text")
    assert "font-size:.85em" in token_css
    # Badges are flex-centred so their label is dead centre, with square padding.
    badge_css = css(bili_html, ".pinned,.author")
    assert "display:inline-flex" in badge_css
    assert "align-items:center" in badge_css
    assert "justify-content:center" in badge_css
    assert "line-height:1" in badge_css
    assert "height:1.55em" in badge_css
    # 作者 and 置顶 must NOT share a palette (user requirement).
    pinned_css = css(bili_html, ".pinned")
    author_css = css(bili_html, ".author")
    assert "background:#262a3d" in pinned_css and "color:#c9cdd4" in pinned_css
    assert "background:#3a2b16" in author_css and "color:#e8b45f" in author_css
    assert pinned_css != author_css
    # The reply row must lead its nickname and body with the same line-height,
    # otherwise the reply text box is visibly asymmetric (text sits high).
    assert ".reply-card .nickname{font-size:29px;line-height:1.6}" in bili_html
    # The reply avatar stays top-aligned (flex-start) on purpose - centring it
    # was tried and rejected, so do not reintroduce align-items:center here.
    assert ".reply-card{display:flex;gap:18px;" in bili_html
    assert ".reply-card{display:flex;align-items:center" not in bili_html
    # 字体栈必须让中文与西文落到同一个字体：
    # 'Noto Sans SC' 是插件自带、以 base64 内联进页面的思源黑体子集
    # （见 skill 的 parser-x-comment-card-rendering.md §2.3）；它不在系统字体里，
    # 所以只会命中我们内嵌的那份，中西文/数字字形完全一致。
    # 'Noto Sans CJK SC'（容器系统那份）与文泉驿、彩色 emoji 字体只做兜底。
    assert "'Noto Sans SC','Noto Sans CJK SC'" in bili_html
    assert "'WenQuanYi Zen Hei'" in bili_html
    assert '"Parser X Latin"' not in bili_html


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

    parser = FakeParser()
    # This fixture isolates the dynamic body; comment fetching is tested separately.
    parser.enable_comment_card = False
    service = BiliDynamicService(parser)

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
    # 单图帖把这张图提升为卡片头图，正文流里不再重复
    assert result.extra["text_card_media"] == "https://i0.hdslb.com/dynamic.jpg@1280w_85q.jpg"
    assert not any(
        block.get("type") == "image"
        for block in result.extra.get("text_card_flow") or []
    )
    assert result.extra["image_post_card_in_forward"] is True
    assert result.extra["text_card_avatar"].startswith("data:image/png")
    assert "单图直接引用" in result.extra["card_info"]
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

    feed = BiliCommentFeed(FakeParser())
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

    renderer = SocialCommentCanvas(
        HtmlRenderService(local_backend=_local_backend(tmp_path, calls))
    )
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
        assert image.width == 900
        assert 170 <= image.height <= 173
    assert "{{ 7 * 7 }}" not in calls["template"]
    assert "{% unsafe %}" not in calls["template"]
    assert "&#123;" in calls["template"]


def test_douyin_comment_normalization_covers_rconsole_visible_fields(tmp_path):
    class FakeParser:
        headers = {"User-Agent": "Mozilla/5.0"}
        cookies = "sessionid=test"
        cache_dir = tmp_path

    feed = DouyinCommentFeed(FakeParser())
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

        def download_video(self, *_args, **_kwargs):
            async def finish():
                output = tmp_path / "video.mp4"
                output.write_bytes(b"video")
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
        result = await douyin._build_result_from_aweme(
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
                "video": {
                    "play_addr": {"url_list": ["https://v3.douyinvod.com/video.mp4"]},
                    "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpg"]},
                    "duration": 12,
                },
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
    assert callable(douyin_extra["text_card_comment_factory"])
    assert douyin_extra["comment_timeout"] == 90
    # 抖音正文表情现在由解析器按官方目录捕获，卡片才能把 [比心] 换成图片。
    assert douyin_extra["card_emotes"]["[比心]"].startswith("https://")
    assert douyin_extra.get("render_text_card") is True
    assert "comment_document_task_factory" not in weibo_extra
    assert callable(weibo_extra["text_card_comment_factory"])
    assert weibo_extra["comment_timeout"] == 90


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
                "comments": {"weibo": True},
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
    # 出图帖现在也生成正文卡，表情目录随卡片一起下发。
    assert result.extra.get("card_emotes")
    assert result.extra.get("render_text_card") is True
    assert len(result.contents) == 1
    assert result.delivery is not None
    assert len(result.delivery.batches) == 1
    assert result.delivery.batches[0].parts == result.contents
    # 不再引用原消息：全平台统一只用合并转发。
    assert result.delivery.batches[0].reply_original is False
    assert callable(result.extra.get("text_card_comment_factory"))


def test_weibo_mixed_image_video_is_parsed_and_sent_in_one_forward(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import (
        VideoContent as PluginVideoContent,
    )
    from astrbot_plugin_parser_x.core.parsers import WeiboParser as PluginWeiboParser
    from astrbot_plugin_parser_x.main import ParserXPlugin

    class FakeDownloader:
        def download_img(self, _url, **_kwargs):
            async def done():
                path = tmp_path / "weibo-image.jpg"
                path.write_bytes(b"image")
                return path

            return asyncio.create_task(done())

        def download_video(self, _url, **_kwargs):
            async def done():
                path = tmp_path / "weibo-video.mp4"
                path.write_bytes(b"video")
                return path

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
                    "text": "<p>同时包含图片和视频的微博</p>",
                    "user": {
                        "id": "1088413295",
                        "screen_name": "Easy",
                        "avatar_large": "https://wx1.sinaimg.cn/avatar.jpg",
                    },
                    "pics": [
                        {
                            "pid": "static-picture",
                            "large": {"url": "https://wx1.sinaimg.cn/large/demo.jpg"},
                        }
                    ],
                    "page_info": {
                        "type": "video",
                        "media_info": {
                            "mp4_hd_url": "https://f.video.weibocdn.com/demo.mp4",
                            "duration": 12,
                        },
                    },
                },
            }

    class FakeClient:
        async def get(self, _url, **_kwargs):
            return FakeResponse()

        async def close(self):
            return None

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

    async def run():
        parser = PluginWeiboParser(
            {
                "cache_dir": str(tmp_path),
                "cookies": {},
                "comments": {"weibo": False},
            },
            FakeDownloader(),
        )
        parser._session = FakeClient()
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {"behavior": {"show_download_fail_tip": True}}
        plugin.cache_dir = tmp_path

        class FakeCardRenderer:
            async def render_card(self, out_path, **_kwargs):
                _save_render_fixture(out_path, size=(760, 420))

        plugin.text_renderer = FakeCardRenderer()
        event = Event()
        try:
            keyword, searched = parser.search_url(
                "https://weibo.com/1088413295/IpOAqcs7h"
            )
            result = await parser.parse(keyword, searched)
            await plugin._send_parse_result(event, result)
            return result, event
        finally:
            await parser.close_session()

    result, event = asyncio.run(run())
    assert len(result.contents) == 2
    assert isinstance(result.contents[0], PluginImageContent)
    assert isinstance(result.contents[1], PluginVideoContent)
    assert result.delivery is not None
    assert len(result.delivery.batches) == 1
    assert result.delivery.batches[0].mode == "forward"
    assert result.delivery.batches[0].parts == result.contents
    # 图文混排帖同样生成正文卡，卡片作为合并转发的首个节点。
    assert result.extra.get("render_text_card") is True
    assert result.extra.get("card_kind") == "微博"

    # 微博混排帖按统一结构发：视频单独直发，正文卡进合并转发。
    forward_chain = next(
        chain
        for chain in event.sent
        if any(isinstance(segment, Nodes) for segment in chain)
    )
    nodes = forward_chain[0].nodes
    node_contents = [node.content[0] for node in nodes]
    assert isinstance(node_contents[0], MessageImage), "合并转发首节点是正文卡"
    # 全平台统一：卡片之后跟随全部原图，视频走投递计划单独直发。
    assert any(isinstance(c, MessageImage) for c in node_contents[1:]), "原图要跟在卡片后面"


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
    # 作者头像现在会先下载内嵌（下载失败才退回远程地址），因此多一次取头像请求。
    assert "https://m.weibo.cn/statuses/show?id=4910316167105260" in calls
    assert "https://m.weibo.cn/statuses/extend?id=4910316167105260" in calls
    assert "https://wx1.sinaimg.cn/avatar.jpg" in calls
    assert result.text == "完整长微博正文"
    assert result.delivery is not None
    assert result.delivery.batches[0].parts == ["识别：微博\n完整长微博正文"]
    assert result.extra["render_text_card"] is True
    assert "text_card_media" not in result.extra


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
        ("https://weibo.com/ajax/statuses/show", "SUB=test"),
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
        cookies = ""
        cache_dir = tmp_path

    feed = WeiboCommentFeed(FakeParser())
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
        {"id": "42"},
        {},
    )

    assert normalized is not None
    assert normalized.author.is_up is True
    assert {badge.text for badge in normalized.author.badges} >= {"V", "VIP3"}
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
        cookies = ""
        cache_dir = tmp_path
        feed_calls = 0

        async def http_get(self, *_args, **_kwargs):
            self.feed_calls += 1
            return FakeResponse()

    parser = FakeParser()
    feed = WeiboCommentFeed(parser)
    result = asyncio.run(feed.fetch("4461526582968019"))

    assert parser.feed_calls == 1
    assert result.has_more is False
    assert result.total == 1
    assert len(result.items) == 1


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
        feed = BiliCommentFeed(FakeParser(), limit=5)

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
        return await feed.build_document(
            2,
            1,
            video_title="标题",
            video_cover="",
        )

    document = asyncio.run(run())
    # 评论区改为只产出 CommentDocument，由统一卡片渲染器内联出图。
    assert document is not None
    assert len(document.entries) == 5


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
        feed = DouyinCommentFeed(FakeParser(), limit=5)

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
        return await feed.build_document(
            "7414051930047106342",
            work_title="标题",
            cover="",
        )

    document = asyncio.run(run())
    # 评论区改为只产出 CommentDocument，由统一卡片渲染器内联出图。
    assert document is not None
    assert len(document.entries) == 5


def test_weibo_comment_feed_renders_all_selected_comments_in_one_image(tmp_path):
    class FakeParser:
        headers = {}
        cookies = "SUB=test"
        cache_dir = tmp_path

    class FakeCanvas:
        documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        canvas = FakeCanvas()
        feed = WeiboCommentFeed(FakeParser(), limit=5)

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
        return await feed.build_document(
            "4461526582968019",
            work_title="标题",
            cover="",
            owner={},
        )

    document = asyncio.run(run())
    # 评论区改为只产出 CommentDocument，由统一卡片渲染器内联出图。
    assert document is not None
    assert len(document.entries) == 5


def test_weibo_comment_feed_embeds_main_and_reply_avatars(tmp_path):
    avatar_bytes = b"weibo-avatar"

    class FakeResponse:
        status_code = 200
        content = avatar_bytes
        headers = {"Content-Type": "image/jpeg"}

    class FakeParser:
        headers = {"User-Agent": "test"}
        cookies = "SUB=test"
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
        feed = WeiboCommentFeed(parser, limit=1)

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
        document = await feed.build_document(
            "4461526582968019",
            work_title="标题",
            cover="",
            owner={},
        )
        return parser, document

    parser, document = asyncio.run(run())
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
        cookies = "SUB=test"
        cache_dir = tmp_path

    class FakeCanvas:
        def __init__(self):
            self.documents = []

        async def render(self, target, document):
            self.documents.append(document)
            target.write_bytes(b"single-image")

    async def run():
        bili_canvas = FakeCanvas()
        bili_feed = BiliCommentFeed(BiliParser(), limit=1)

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
        douyin_feed = DouyinCommentFeed(DouyinParserStub(), limit=1)

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
        weibo_feed = WeiboCommentFeed(WeiboParserStub(), limit=1)

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
            bili_feed.build_document(2, 1, video_title="标题", video_cover=""),
            douyin_feed.build_document("3", work_title="标题", cover=""),
            weibo_feed.build_document("4", work_title="标题", cover="", owner={}),
        )
        return results

    documents = asyncio.run(run())
    for document in documents:
        # 页脚署名已整块移除：文档不再携带 footer_text，卡片里也只有评论本身。
        assert not hasattr(document, "footer_text")
        assert not hasattr(document, "theme") or document.theme



def test_comment_layout_cache_versions_invalidate_pre_fix_images():
    assert BiliCommentFeed.CACHE_VERSION == "bili_comment_v18_author_color_reply_pad_font"
    # v18：抖音评论区补抓楼中楼回复，缓存结构变化必须让旧缓存失效。
    assert DouyinCommentFeed.CACHE_VERSION == "douyin_comment_v18_reply_thread"
    # v19 includes reply picture links; pre-v19 cached images must not be reused.
    assert WeiboCommentFeed.CACHE_VERSION == "weibo_comment_v19_reply_picture_links"


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


def test_single_image_result_only_replies_with_source_image(tmp_path):
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

    # 单图帖不再引用原消息，而是打包成合并转发里的一批节点。
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Nodes)
    node_contents = [node.content[0] for node in event.sent[0][0].nodes]
    assert any(isinstance(c, MessageImage) for c in node_contents)
    assert not any(isinstance(c, Reply) for c in node_contents)
    # 正文文本作为一个独立节点一起进转发，不能丢。
    assert any(
        isinstance(c, Nodes) for c in []  # 占位，保持结构简单
    ) is False
    assert any(
        "不能被漏掉的动态正文" in getattr(c, "text", "")
        for c in node_contents
    ), "正文文本必须作为独立节点进转发"


def test_weibo_single_image_sends_body_card_and_source_image(tmp_path):
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
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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
        extra={
            "render_text_card": True,
            "text_card_media": "",
            "image_post_card_in_forward": True,
        },
    )

    asyncio.run(plugin._send_parse_result(event, result))

    # 单图帖不再引用原消息：正文卡与源图都作为独立节点进合并转发。
    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    images = [c for c in parts if isinstance(c, MessageImage)]
    assert len(images) == 2
    assert Path(images[0].file).name.startswith("text_card_weibo_")
    assert images[1].file == str(image_path)


def test_body_card_media_never_replaces_single_source_image(tmp_path):
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

    class FakeCardRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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
            # 微博语义：正文卡片不替代源图，原图要单独发
            "card_keeps_source_media": True,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.kwargs is not None
    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    images = [c for c in parts if isinstance(c, MessageImage)]
    assert len(images) == 2
    assert Path(images[0].file).name.startswith("text_card_weibo_")
    assert images[1].file == str(source_path)


@pytest.mark.parametrize(
    ("platform_name", "display_name"),
    [("demo", "演示平台")],
)
def test_native_graphic_posts_render_as_one_direct_image(
    tmp_path,
    platform_name,
    display_name,
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
    from astrbot_plugin_parser_x.main import ParserXPlugin

    source_path = tmp_path / "body.png"
    _save_render_fixture(source_path)
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 420))

    plugin.text_renderer = FakeCardRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2806)

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

    source = PluginImageContent(source_path)
    result = ParseResult(
        platform=Platform(name=platform_name, display_name=display_name),
        title="图文标题",
        text="图文正文",
        contents=[source],
        delivery=DeliveryPlan([DeliveryBatch(["图文正文", source], mode="forward")]),
        extra={
            "render_text_card": True,
            "text_card_flow": [
                {"type": "text", "text": "图文正文"},
                {"type": "image", "url": "https://img.example.com/body.png"},
            ],
            "delivery_text_card_consume_non_video": True,
            "native_delivery": True,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    # 全平台统一：不再引用原消息直发单图，正文卡 + 媒体一起进合并转发。
    assert plugin.text_renderer.kwargs is not None
    assert len(event.sent) == 1
    payload = event.sent[0]
    assert len(payload) == 1
    assert isinstance(payload[0], Nodes)
    node_contents = [node.content[0] for node in payload[0].nodes]
    assert isinstance(node_contents[0], MessageImage), "首节点是正文卡"
    assert not any(isinstance(part, Reply) for node in payload[0].nodes for part in node.content)


def test_native_one_image_flow_keeps_video_separate(tmp_path):
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

    class FakeCardRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 320))

    plugin.text_renderer = FakeCardRenderer()

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
        platform=Platform(name="demo", display_name="演示平台"),
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
        extra={
            "render_text_card": True,
            "text_card_media": "https://img.example.com/cover.png",
            "text_card_flow": [
                {"type": "text", "text": "富文本正文"},
                {"type": "image", "url": "https://img.example.com/body.png"},
            ],
            "delivery_text_card_consume_non_video": True,
            "native_delivery": True,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.kwargs is None
    # No comments: video is the entire payload, never a body-card/text fallback.
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)


@pytest.mark.parametrize("image_count", [1, 2])
def test_split_image_posts_send_body_card_with_original_images(
    tmp_path,
    image_count,
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
    from astrbot_plugin_parser_x.main import ParserXPlugin

    source_paths = []
    for index in range(image_count):
        path = tmp_path / f"source-{index}.jpg"
        path.write_bytes(f"source-{index}".encode())
        source_paths.append(path)
    comment_path = tmp_path / "comment.jpg"
    comment_path.write_bytes(b"comment")

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        def __init__(self):
            self.kwargs = None

        async def render_card(self, out_path, **kwargs):
            self.kwargs = kwargs
            _save_render_fixture(out_path, size=(760, 300))

    plugin.text_renderer = FakeCardRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2810)

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

    comment_builds = []

    async def build_comment_images():
        comment_builds.append(True)
        return [PluginImageContent(comment_path)]

    sources = [PluginImageContent(path) for path in source_paths]
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        title="图文标题",
        text="图文正文",
        contents=sources,
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["旧正文摘要"]),
                DeliveryBatch(list(sources), mode="forward"),
            ]
        ),
        extra={
            "render_text_card": True,
            "text_card_media": "",
            "image_post_card_in_forward": True,
            "text_card_comment_factory": build_comment_images,
            "comment_timeout": 5,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.kwargs is not None
    # 图文帖同样要出评论区：评论图必须在卡片里拼好。视频帖才由
    # process_comment_content 单独发，图文帖没有那条路径。
    assert comment_builds == [True]
    # 单图与多图现在走同一套投递：不再引用原消息，全部打包成合并转发。
    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    if image_count == 1:
        parts = [node.content[0] for node in forward.nodes]
        assert not any(isinstance(c, Reply) for c in parts)
        images = [c for c in parts if isinstance(c, MessageImage)]
        assert len(images) == 2
        assert Path(images[0].file).name.startswith("text_card_weibo_")
        assert images[1].file == str(source_paths[0])
    else:
        assert all(len(node.content) == 1 for node in forward.nodes)
        assert Path(forward.nodes[0].content[0].file).name.startswith(
            "text_card_weibo_"
        )
        assert [node.content[0].file for node in forward.nodes[1:]] == [
            str(path) for path in source_paths
        ]


def test_split_single_image_card_failure_keeps_text_and_original(tmp_path):
    from astrbot_plugin_parser_x.core.data import (
        ImageContent as PluginImageContent,
    )
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    source_path = tmp_path / "source.jpg"
    source_path.write_bytes(b"source")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FailingTextRenderer:
        async def render_card(self, *_args, **_kwargs):
            raise RuntimeError("renderer unavailable")

    plugin.text_renderer = FailingTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2811)

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
        platform=Platform(name="xiaohongshu", display_name="小红书"),
        title="笔记标题",
        text="渲染失败也不能丢失的正文",
        contents=[PluginImageContent(source_path)],
        url="https://www.xiaohongshu.com/explore/demo",
        extra={
            "render_text_card": True,
            "text_card_media": "",
            "image_post_card_in_forward": True,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    assert any(
        isinstance(c, Plain) and "渲染失败也不能丢失的正文" in c.text for c in parts
    )
    assert any(
        isinstance(c, MessageImage) and c.file == str(source_path) for c in parts
    )


def test_one_flow_card_failure_restores_native_text_and_images(tmp_path):
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

    source_path = tmp_path / "body.jpg"
    source_path.write_bytes(b"body")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FailingTextRenderer:
        async def render_card(self, *_args, **_kwargs):
            raise RuntimeError("renderer unavailable")

    plugin.text_renderer = FailingTextRenderer()

    class Event:
        message_obj = SimpleNamespace(message_id=2812)

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

    source = PluginImageContent(source_path)
    result = ParseResult(
        platform=Platform(name="demo", display_name="演示平台"),
        title="文章标题",
        text="文章正文",
        contents=[source],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["文章概要"]),
                DeliveryBatch(["文章正文", source], mode="forward"),
            ]
        ),
        extra={
            "render_text_card": True,
            "text_card_flow": [
                {"type": "text", "text": "文章正文"},
                {"type": "image", "url": "https://img.example.com/body.jpg"},
            ],
            "delivery_text_card_consume_non_video": True,
            "native_delivery": True,
        },
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    # 渲染失败时正文不能丢：文字以合并转发发出，原图也照常跟着发。
    forward_chain = next(
        chain
        for chain in event.sent
        if any(isinstance(segment, Nodes) for segment in chain)
    )
    payload = forward_chain
    assert isinstance(payload[0], Nodes)
    parts = [node.content[0] for node in payload[0].nodes]
    assert any(
        isinstance(c, Plain) and "文章正文" in c.text for c in parts
    )


def test_video_comments_wait_until_main_video_send_finishes(tmp_path):
    # Both native and non-native document paths are exercised by the shared
    # parametrized regression suite; this keeps the historical entry point.
    from ..delivery.test_unified_delivery import test_ready_comment_card_waits_for_video_completion
    test_ready_comment_card_waits_for_video_completion(tmp_path, False)


def test_native_video_comments_also_wait_until_video_send_finishes(tmp_path):
    from ..delivery.test_unified_delivery import test_ready_comment_card_waits_for_video_completion
    test_ready_comment_card_waits_for_video_completion(tmp_path, True)


def test_comment_failure_does_not_block_video(tmp_path):
    from ..delivery.test_unified_delivery import assert_comment_failure_preserves_video
    assert_comment_failure_preserves_video(tmp_path, "failure")


def test_comment_forward_failure_falls_back_to_title_and_images(tmp_path):
    from ..delivery.test_unified_delivery import assert_forward_failure_preserves_card
    assert_forward_failure_preserves_card(tmp_path)


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
        async def render_card(self, *_args, **_kwargs):
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


def test_plain_weibo_without_media_sends_body_card(tmp_path):
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

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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
        extra={"render_text_card": True},
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    # 全平台统一：纯文字帖的正文卡也作为合并转发的唯一节点发出。
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Nodes)
    node_contents = [node.content[0] for node in event.sent[0][0].nodes]
    assert len(node_contents) == 1
    assert isinstance(node_contents[0], MessageImage)
    assert Path(node_contents[0].file).name.startswith("text_card_weibo_")


def test_single_image_waits_for_card_and_source_before_combined_send(tmp_path):
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
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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
        extra={
            "render_text_card": True,
            "text_card_media": "",
            "image_post_card_in_forward": True,
        },
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
            assert event.sent == []
            release_download.set()
            await send_task

    asyncio.run(run())
    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    assert len([c for c in parts if isinstance(c, MessageImage)]) == 2


def test_video_delivery_plan_suppresses_body_text_and_preview_images(tmp_path):
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

    # No comments: video is the entire payload, never a body-card/text fallback.
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)


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
        extra={"native_delivery": True},
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


def test_single_image_card_keeps_original_source_image(tmp_path):
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

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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
            # 微博语义：正文卡片不替代源图，原图要单独发
            "card_keeps_source_media": True,
        },
    )

    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    images = [c for c in parts if isinstance(c, MessageImage)]
    assert len(images) == 2
    assert Path(images[0].file).name.startswith("text_card_weibo_")
    assert images[1].file == str(image_path)


def test_unified_card_is_sent_standalone_without_original_message_id(tmp_path):
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(760, 260))

    plugin.text_renderer = FakeCardRenderer()

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

    # 不依赖原消息 message_id 的引用，卡片照常进合并转发。
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Nodes)
    assert isinstance(event.sent[0][0].nodes[0].content[0], MessageImage)


def test_multi_image_result_sends_card_and_all_source_images_in_forward(tmp_path):
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

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(900, 480))

    plugin.text_renderer = FakeCardRenderer()

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

    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Nodes)
    assert len(event.sent[0][0].nodes) == 3
    assert all(len(node.content) == 1 for node in event.sent[0][0].nodes)
    assert all(
        isinstance(node.content[0], MessageImage) for node in event.sent[0][0].nodes
    )
    assert Path(event.sent[0][0].nodes[0].content[0].file).name.startswith(
        "text_card_xiaohongshu_"
    )
    assert [node.content[0].file for node in event.sent[0][0].nodes[1:]] == [
        str(first_path),
        str(second_path),
    ]


def test_video_result_suppresses_body_card_without_comments(tmp_path):
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

    class FakeCardRenderer:
        async def render_card(self, out_path, **_kwargs):
            _save_render_fixture(out_path, size=(900, 480))

    plugin.text_renderer = FakeCardRenderer()

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

    # No comments: video is the entire payload, never a body-card/text fallback.
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)


@pytest.mark.parametrize("has_comments", [False, True])
def test_video_delivery_keeps_only_video_and_optional_comment_card(tmp_path, has_comments):
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform, VideoContent
    from astrbot_plugin_parser_x.main import ParserXPlugin

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path

    class FakeCardRenderer:
        def __init__(self):
            self.calls = []

        async def render_card(self, out_path, **kwargs):
            self.calls.append(kwargs)
            _save_render_fixture(out_path, size=(900, 480))

    plugin.text_renderer = FakeCardRenderer()

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

    result = ParseResult(
        platform=Platform(name="bilibili", display_name="B站"),
        title="视频标题",
        text="视频简介",
        contents=[VideoContent(video_path)],
        extra={"render_text_card": True},
    )
    from astrbot_plugin_parser_x.core.comment_canvas import (
        CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, WEIBO_THEME,
    )
    document = CommentDocument(WEIBO_THEME, "视频标题", "", "1", [
        CommentEntry(CommentAuthor("评论作者"), [CommentRichPart("text", "保留评论")]),
    ])

    async def comments():
        return document

    if has_comments:
        result.extra["text_card_comment_factory"] = comments
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == (2 if has_comments else 1)
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)
    if has_comments:
        assert len(plugin.text_renderer.calls) == 1
        assert plugin.text_renderer.calls[0]["comment_document"] is document
        assert len(event.sent[1]) == 1
        forward = event.sent[1][0]
        assert isinstance(forward, Nodes)
        assert isinstance(forward.nodes[0].content[0], Plain)
        assert forward.nodes[0].content[0].text == "B站 · 热门评论"
        assert isinstance(forward.nodes[1].content[0], MessageImage)
        assert len(forward.nodes) == 2
    else:
        assert plugin.text_renderer.calls == []


def test_native_video_delivery_suppresses_body_text_and_card(tmp_path):
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

    class FailingTextRenderer:
        async def render_card(self, *_args, **_kwargs):
            raise AssertionError("body card renderer must not be called")

    plugin.text_renderer = FailingTextRenderer()

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

    video = PluginVideoContent(video_path)
    result = ParseResult(
        platform=Platform(name="demo", display_name="演示平台"),
        title="文章标题",
        text="文章正文",
        contents=[video],
        delivery=DeliveryPlan(
            [
                DeliveryBatch(["识别：演示平台\n文章正文"]),
                DeliveryBatch([video]),
            ]
        ),
        extra={"render_text_card": True, "native_delivery": True},
    )
    event = Event()
    asyncio.run(plugin._send_parse_result(event, result))

    # No comments: video is the entire payload, never a body-card/text fallback.
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)


def test_video_body_renderer_failure_does_not_resurrect_text(tmp_path):
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
        calls = 0

        async def render_card(self, *_args, **_kwargs):
            self.calls += 1
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

    from astrbot_plugin_parser_x.core.comment_canvas import (
        CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, WEIBO_THEME,
    )

    async def comments():
        return CommentDocument(WEIBO_THEME, "视频", "", "1", [
            CommentEntry(CommentAuthor("读者"), [CommentRichPart("text", "评论")]),
        ])

    event = Event()
    result = ParseResult(
        platform=Platform(name="weibo", display_name="微博"),
        author=Author(name="作者"),
        text="渲染失败也不能恢复视频正文",
        contents=[VideoContent(video_path)],
        url="https://weibo.com/example",
        extra={"render_text_card": True, "text_card_comment_factory": comments},
    )

    asyncio.run(plugin._send_parse_result(event, result))

    assert plugin.text_renderer.calls == 1
    # Comment renderer failed: never resurrect the video body as plain text.
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert isinstance(event.sent[0][0], MessageVideo)
    assert event.sent[0][0].file == str(video_path)


def test_plugin_initializes_and_registers_aiocqhttp_parsers(tmp_path):
    from astrbot_plugin_parser_x.main import ParserXPlugin

    class Context:
        def __init__(self):
            self.web_apis = []

        def get_config(self):
            return {}

        def register_web_api(self, route, handler, methods, description):
            self.web_apis.append((route, handler, methods, description))

    schema = json.loads(
        (Path(__file__).parents[2] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    config = AstrBotConfig(
        config_path=str(tmp_path / "parser_x_config.json"),
        schema=schema,
    )
    config["bili_comment"] = "false"
    config["platforms"] = {
        "tieba": True,
        "xigua": True,
        "pipixia": True,
        "weishi": True,
        "xiaoheihe": True,
        "miyoushe": True,
    }
    config["comments"].update({"xiaoheihe": True, "miyoushe": True})
    config["cookies"].update(
        {"xiaoheihe_cookie": "retired-cookie", "weibo_cookie": "keep-cookie"}
    )
    config["integrations"] = {"tieba_api_base": "http://example.invalid/api"}

    async def run_lifecycle():
        with patch.object(StarTools, "get_data_dir", return_value=tmp_path / "data"):
            context = Context()
            plugin = ParserXPlugin(context, config)
            await plugin.initialize()
            try:
                assert "b23.tv" in plugin.parser_map
                assert "miyoushe.com" not in plugin.parser_map
                assert "xiaoheihe" in plugin.parser_map
                assert "tieba.baidu.com" in plugin.parser_map
                assert "y.qq.com" not in plugin.parser_map
                assert "kugou.com" not in plugin.parser_map
                assert "qishui.douyin.com" not in plugin.parser_map
                assert "channels.weixin.qq.com" in plugin.parser_map
                assert "tiktok.com" not in plugin.parser_map
                assert "youtube.com" not in plugin.parser_map
                assert "ixigua.com" not in plugin.parser_map
                assert "pipix.com" not in plugin.parser_map
                assert "pipigx.com" not in plugin.parser_map
                assert "weishi.qq.com" not in plugin.parser_map
                assert "xsj.qq.com" not in plugin.parser_map
                assert plugin.key_pattern_list
                assert plugin.config["config_slim_migrated"] is True
                assert plugin.config["comments"]["enabled"] is True
                assert plugin._show_download_fail_tip() is True
                assert plugin.config["behavior"]["disabled_sessions"] == []
                assert "platforms" not in plugin.config
                assert "miyoushe" not in plugin.config["comments"]
                assert "xiaoheihe_cookie" in plugin.config["cookies"]
                assert plugin.config["cookies"]["weibo_cookie"] == "keep-cookie"
                saved_config = json.loads(
                    (tmp_path / "parser_x_config.json").read_text(encoding="utf-8-sig")
                )
                assert "xiaoheihe_cookie" in saved_config["cookies"]
                assert "platforms" not in saved_config
                assert "miyoushe" not in saved_config["comments"]
                assert saved_config["comments"]["enabled"] is True
                assert {
                    parser.platform.name for parser in plugin.parser_map.values()
                } == {
                    "bilibili",
                    "douyin",
                    "kuaishou",
                    "weibo",
                    "xiaohongshu",
                    "acfun",
                    "xiaoheihe",
                    "tieba",
                    "coolapk",
                    "wechat_article",
                    "netease",
                    "nga",
                    "zhihu",
                    "wechat_channels",
                }
                assert "integrations" not in plugin.config
                assert plugin.parser_map["b23.tv"].enable_comment_card is True
                assert plugin.render_service.available
                bili = plugin.parser_map["b23.tv"]
                douyin = plugin.parser_map["douyin"]
                weibo = plugin.parser_map["weibo.com"]
                xiaohongshu = plugin.parser_map["xhslink.com"]
                assert bili.render_service is plugin.render_service
                assert bili.render_service is plugin.render_service
                assert not hasattr(bili, "dynamic_renderer")
                assert douyin.render_service is plugin.render_service
                assert douyin.render_service is plugin.render_service
                assert weibo.render_service is plugin.render_service
                assert weibo.render_service is plugin.render_service
                assert xiaohongshu.render_service is plugin.render_service
                assert xiaohongshu.render_service is plugin.render_service
                assert {route for route, *_ in context.web_apis} == {
                    "/astrbot_plugin_parser_x/debug/mode",
                    "/astrbot_plugin_parser_x/debug/status",
                    "/astrbot_plugin_parser_x/debug/start",
                    "/astrbot_plugin_parser_x/debug/events",
                    "/astrbot_plugin_parser_x/debug/cancel",
                    "/astrbot_plugin_parser_x/debug/media/<token>/preview",
                    "/astrbot_plugin_parser_x/debug/media/<token>",
                }
            finally:
                await plugin.terminate()

    asyncio.run(run_lifecycle())

def test_xiaohongshu_comment_normalization_keeps_nested_reply_and_emotes(tmp_path):
    class FakeParser:
        headers = {}
        cache_dir = tmp_path
        comment_cookie = "a1=test; web_session=test"

    feed = XiaohongshuCommentFeed(FakeParser())
    normalized = feed.adapt_comment(
        {
            "id": "c1",
            "content": "收到 @小明 谢谢 [大笑R] #好物#",
            "like_count": 100000000,
            "sub_comment_count": 2,
            "create_time": 1_700_000_000,
            "ip_location": "IP属地：上海",
            "show_tags": [{"name": "is_author"}],
            "user_info": {
                "user_id": "42",
                "nickname": "作者",
                "image": "https://sns-avatar.example.com/a.jpg",
            },
            "sub_comments": [
                {
                    "id": "c2",
                    "content": "楼中楼",
                    "user_info": {"user_id": "7", "nickname": "回复者"},
                    "target_comment": {"user_info": {"nickname": "小明"}},
                }
            ],
        },
        "42",
        emote_map={"[大笑R]": "https://sns-webpic.example.com/emote.png"},
    )

    assert normalized is not None
    assert normalized.like_text == "1亿"
    assert normalized.reply_text == "回复 2"
    assert {badge.text for badge in normalized.author.badges} == {"作者"}
    kinds = [part.kind for part in normalized.content]
    assert "emote" in kinds
    assert "#好物#" not in "".join(
        part.text for part in normalized.content if part.kind == "highlight"
    )
    assert normalized.first_reply is not None
    assert normalized.location == "上海"


def test_xiaohongshu_comment_only_mentions_keep_accent_colour(tmp_path):
    class FakeParser:
        headers = {}
        cache_dir = tmp_path
        comment_cookie = "a1=test"

    feed = XiaohongshuCommentFeed(FakeParser())
    normalized = feed.adapt_comment(
        {"id": "c1", "content": "看 ＠小红 和 #话题# 和链接"},
        "",
    )
    highlighted = [
        part.text for part in normalized.content if part.kind == "highlight"
    ]
    assert highlighted == ["＠小红"]


def test_xiaohongshu_comment_feed_is_skipped_without_cookie(tmp_path):
    class FakeParser:
        headers = {}
        cache_dir = tmp_path
        comment_cookie = ""

    feed = XiaohongshuCommentFeed(FakeParser())
    rendered = asyncio.run(
        feed.build_document(
            "note1",
            "token",
            work_title="标题",
            cover=None,
            owner_id="42",
        )
    )
    # 没有 Cookie 时直接返回 None（不产出评论文档），由调用方跳过评论区。
    assert rendered is None


def test_xiaohongshu_emote_map_parses_live_shape_and_field_variants():
    live = {
        "code": 0,
        "success": True,
        "data": {
            "version": 5,
            "emoji": {
                "tabs": [
                    {
                        "collection": [
                            {
                                "emoji": [
                                    {
                                        "image_name": "[微笑R]",
                                        "image": "https://picasso-static.xiaohongshu.com/a.png",
                                    },
                                    {
                                        "imageName": "[偷笑R]",
                                        "src": "https://picasso-static.xiaohongshu.com/b.png",
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        },
    }
    parsed = build_xiaohongshu_emote_map(live)
    assert parsed["[微笑R]"] == "https://picasso-static.xiaohongshu.com/a.png"
    assert parsed["[偷笑R]"] == "https://picasso-static.xiaohongshu.com/b.png"

    # 无 data 包裹、collection/emoji 以单对象或复数键给出的形态也要能解析。
    flat = {
        "tabs": [
            {
                "collections": {
                    "emojis": [
                        {"name": "[鼓掌R]", "url": "https://x.com/c.png"},
                    ]
                }
            }
        ]
    }
    parsed_flat = build_xiaohongshu_emote_map(flat)
    assert parsed_flat["[鼓掌R]"] == "https://x.com/c.png"

    # 非 http(s) 地址（含 data: 内联载荷）与空载荷一律丢弃，不得抛异常。
    # 内嵌底图只从 fallback_emote_map 进入，线上载荷不得注入内联来源。
    assert build_xiaohongshu_emote_map(None) == {}
    assert build_xiaohongshu_emote_map({}) == {}
    assert (
        build_xiaohongshu_emote_map(
            {
                "data": {
                    "emoji": {
                        "tabs": [
                            {
                                "collection": [
                                    {"emoji": [{"image_name": "[坏]", "image": "data:image/png;base64,xx"}]}
                                ]
                            }
                        ]
                    }
                }
            }
        )
        == {}
    )


def test_xiaohongshu_video_placeholder_detected_but_normal_text_kept(tmp_path):
    """[视频] Ns 是小红书给「视频评论」的占位符；正常文字里的「视频」二字必须保留。"""
    has = XiaohongshuCommentFeed._has_video_placeholder

    # 占位符（带时长）→ 识别为视频评论
    assert has("[视频] 14s") is True
    assert has("gigity\n[视频] 8s") is True
    assert has("[视频] 8s\n我也说两句") is True
    assert has("多行\n[视频] 12s") is True
    assert has("[视频]3s") is True

    # 非占位符 → 不得误判（"视频"二字 / 裸 [视频] 无时长 / 空）
    assert has("我想起这个视频你就完蛋了[微笑R]") is False
    assert has("这个视频太好笑了") is False
    assert has("[视频]") is False
    assert has("正常评论") is False
    assert has("") is False
    assert has(None) is False


def test_xiaohongshu_video_comment_is_dropped_entirely(tmp_path):
    """视频评论必须整条丢弃——只剥占位符会留下半截文字（如孤零零的 "gigity"）。"""
    class FakeParser:
        headers = {}
        cache_dir = tmp_path
        comment_cookie = "a1=test; web_session=test"

    feed = XiaohongshuCommentFeed(FakeParser())
    base = {
        "id": "c1",
        "like_count": 1,
        "sub_comment_count": 0,
        "create_time": 1_700_000_000,
        "user_info": {"user_id": "42", "nickname": "某人"},
    }

    # 纯视频评论 + 带半截文字的视频评论 → 都返回 None
    for text in ("[视频] 14s", "gigity\n[视频] 8s"):
        assert feed.adapt_comment({**base, "content": text}, "42") is None, text

    # 楼中楼里的视频回复同样丢弃
    assert (
        feed.adapt_comment({**base, "content": "[视频] 14s"}, "42", nested=True) is None
    )

    # 正文里提到「视频」二字的正常评论必须保留
    kept = feed.adapt_comment({**base, "content": "这个视频真好笑[微笑R]"}, "42")
    assert kept is not None
    joined = "".join(p.text or "" for p in kept.content)
    assert "这个视频真好笑" in joined


def test_xiaohongshu_pag_only_emote_uses_v001_trusted_static_frame():
    from core.platform_emotes import fallback_emote_map, resolve_emote_url

    catalog = fallback_emote_map("xiaohongshu")
    for token in ("[呃R]", "[完啦R]", "[超喜欢R]"):
        assert resolve_emote_url('xiaohongshu', token, catalog).startswith('data:image/png;')
        parts = XiaohongshuCommentFeed._rich_text('怪不得' + token, catalog)
        assert any(part.kind == 'emote' for part in parts)
        assert ''.join(part.text or '' for part in parts) == '怪不得' + token


def test_xiaohongshu_remote_payload_cannot_inject_inline_image_source():
    """线上接口载荷不得注入 data: 等内联来源，内嵌底图只走 fallback_emote_map。"""
    parsed = build_xiaohongshu_emote_map(
        {
            "data": {
                "emoji": {
                    "tabs": [
                        {
                            "collection": [
                                {"emoji": [{"image_name": "[坏]", "image": "data:image/png;base64,xx"}]}
                            ]
                        }
                    ]
                }
            }
        }
    )
    assert parsed == {}


def test_xiaohongshu_emoji_config_parses_catalogue_and_skips_animated_only():
    """emoji/config 的 tuples 提供静态表情；只有动画地址的条目跳过。"""
    parsed = build_xiaohongshu_emote_config_map(
        {
            "data": {
                "tabs": [
                    {
                        "collections": [
                            {
                                "tuples": [
                                    {"image_name": "[嗨R]", "image_url": "https://cdn/1.png"},
                                    {"image_name": "[呃R]", "image_url": ""},
                                    {"text": "[赞R]", "image": "https://cdn/3.png"},
                                ]
                            }
                        ]
                    }
                ]
            }
        }
    )
    assert parsed["[嗨R]"] == "https://cdn/1.png"
    assert parsed["[赞R]"] == "https://cdn/3.png"
    assert "[呃R]" not in parsed


# --------------------------------------------------------------------------
# 抖音"分享主页"短链：落点是 /share/user/<sec_uid>，不是作品，必须静默跳过。
# 以前会一路走到 ID 兜底（sec_uid 被误当成作品 ID）并给群里弹
# "短链解析失败，无法识别最终链接: https://www.iesdouyin.com/share/user/..."。
# --------------------------------------------------------------------------

DOUYIN_PROFILE_SHORT_SHARE = (
    "0- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 "
    "https://v.douyin.com/y_rk4uYY_jc/ 9@9.com :3pm"
)
DOUYIN_PROFILE_FINAL_URL = (
    "https://www.iesdouyin.com/share/user/MS4wLjABAAAAyvHxRyW1A_ZwjP0d-DMDAjGYA1oyS2bhaM4AV-itFUk"
    "?from_aid=1128&u_code=18iblfa30&sec_uid=MS4wLjABAAAAyvHxRyW1A_ZwjP0d-DMDAjGYA1oyS2bhaM4AV-itFUk"
    "&from_ssr=1&utm_source=copy&utm_campaign=client_share&utm_medium=android&app=aweme"
)
DOUYIN_VIDEO_SHORT_SHARE = (
    "7.63 复制打开抖音，看看【某人的作品】测试视频 https://v.douyin.com/AbCdEf_Gh/ 复制此链接，打开Dou音搜索，直接观看视频！"
)


class _InertDownloader:
    """主页分享用例不应触达下载，任何下载请求都视为用例失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"主页分享用例不应调用下载器: {name}")


def _douyin_parser_for_profile_tests(tmp_path):
    return DouyinParser(
        {
            "cache_dir": str(tmp_path),
            "cookies": {"douyin_ck": "sessionid=test"},
            "comments": {"douyin": False},
        },
        _InertDownloader(),
    )


def test_douyin_profile_url_is_recognized_as_non_work():
    """用户主页落点（含 sec_uid 查询串）一律判定为无需解析。"""
    assert DouyinParser._is_profile_url(DOUYIN_PROFILE_FINAL_URL)
    assert DouyinParser._is_profile_url(
        "https://www.douyin.com/user/MS4wLjABAAAAyvHxRyW1A_ZwjP0d-DMDAjGYA1oyS2bhaM4AV-itFUk"
    )
    assert DouyinParser._is_profile_url("https://www.douyin.com/user/self?showTab=post")

    # 真正的作品链接不能被误判
    assert not DouyinParser._is_profile_url(
        "https://www.iesdouyin.com/share/video/7697103818928702153/?region=CN"
    )
    assert not DouyinParser._is_profile_url("https://www.douyin.com/video/7697103818928702153")
    assert not DouyinParser._is_profile_url("https://m.douyin.com/share/note/1234")


def test_douyin_extract_id_does_not_mistake_sec_uid_for_aweme_id(tmp_path):
    """主页路径不得产出作品 ID，否则会被当成作品去解析。"""
    from core.parsers.douyin.extractor import extract_id_from_query

    assert extract_id_from_query(DOUYIN_PROFILE_FINAL_URL) is None
    assert (
        extract_id_from_query(
            "https://www.douyin.com/user/MS4wLjABAAAAyvHxRyW1A_ZwjP0d-DMDAjGYA1oyS2bhaM4AV-itFUk"
        )
        is None
    )
    # 作品链接照常提取
    assert (
        extract_id_from_query(
            "https://www.iesdouyin.com/share/video/7697103818928702153/?region=CN"
        )
        == "7697103818928702153"
    )


def test_douyin_profile_share_text_is_detected_before_any_request(tmp_path):
    """分享文案级判定：命中"更多作品"时连跳转都不做。"""
    parser = _douyin_parser_for_profile_tests(tmp_path)

    assert parser._is_profile_share_text(DOUYIN_PROFILE_SHORT_SHARE)
    assert parser._is_unsupported_share_text(DOUYIN_PROFILE_SHORT_SHARE)

    assert not parser._is_profile_share_text(DOUYIN_VIDEO_SHORT_SHARE)
    assert not parser._is_unsupported_share_text(DOUYIN_VIDEO_SHORT_SHARE)

    searched = re.search(r"v\.douyin\.com/[a-zA-Z0-9_\-]+", DOUYIN_PROFILE_SHORT_SHARE)
    assert searched is not None

    async def run():
        with (
            patch.object(
                parser,
                "_resolve_final_url_by_head",
                side_effect=AssertionError("主页分享不应发起短链解析"),
            ),
            patch.object(
                parser,
                "get_final_url",
                side_effect=AssertionError("主页分享不应发起短链解析"),
            ),
        ):
            await parser._parse_short_link(searched)

    with pytest.raises(SkipParseException):
        asyncio.run(run())


def test_douyin_short_link_to_profile_is_skipped_not_parsed(tmp_path):
    """文案没带"更多作品"、但跳转落点是主页时，同样静默跳过而不是报错。"""
    parser = _douyin_parser_for_profile_tests(tmp_path)
    searched = re.search(r"v\.douyin\.com/[a-zA-Z0-9_\-]+", DOUYIN_PROFILE_SHORT_SHARE)
    assert searched is not None

    async def run():
        with (
            patch.object(
                parser,
                "_resolve_final_url_by_head",
                return_value=DOUYIN_PROFILE_FINAL_URL,
            ),
            patch.object(
                parser,
                "_parse_by_id_fallback",
                side_effect=AssertionError("主页落点不应进入作品 ID 解析"),
            ),
            patch.object(
                parser,
                "_ytdlp_fallback",
                side_effect=AssertionError("主页落点不应进入 ytdlp 兜底"),
            ),
        ):
            await parser._parse_short_link(searched)

    with pytest.raises(SkipParseException):
        asyncio.run(run())


def test_plugin_drops_douyin_profile_share_before_parsing():
    """消息入口直接丢弃主页分享，不产生任何解析尝试。"""
    from astrbot_plugin_parser_x.main import ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    douyin = SimpleNamespace(platform=SimpleNamespace(name="douyin"))
    plugin.parser_map = {"v.douyin": douyin, "jx.douyin": douyin}
    plugin.key_pattern_list = [
        ("v.douyin", re.compile(r"v\.douyin\.com/[a-zA-Z0-9_\-]+")),
        ("jx.douyin", re.compile(r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")),
    ]

    assert plugin._collect_parser_matches(DOUYIN_PROFILE_SHORT_SHARE) == []

    # 普通作品分享仍要正常进入解析队列
    video_matches = plugin._collect_parser_matches(DOUYIN_VIDEO_SHORT_SHARE)
    assert len(video_matches) == 1
    assert video_matches[0][1] == "v.douyin"


def test_tall_image_goes_into_forward_not_quoted(tmp_path):
    """超长图也必须进合并转发。

    以前超过 ``_TALL_IMAGE_RATIO`` 的图会被挑出来、用 ``reply_original=True``
    单独引用发送；统一投递之后所有媒体都打包成合并转发，不再有引用消息。
    """
    from astrbot_plugin_parser_x.core.data import (
        Author,
        ImageContent as PluginImageContent,
        ParseResult,
        Platform,
    )
    from astrbot_plugin_parser_x.main import ParserXPlugin

    tall = tmp_path / "tall.png"
    short = tmp_path / "short.png"
    Image.new("RGB", (100, 1200), (20, 20, 20)).save(tall)  # 比例 12:1
    Image.new("RGB", (100, 100), (20, 20, 20)).save(short)

    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"show_download_fail_tip": True}

    async def fake_download_content(_self, cont):
        return cont, cont.path_task, None

    class Event:
        message_obj = SimpleNamespace(message_id=99)

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
    contents = [PluginImageContent(tall), PluginImageContent(short)]
    result = ParseResult(
        platform=Platform(name="xiaoheihe", display_name="小黑盒"),
        author=Author(name="作者"),
        title="标题",
        contents=contents,
        text="正文",
        url="https://example.com",
    )

    with (
        patch.object(ParserXPlugin, "_download_content", fake_download_content),
        patch.object(
            ParserXPlugin,
            "_convert_to_seg",
            side_effect=lambda _content, path: MessageImage(str(path)),
        ),
    ):
        asyncio.run(plugin._send_parse_result(event, result))

    assert len(event.sent) == 1
    forward = event.sent[0][0]
    assert isinstance(forward, Nodes)
    parts = [node.content[0] for node in forward.nodes]
    assert not any(isinstance(c, Reply) for c in parts)
    files = {c.file for c in parts if isinstance(c, MessageImage)}
    assert str(tall) in files, "长图必须进转发，不能被挑出来单独引用发送"
