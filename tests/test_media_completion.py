from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image, Nodes, Video
from astrbot_plugin_parser_x.core.data import (
    DynamicContent,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from astrbot_plugin_parser_x.core.exception import ParseException
from astrbot_plugin_parser_x.core.parsers import (
    BilibiliParser,
    DouyinParser,
    WeiboParser,
    XiaoHongShuParser,
)
from astrbot_plugin_parser_x.core.parsers.bilibili.dynamic_service import (
    BiliDynamicService,
)
from astrbot_plugin_parser_x.core.parsers.gallery import ordered_media_plan
from astrbot_plugin_parser_x.main import ParserXPlugin


class Downloads:
    def __init__(self, root):
        self.root, self.calls, self.tasks = root, [], []

    def start(self, kind, url):
        path = self.root / f"{len(self.calls)}.{kind}"
        self.calls.append((kind, url))

        async def download():
            await asyncio.sleep(0)
            if "broken" in url:
                raise RuntimeError("download unavailable")
            path.write_bytes(b"fixture-media")
            return path

        task = asyncio.create_task(download())
        self.tasks.append(task)
        return task

    def download_img(self, url, **kwargs):
        return self.start("jpg", url)

    def download_video(self, url, **kwargs):
        return self.start("mp4", url)

    async def finish(self):
        await asyncio.gather(*self.tasks, return_exceptions=True)


def config(root):
    return {"cache_dir": str(root), "comments": {"douyin": True, "weibo": True}}


def xhs_note(discovery=False, broken=False):
    return {
        "type": "normal",
        "title": "gallery",
        "desc": "body",
        "user": {"nickName" if discovery else "nickname": "author"},
        "imageList": [
            {"urlDefault": "https://example.com/first.jpg"},
            {
                "urlDefault": "https://example.com/still.jpg",
                "livePhoto": True,
                "stream": {
                    "h264": [
                        {
                            "masterUrl": "https://example.com/"
                            + ("broken.mp4" if broken else "live.mp4")
                        }
                    ]
                },
            },
            {"urlDefault": "https://example.com/last.jpg"},
        ],
    }


class Event:
    message_obj = SimpleNamespace(message_id="original")

    def __init__(self):
        self.sent = []

    def get_sender_id(self):
        return "42"

    def get_sender_name(self):
        return "user"

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return text

    async def send(self, chain):
        self.sent.append(chain)


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("broken", [False, True])
def test_xhs_live_gallery_delivers_in_order_and_falls_back_to_still(
    tmp_path, discovery, broken
):
    async def run():
        downloads = Downloads(tmp_path)
        parser = XiaoHongShuParser(config(tmp_path), downloads)
        note = xhs_note(discovery, broken)
        result = (
            parser._process_discovery_data(note, {})
            if discovery
            else parser._process_explore_data(note)
        )
        assert [type(c) for c in result.contents] == [
            ImageContent,
            DynamicContent,
            ImageContent,
        ]
        assert result.has_video is False
        assert "comment_image_task_factory" not in result.extra
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}
        event = Event()
        await plugin._send_parse_result(event, result)
        assert len(event.sent) == 1
        nodes = event.sent[0][0].nodes
        assert [type(n.content[0]) for n in nodes] == [
            Image,
            Image if broken else Video,
            Image,
        ]
        assert Path(nodes[0].content[0].file).name == "0.jpg"
        assert Path(nodes[1].content[0].file).name == ("1.jpg" if broken else "2.mp4")
        assert Path(nodes[2].content[0].file).name == "3.jpg"
        await downloads.finish()

    asyncio.run(run())


