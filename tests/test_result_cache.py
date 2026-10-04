from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from astrbot.api.message_components import Image, Reply
from astrbot_plugin_parser_x import main as main_module
from astrbot_plugin_parser_x.core.data import (
    Author,
    DeliveryBatch,
    DeliveryPlan,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from astrbot_plugin_parser_x.core.exception import ParseException, SkipParseException
from astrbot_plugin_parser_x.core.result_cache import CacheSettings, ParseResultCache
from astrbot_plugin_parser_x.main import ParserXPlugin


class Parser:
    platform = Platform("demo", "演示平台")

    def __init__(self, build=None):
        self.calls = 0
        self.build = build

    async def parse(self, keyword, searched):
        self.calls += 1
        if self.build:
            return await self.build()
        return ParseResult(self.platform, text=searched.group())


async def lookup(cache, parser, url="https://example.com/1", *, text=None, config=None):
    return await cache.parse(
        parser,
        "example.com",
        re.search(r".+", url),
        text if text is not None else url,
        config if config is not None else {},
    )


def test_cache_copies_mutable_results_and_preserves_delivery_aliases(tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")

    async def run():
        async def build():
            image = ImageContent(path)
            return ParseResult(
                Parser.platform,
                contents=[image],
                delivery=DeliveryPlan([DeliveryBatch([image])]),
                extra={"nested": ["original"]},
            )

        parser, cache = Parser(build), ParseResultCache()
        first = await lookup(cache, parser)
        first.extra["nested"].append("changed")
        first.contents.clear()
        second = await lookup(cache, parser)
        assert parser.calls == 1
        assert second.extra == {"nested": ["original"]}
        assert second.contents[0] is second.delivery.batches[0].parts[0]
        assert first.delivery is not second.delivery
        await cache.close()

    asyncio.run(run())


def test_ttl_is_fixed_and_capacity_evicts_least_recently_used():
    async def run():
        now = [0]
        cache, parser = ParseResultCache(clock=lambda: now[0]), Parser()
        config = {"result_cache": {"ttl": 15, "max_entries": 2}}

        async def get(url):
            return await lookup(cache, parser, url, config=config)

        await get("one")
        now[0] = 10
        await get("two")
        await get("one")
        await get("three")
        assert parser.calls == 3
        await get("two")  # least recently used was evicted
        assert parser.calls == 4
        await get("three")
        now[0] = 25
        await get("three")  # a hit did not extend its deadline
        assert parser.calls == 5
        await cache.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [ParseException, SkipParseException, RuntimeError])
def test_parse_failures_can_be_retried_immediately(failure):
    async def run():
        async def build():
            raise failure("temporary failure")

        cache, parser = ParseResultCache(), Parser(build)
        for _ in range(2):
            with pytest.raises(failure):
                await lookup(cache, parser)
        assert parser.calls == 2
        await cache.close()

    asyncio.run(run())


@pytest.mark.parametrize("invalidate", ["delete", "empty", "failed", "cancelled"])
def test_invalid_media_causes_reparse(tmp_path, invalidate):
    async def run():
        path = tmp_path / "image.jpg"
        path.write_bytes(b"image")
        media = asyncio.get_running_loop().create_future()

        async def build():
            return ParseResult(Parser.platform, contents=[ImageContent(media)])

        cache, parser = ParseResultCache(), Parser(build)
        await lookup(cache, parser)
        await lookup(cache, parser)
        assert parser.calls == 1  # pending downloads do not block the first result
        if invalidate == "failed":
            media.set_exception(RuntimeError("download failed"))
        elif invalidate == "cancelled":
            media.cancel()
        else:
            media.set_result(path)
            if invalidate == "delete":
                path.unlink()
            else:
                path.write_bytes(b"")
        await lookup(cache, parser)
        assert parser.calls == 2
        await cache.close()

    asyncio.run(run())


def test_simultaneous_requests_share_parse_but_each_receives_a_copy():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def build():
            started.set()
            await release.wait()
            return ParseResult(Parser.platform, text="ready")

        cache, parser = ParseResultCache(), Parser(build)
        requests = [asyncio.create_task(lookup(cache, parser)) for _ in range(3)]
        await started.wait()
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        release.set()
        results = await asyncio.gather(*requests[1:])
        assert parser.calls == 1
        assert results[0] is not results[1]
        assert results[0].text == results[1].text == "ready"
        await cache.close()

    asyncio.run(run())


def test_last_waiter_cancel_and_shutdown_cancel_owned_work(tmp_path):
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def build():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        cache, parser = ParseResultCache(), Parser(build)
        request = asyncio.create_task(lookup(cache, parser))
        await started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(cancelled.wait(), 1)
        parser.build = None
        await lookup(cache, parser)
        assert parser.calls == 2

        media = asyncio.create_task(asyncio.Event().wait())

        async def media_result():
            return ParseResult(Parser.platform, contents=[ImageContent(media)])

        parser.build = media_result
        await lookup(
            cache, parser, "media", config={"result_cache": {"max_entries": 1}}
        )
        parser.build = None
        await lookup(
            cache, parser, "other", config={"result_cache": {"max_entries": 1}}
        )
        await cache.close()  # also owns media from evicted entries
        assert media.cancelled()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["media", "cover", "avatar"])
