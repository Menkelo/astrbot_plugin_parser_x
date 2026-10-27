"""Disabled cards are intentional, not a failed-render summary fallback."""
import asyncio

import pytest
from astrbot.api.message_components import Image, Nodes, Plain, Video
from astrbot_plugin_parser_x.core.data import (
    DeliveryBatch, DeliveryPlan, ImageContent, ParseResult, Platform, VideoContent,
)
from .test_unified_delivery import CaptureEvent, document, forwarded_parts, make_plugin


def delivered_parts(event):
    return [part for chain in event.sent for part in chain if not isinstance(part, Nodes)] + forwarded_parts(event)


@pytest.mark.parametrize("send_text", [False, True])
@pytest.mark.parametrize("body", ["source body", ""])
@pytest.mark.parametrize("warm_cache", [False, True])
@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("kind", ["image", "video", "album", "mixed"])
def test_disabled_cards_do_not_emit_replacement_summary(tmp_path, kind, native, warm_cache, body, send_text):
    async def run():
        plugin = make_plugin(tmp_path)
        path = tmp_path / ("source.mp4" if kind == "video" else "source.jpg")
        path.write_bytes(b"original media")
        source = VideoContent(path) if kind == "video" else ImageContent(path)
        sources = [source]
        if kind in {"album", "mixed"}:
            second_path = tmp_path / ("second.mp4" if kind == "mixed" else "second.jpg")
            second_path.write_bytes(b"second media")
            sources.append(VideoContent(second_path) if kind == "mixed" else ImageContent(second_path))
        comment_calls = []

        async def comments():
            comment_calls.append("fetch")
            return document()

        result = ParseResult(
            Platform("weibo", "微博"), title="post title", text=body,
            url="https://example.com/post", contents=sources,
            delivery=DeliveryPlan([DeliveryBatch(sources)]) if native else None,
            extra={"native_delivery": native, "render_text_card": True,
                   "text_card_comment_factory": comments, "send_text": send_text,
                   # Real Weibo mixed posts carry this marker; images are not covers.
                   "mixed_post_body_card": kind == "mixed"},
        )
        # Prime a genuine successful card cache, then disable cards on the same instance.
        if warm_cache:
            assert await plugin._build_text_card_content(result) is not None
        plugin.text_renderer.calls.clear()
        comment_calls.clear()
        plugin.config["comments"] = {"enabled": False}
        event = CaptureEvent()
        await plugin._send_parse_result(event, result)
        parts = delivered_parts(event)
        expected_body = [body] if body and kind != "video" else []
        assert [part.text for part in parts if isinstance(part, Plain)] == expected_body
        media = [part for part in parts if isinstance(part, (Image, Video))]
        assert len(media) == len(sources)
        expected_paths = [path] + ([second_path] if kind in {"album", "mixed"} else [])
        assert all(str(expected) in part.file for expected, part in zip(expected_paths, media, strict=True))
        assert not plugin.text_renderer.calls
        assert not comment_calls
        assert path.read_bytes() == b"original media"
    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("kind", ["image", "video", "album", "mixed"])
def test_enabled_render_failure_still_sends_summary(tmp_path, native, kind):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.config["comments"] = {"enabled": True}
        calls = []

        async def fail_render(*args, **kwargs):
            calls.append("render")
            raise RuntimeError("intentional render failure")

        plugin.text_renderer.render_card = fail_render
        path = tmp_path / "original.jpg"
        path.write_bytes(b"original")
        sources = [VideoContent(path) if kind == "video" else ImageContent(path)]
        if kind in {"album", "mixed"}:
            sources.append(VideoContent(path) if kind == "mixed" else ImageContent(path))
        result = ParseResult(
            Platform("weibo", "微博"), title="post title", text="summary body",
            url="https://example.com/post", contents=sources,
            delivery=DeliveryPlan([DeliveryBatch(sources)]) if native else None,
            extra={"native_delivery": native, "render_text_card": True,
                   "mixed_post_body_card": kind == "mixed"},
        )
        event = CaptureEvent()
        await plugin._send_parse_result(event, result)
        parts = delivered_parts(event)
        summaries = [part.text for part in parts if isinstance(part, Plain)]
        if kind == "video":
            # No comments means intentional suppression, not a body render failure.
            assert not calls
            assert summaries == []
        elif kind == "mixed":
            assert calls
            assert summaries == ["summary body"]
        else:
            assert calls
            assert len(summaries) == 1
            assert "post title" in summaries[0]
            assert "summary body" in summaries[0]
            assert "https://example.com/post" in summaries[0]
        assert len([part for part in parts if isinstance(part, (Image, Video))]) == len(sources)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["image", "video", "text"])
def test_disabled_cards_preserve_native_body(tmp_path, kind):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.config["comments"] = {"enabled": False}
        path = tmp_path / "original.jpg"
        path.write_bytes(b"original")
        sources = [] if kind == "text" else [VideoContent(path) if kind == "video" else ImageContent(path)]
        result = ParseResult(
            Platform("weibo", "微博"), title="post title", text="native body",
            contents=sources,
            delivery=DeliveryPlan([DeliveryBatch(["native body", *sources])]),
            extra={"native_delivery": True, "render_text_card": True},
        )
        event = CaptureEvent()
        await plugin._send_parse_result(event, result)
        parts = delivered_parts(event)
        expected_body = [] if kind == "video" else ["native body"]
        assert [part.text for part in parts if isinstance(part, Plain)] == expected_body
        assert len([part for part in parts if isinstance(part, (Image, Video))]) == len(sources)
        assert not plugin.text_renderer.calls
    asyncio.run(run())


@pytest.mark.parametrize("render_text_card", [False, True])
def test_disabled_cards_preserve_plain_text_work(tmp_path, render_text_card):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.config["comments"] = {"enabled": False}
        result = ParseResult(Platform("weibo", "微博"), text="plain text work",
                             extra={"render_text_card": render_text_card})
        event = CaptureEvent()
        await plugin._send_parse_result(event, result)
        assert any("plain text work" in part.text for part in delivered_parts(event) if isinstance(part, Plain))
        assert not plugin.text_renderer.calls
    asyncio.run(run())