@pytest.mark.parametrize("layout", ["images", "image_post_info", "slides"])
@pytest.mark.parametrize("video_field", ["play_addr", "bit_rate"])
def test_douyin_live_variants_keep_order_and_do_not_fetch_video_comments(
    tmp_path, layout, video_field
):
    async def run():
        downloads = Downloads(tmp_path)
        parser = DouyinParser(config(tmp_path), downloads)
        parser.cookies = "fixture"
        video = (
            {"play_addr": {"url_list": ["https://example.com/live.mp4"]}}
            if video_field == "play_addr"
            else {
                "bit_rate": [
                    {"play_addr": {"url_list": ["https://example.com/live.mp4"]}}
                ]
            }
        )
        items = [
            {"url_list": ["https://example.com/first.jpg"]},
            {
                "display_image": {"url_list": ["https://example.com/still.jpg"]},
                "video": video,
            },
            {"url_list": ["https://example.com/last.jpg"]},
        ]
        payload = {
            "aweme_id": "123",
            "desc": "gallery",
            "create_time": 1700000000,
            "author": {"nickname": "author", "avatar_thumb": {"url_list": []}},
        }
        if layout == "image_post_info":
            payload[layout] = {"images": items}
        else:
            payload["images"] = items
        if layout == "slides":
            import json

            parser.http_get = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    content=json.dumps({"aweme_details": [payload]}).encode(),
                )
            )
            result = await parser.parse_slides("123")
        else:
            result = parser._build_result_from_aweme(payload, "123")
        assert [type(c) for c in result.contents] == [
            ImageContent,
            DynamicContent,
            ImageContent,
        ]
        assert result.delivery.batches[0].parts == result.contents
        assert "comment_image_task_factory" not in result.extra
        assert [url for kind, url in downloads.calls if kind == "mp4"] == [
            "https://example.com/live.mp4"
        ]
        await downloads.finish()

    asyncio.run(run())


@pytest.mark.parametrize("still", [None, "https://example.com/still.jpg"])
def test_live_photo_with_only_alternate_stream_is_not_dropped(tmp_path, still):
    async def run():
        downloads = Downloads(tmp_path)
        parser = XiaoHongShuParser(config(tmp_path), downloads)
        note = xhs_note()
        note["imageList"] = [
            {
                "urlDefault": still,
                "livePhoto": True,
                "stream": {
                    "h265": [
                        {"masterUrl": "", "backupUrls": ["//example.com/live.mp4"]}
                    ]
                },
            }
        ]
        result = parser._process_explore_data(note)
        assert len(result.contents) == 1
        assert isinstance(result.contents[0], DynamicContent)
        assert ("mp4", "https://example.com/live.mp4") in downloads.calls
        await downloads.finish()

    asyncio.run(run())