def test_cancelling_one_media_consumer_preserves_other_consumers(tmp_path, kind):
    async def run():
        path = tmp_path / "asset.jpg"
        path.write_bytes(b"asset")
        release = asyncio.Event()

        async def download():
            await release.wait()
            return path

        media = asyncio.create_task(download())

        async def build():
            return ParseResult(
                Parser.platform,
                author=Author("name", avatar=media),
                contents=[ImageContent(media), VideoContent(path, cover=media)],
            )

        cache, parser = ParseResultCache(), Parser(build)
        first, second = await lookup(cache, parser), await lookup(cache, parser)

        def get(result):
            if kind == "media":
                return result.contents[0].get_path()
            if kind == "cover":
                return result.contents[1].get_cover_path()
            return result.author.get_avatar_path()

        one, two = asyncio.create_task(get(first)), asyncio.create_task(get(second))
        await asyncio.sleep(0)
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        release.set()
        assert await two == path
        assert not media.cancelled()
        await cache.close()

    asyncio.run(run())


def test_uncached_media_cancellation_still_stops_its_download():
    async def run():
        download = asyncio.create_task(asyncio.Event().wait())
        consumer = asyncio.create_task(ImageContent(download).get_path())
        await asyncio.sleep(0)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert download.cancelled()

    asyncio.run(run())


def test_different_concurrent_requests_keep_source_text_isolated():
    from astrbot_plugin_parser_x.core.parsers.base import BaseParser

    async def run():
        release = asyncio.Event()

        class ContextParser(Parser):
            source_text = BaseParser.source_text
            _source_text_ctx = BaseParser._source_text_ctx

            async def parse(self, keyword, searched):
                self.calls += 1
                if self.calls == 2:
                    release.set()
                await release.wait()
                return ParseResult(self.platform, text=self.source_text)

        parser, cache = ContextParser(), ParseResultCache()
        results = await asyncio.gather(
            lookup(cache, parser, text="token-a"),
            lookup(cache, parser, text="token-b"),
        )
        assert [result.text for result in results] == ["token-a", "token-b"]
        await cache.close()

    asyncio.run(run())


def test_shutdown_cancels_an_unfinished_parse():
    async def run():
        started = asyncio.Event()

        async def build():
            started.set()
            await asyncio.Event().wait()

        cache, parser = ParseResultCache(), Parser(build)
        request = asyncio.create_task(lookup(cache, parser))
        await started.wait()
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not cache._pending

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["url", "text", "cookie", "config", "parser", "cookie_file"]
)
def test_distinct_parse_inputs_do_not_share_results(tmp_path, change):
    async def run():
        cache, parser = ParseResultCache(), Parser()
        path = tmp_path / "cookies.txt"
        path.write_text("first")
        config = {"cookies": {"bili_ck": "secret", "ytdlp_cookie_file": [str(path)]}}
        url, text = "https://example.com/video?p=1&xsec_token=a+b", "share"
        await lookup(cache, parser, url, text=text, config=config)
        if change == "url":
            url = url.replace("p=1", "p=2")
        elif change == "text":
            text = "share xsec_token=different 直播"
        elif change == "cookie":
            config["cookies"]["bili_ck"] = "new secret"
        elif change == "config":
            config["comments"] = {"bilibili": False}
        elif change == "cookie_file":
            path.write_text("updated cookie contents")
        else:
            parser = Parser()
        await lookup(cache, parser, url, text=text, config=config)
        assert parser.calls == (1 if change == "parser" else 2)
        assert "secret" not in repr(list(cache._entries))
        await cache.close()

    asyncio.run(run())