@pytest.mark.parametrize("reposted", [False, True])
@pytest.mark.parametrize("use_bvid", [False, True])
def test_bilibili_dynamic_downloads_archive_video_and_reuses_video_pipeline(
    tmp_path, reposted, use_bvid
):
    async def run():
        parser = object.__new__(BilibiliParser)
        video = VideoContent(tmp_path / "video.mp4")
        factory = AsyncMock(return_value=[])
        parser.parse_video = AsyncMock(
            return_value=ParseResult(
                parser.platform,
                contents=[video],
                extra={"comment_image_task_factory": factory},
            )
        )
        item = {
            "modules": {
                "module_dynamic": {
                    "desc": {"text": "original"},
                    "major": {
                        "archive": {"bvid": "BV1xx411c7mD"}
                        if use_bvid
                        else {"aid": "123456"},
                    },
                }
            }
        }
        if reposted:
            item = {
                "orig": deepcopy(item),
                "modules": {"module_dynamic": {"desc": {"text": "reposted"}}},
            }
        service = BiliDynamicService(parser)
        service._fetch_dynamic_raw = AsyncMock(return_value={"item": item})
        result = await service.parse_dynamic(12345)
        parser.parse_video.assert_awaited_once_with(
            **({"bvid": "BV1xx411c7mD"} if use_bvid else {"avid": 123456})
        )
        assert result.contents == [video]
        assert result.extra["comment_image_task_factory"] is factory
        assert result.url == "https://t.bilibili.com/12345"
        if reposted:
            assert "reposted" in result.text and "original" in result.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "https://video.weibo.com/show?fid=1034:123456",
        "https://video.weibo.com/show?other=value&fid=1034:123456",
        "https://h5.video.weibo.com/show/1034:123456",
        "https://weibo.com/tv/show/1034:123456",
    ],
)
def test_weibo_fid_entry_resolves_to_actual_video(tmp_path, url):
    async def run():
        downloads = Downloads(tmp_path)
        parser = WeiboParser(config(tmp_path), downloads)
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "data": {
                    "Component_Play_Playinfo": {
                        "title": "video",
                        "text": "body",
                        "reward": {"user": {"name": "author"}},
                        "urls": {"高清 1080P": "//example.com/clip.mp4"},
                        "duration_time": 4.9,
                    }
                }
            },
        )
        parser._session = SimpleNamespace(post=AsyncMock(return_value=response))
        key, matched = parser.search_url(url)
        result = await parser.parse(key, matched)
        assert len(result.contents) == 1 and isinstance(
            result.contents[0], VideoContent
        )
        assert ("mp4", "https://example.com/clip.mp4") in downloads.calls
        assert result.contents[0].duration == 4.9
        assert result.author.name == "author"
        parser._session.post.assert_awaited_once()
        await downloads.finish()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload", [{}, {"data": {}}, {"data": {"Component_Play_Playinfo": {}}}]
)
def test_weibo_fid_missing_media_is_a_parse_error(tmp_path, payload):
    async def run():
        parser = WeiboParser(config(tmp_path), Downloads(tmp_path))
        parser._session = SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(status_code=200, json=lambda: payload)
            )
        )
        key, match = parser.search_url("https://video.weibo.com/show?fid=1034:123456")
        with pytest.raises(ParseException):
            await parser.parse(key, match)

    asyncio.run(run())


def test_weibo_article_path_does_not_become_a_fake_status_id():
    with pytest.raises(ParseException):
        WeiboParser.search_url("https://weibo.com/ttarticle/p/show?id=123456")


def test_weibo_app_share_redirects_into_supported_parser(tmp_path):
    async def run():
        parser = WeiboParser(config(tmp_path), Downloads(tmp_path))
        parser.get_redirect_url = AsyncMock(
            return_value="https://m.weibo.cn/detail/123456"
        )
        parser._fetch_status_data = AsyncMock(
            return_value={
                "id": "123456",
                "text": "body",
                "user": {"screen_name": "author"},
            }
        )
        key, match = parser.search_url("https://mapp.api.weibo.cn/fx/abcdef.html")
        result = await parser.parse(key, match)
        assert result.text == "body"
        parser._fetch_status_data.assert_awaited_once_with("123456")

    asyncio.run(run())


@pytest.mark.parametrize("mixed", [False, True])
def test_weibo_live_photo_and_multiple_videos_keep_source_order(tmp_path, mixed):
    async def run():
        downloads = Downloads(tmp_path)
        parser = WeiboParser(config(tmp_path), downloads)
        picture = {"type": "pic", "largest": {"url": "https://example.com/first.jpg"}}
        live = {
            "type": "livephoto",
            "largest": {"url": "https://example.com/still.jpg"},
            "video": "https://example.com/live.mp4",
        }
        data = {
            "id": "123456",
            "user": {"id": "123", "screen_name": "author"},
            "text": "body",
        }
        if mixed:
            data["mix_media_info"] = {
                "items": [
                    {"type": "pic", "data": picture},
                    {
                        "type": "video",
                        "data": {
                            "media_info": {
                                "stream_url_hd": "https://example.com/regular1.mp4"
                            }
                        },
                    },
                    {"type": "pic", "data": live},
                    {
                        "type": "video",
                        "data": {
                            "media_info": {
                                "playback_list": [
                                    {
                                        "play_info": {
                                            "url": "https://example.com/regular2.mp4"
                                        }
                                    }
                                ]
                            }
                        },
                    },
                ]
            }
        else:
            data["pic_ids"] = ["first", "live"]
            data["pic_infos"] = {"live": live, "first": picture}
        parser._fetch_status_data = AsyncMock(return_value=data)
        key, match = parser.search_url("https://weibo.com/123/Abc123")
        result = await parser.parse(key, match)
        expected = (
            [ImageContent, VideoContent, DynamicContent, VideoContent]
            if mixed
            else [ImageContent, DynamicContent]
        )
        assert [type(c) for c in result.contents] == expected
        assert result.delivery.batches[0].parts == result.contents
        assert bool(result.extra.get("comment_image_task_factory")) is mixed
        await downloads.finish()

    asyncio.run(run())