@pytest.mark.parametrize("with_url", [True, False])
def test_comments_run_afresh_and_live_results_are_not_cached(tmp_path, with_url):
    async def run():
        path = tmp_path / "video.mp4"
        path.write_bytes(b"video")
        comments = AsyncMock(return_value=[])

        async def build():
            return ParseResult(
                Parser.platform,
                contents=[VideoContent(path)],
                extra={"comment_image_task_factory": comments},
            )

        # Real factories are functions; mocks themselves are mutable objects.
        async def factory():
            return await comments()

        async def build_with_factory():
            result = await build()
            result.extra["comment_image_task_factory"] = factory
            return result

        cache, parser = ParseResultCache(), Parser(build_with_factory)
        for _ in range(2):
            result = await lookup(cache, parser)
            await result.extra["comment_image_task_factory"]()
        assert parser.calls == 1
        assert comments.await_count == 2

        async def live():
            return ParseResult(
                Parser.platform,
                url="https://live.bilibili.com/1" if with_url else None,
                text="live" if with_url else "B站直播间未开播",
                extra={} if with_url else {"cache_result": False},
            )

        parser.build = live
        for _ in range(2):
            await lookup(cache, parser, "live")
        assert parser.calls == 3
        await cache.close()

    asyncio.run(run())


def test_disabling_cache_and_changing_config_during_parse():
    async def run():
        cache, parser = ParseResultCache(), Parser()
        await lookup(cache, parser)
        for _ in range(2):
            await lookup(cache, parser, config={"result_cache": {"enabled": False}})
        await lookup(cache, parser)
        assert parser.calls == 4
        started, release = asyncio.Event(), asyncio.Event()

        async def build():
            started.set()
            await release.wait()
            return ParseResult(Parser.platform, text="old config")

        parser.build = build
        old = asyncio.create_task(lookup(cache, parser, "old"))
        await started.wait()
        parser.build = None
        config = {"comments": {"bilibili": False}}
        await lookup(cache, parser, config=config)
        release.set()
        await old
        assert len(cache._entries) == 1
        await cache.close()

    asyncio.run(run())


def test_cache_settings_normalize_invalid_values():
    assert CacheSettings.from_config({"result_cache": []}) == CacheSettings()
    assert CacheSettings.from_config(
        {
            "result_cache": {
                "enabled": "false",
                "ttl": "oops",
                "max_entries": 9999,
            }
        }
    ) == CacheSettings(False, 300, 512)


def test_qq_repeated_shares_send_to_both_events_without_reparsing(tmp_path):
    async def run():
        path = tmp_path / "image.jpg"
        path.write_bytes(b"image")

        async def build():
            return ParseResult(Parser.platform, contents=[ImageContent(path)])

        parser = Parser(build)
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}
        plugin.context = SimpleNamespace(get_config=lambda: {})
        plugin.arbiter = SimpleNamespace(notify=AsyncMock())
        plugin.parser_map = {"example.com": parser}
        plugin.key_pattern_list = [
            ("example.com", re.compile(r"https://example\.com/\d+"))
        ]

        class Event:
            message_str = "https://example.com/1"
            bot = None

            def __init__(self, message_id):
                self.message_obj = SimpleNamespace(message_id=message_id)
                self.unified_msg_origin = f"group:{message_id}"
                self.sent = []

            def get_self_id(self):
                return "bot"

            def get_messages(self):
                return []

            def get_sender_id(self):
                return "42"

            def get_sender_name(self):
                return "user"

            def chain_result(self, chain):
                return chain

            async def send(self, chain):
                self.sent.append(chain)

        first, second = Event(1), Event(2)
        with patch.object(main_module, "AiocqhttpMessageEvent", Event):
            await plugin.on_message(first)
            await plugin.on_message(second)
        assert parser.calls == 1
        for event in (first, second):
            assert len(event.sent) == 1
            assert isinstance(event.sent[0][0], Reply)
            assert str(event.sent[0][0].id) == str(event.message_obj.message_id)
            assert isinstance(event.sent[0][1], Image)
        await plugin.result_cache.close()

    asyncio.run(run())