def test_forward_failure_and_one_video_send_failure_do_not_stop_the_album(tmp_path):
    async def run():
        downloads = Downloads(tmp_path)
        contents = [
            ImageContent(downloads.download_img("https://example.com/first.jpg")),
            DynamicContent(downloads.download_video("https://example.com/clip.mp4")),
            ImageContent(downloads.download_img("https://example.com/last.jpg")),
        ]
        result = ParseResult(
            Platform("demo", "demo"),
            contents=contents,
            delivery=ordered_media_plan(contents),
            extra={"native_delivery": True},
        )

        class FailingForwardEvent(Event):
            async def send(self, chain):
                if isinstance(chain[0], Nodes):
                    raise RuntimeError("forward unavailable")
                self.sent.append(chain)

        event = FailingForwardEvent()
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}
        plugin._send_video_segment = AsyncMock(
            side_effect=RuntimeError("video upload unavailable")
        )
        await plugin._send_parse_result(event, result)
        assert [Path(chain[0].file).name for chain in event.sent] == ["0.jpg", "2.jpg"]
        await downloads.finish()

    asyncio.run(run())


def test_share_tokens_do_not_leak_into_fresh_parser_instances(tmp_path):
    first = XiaoHongShuParser(config(tmp_path), Downloads(tmp_path))
    first.source_text = "xsec_token=private-token"
    second = XiaoHongShuParser(config(tmp_path), Downloads(tmp_path))
    assert second.source_text == ""


def test_weibo_cookie_uses_web_media_metadata(tmp_path):
    async def run():
        parser = WeiboParser(
            {**config(tmp_path), "cookies": {"weibo_cookie": "SUB=fixture"}},
            Downloads(tmp_path),
        )
        data = {
            "id": 123,
            "text_raw": "body",
            "user": {"screen_name": "author"},
            "pic_ids": ["one"],
            "pic_infos": {
                "one": {"type": "livephoto", "video": "https://example.com/live.mp4"}
            },
        }
        get = AsyncMock(
            return_value=SimpleNamespace(status_code=200, json=lambda: data)
        )
        parser._session = SimpleNamespace(get=get)
        assert await parser._fetch_status_data("123") == data
        get.assert_awaited_once()
        assert get.call_args.args[0] == "https://weibo.com/ajax/statuses/show"
        assert get.call_args.kwargs["headers"]["Cookie"] == "SUB=fixture"

    asyncio.run(run())


def test_missing_live_clip_without_cover_keeps_remaining_images(tmp_path):
    async def run():
        downloads = Downloads(tmp_path)
        contents = [
            ImageContent(downloads.download_img("https://example.com/first.jpg")),
            DynamicContent(downloads.download_video("https://example.com/broken.mp4")),
            ImageContent(downloads.download_img("https://example.com/last.jpg")),
        ]
        result = ParseResult(
            Platform("demo", "demo"),
            contents=contents,
            delivery=ordered_media_plan(contents),
            extra={"native_delivery": True},
        )
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {"behavior": {"show_download_fail_tip": False}}
        event = Event()
        await plugin._send_parse_result(event, result)
        assert [Path(n.content[0].file).name for n in event.sent[0][0].nodes] == [
            "0.jpg",
            "2.jpg",
        ]
        await downloads.finish()

    asyncio.run(run())
