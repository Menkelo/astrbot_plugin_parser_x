"""Regression tests for the parser -> document -> unified-card delivery seam."""
import asyncio
from types import SimpleNamespace

import pytest
from astrbot.api.message_components import Image, Nodes, Plain, Reply, Video
from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, DOUYIN_THEME,
)
from astrbot_plugin_parser_x.core.data import (
    DeliveryBatch, DeliveryPlan, ImageContent, ParseResult, Platform, VideoContent,
)
from astrbot_plugin_parser_x.main import ParserXPlugin


class CaptureEvent:
    message_obj = SimpleNamespace(message_id=123)

    def __init__(self):
        self.sent = []
        self.video_started = asyncio.Event()
        self.release_video = asyncio.Event()
        self.release_video.set()

    def get_sender_id(self):
        return "42"

    def get_sender_name(self):
        return "Tester"

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return [Plain(text)]

    async def send(self, chain):
        if isinstance(chain[0], Video):
            self.video_started.set()
            await self.release_video.wait()
        self.sent.append(chain)


class CaptureRenderer:
    def __init__(self):
        self.calls = []
        self.ready = asyncio.Event()

    async def render_card(self, out_path, **kwargs):
        self.calls.append(kwargs)
        out_path.write_bytes(b"rendered unified card")
        self.ready.set()


def make_plugin(tmp_path):
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.cache_dir = tmp_path
    plugin.text_renderer = CaptureRenderer()
    return plugin


def document():
    return CommentDocument(DOUYIN_THEME, "work", "", "1", [
        CommentEntry(CommentAuthor("reader"), [CommentRichPart("text", "visible comment")]),
    ])


def forwarded_parts(event):
    return [node.content[0] for chain in event.sent if isinstance(chain[0], Nodes)
            for node in chain[0].nodes]


@pytest.mark.parametrize("native", [False, True])
def test_image_post_renders_document_once_and_keeps_original(tmp_path, native):
    plugin = make_plugin(tmp_path)
    source_path = tmp_path / "original.jpg"
    source_path.write_bytes(b"original image")
    source = ImageContent(source_path)
    doc = document()
    calls = []

    async def comments():
        calls.append("fetch")
        return doc

    result = ParseResult(Platform("douyin", "抖音"), text="body", contents=[source],
                         delivery=DeliveryPlan([DeliveryBatch([source])]) if native else None,
                         extra={"render_text_card": True, "native_delivery": native,
                                "text_card_comment_factory": comments})
    event = CaptureEvent()
    asyncio.run(plugin._send_parse_result(event, result))
    assert calls == ["fetch"]
    assert len(plugin.text_renderer.calls) == 1
    assert plugin.text_renderer.calls[0]["comment_document"] is doc
    parts = forwarded_parts(event)
    assert len(parts) == 2
    assert all(isinstance(part, Image) for part in parts)
    assert str(source_path) in parts[1].file
    assert source_path.read_bytes() == b"original image"


@pytest.mark.parametrize("native", [False, True])
def test_ready_comment_card_waits_for_video_completion(tmp_path, native):
    async def run():
        plugin = make_plugin(tmp_path)
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"video")
        video = VideoContent(video_path)
        doc = document()
        calls = []

        async def comments():
            calls.append("fetch")
            return doc

        event = CaptureEvent()
        event.release_video.clear()
        result = ParseResult(Platform("douyin", "抖音"), text="body", contents=[video],
                             delivery=DeliveryPlan([DeliveryBatch([video])]) if native else None,
                             extra={"render_text_card": True, "native_delivery": native,
                                    "text_card_comment_factory": comments})
        task = asyncio.create_task(plugin._send_parse_result(event, result))
        try:
            await asyncio.wait_for(event.video_started.wait(), 1)
            await asyncio.wait_for(plugin.text_renderer.ready.wait(), 1)
            assert event.sent == [], "card must not overtake an in-flight video send"
        finally:
            event.release_video.set()
            await asyncio.wait_for(task, 2)
        assert isinstance(event.sent[0][0], Video)
        assert len(event.sent) == 2
        assert len(forwarded_parts(event)) == 2
        assert forwarded_parts(event)[0].text == "抖音 · 热门评论"
        assert isinstance(forwarded_parts(event)[1], Image)
        assert plugin.text_renderer.calls[0]["comment_document"] is doc
        assert calls == ["fetch"]
    asyncio.run(run())


def assert_comment_failure_preserves_video(tmp_path, mode, native=False):
    async def run():
        plugin = make_plugin(tmp_path)
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"video")
        video = VideoContent(video_path)
        calls = []
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def comments():
            calls.append("fetch")
            started.set()
            if mode == "failure":
                raise RuntimeError("comment unavailable")
            if mode == "invalid":
                return [ImageContent(video_path)]
            if mode == "empty":
                return None
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        result = ParseResult(Platform("douyin", "抖音"), text="body survives",
                             contents=[video],
                             delivery=DeliveryPlan([DeliveryBatch([video])]) if native else None,
                             extra={"render_text_card": True, "native_delivery": native,
                                    "text_card_comment_factory": comments,
                                    "comment_timeout": 0.05})
        event = CaptureEvent()
        task = asyncio.create_task(plugin._send_parse_result(event, result))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(event.video_started.wait(), 1)
        if mode == "timeout":
            assert not task.done(), "video must start before the slow comment finishes"
        await asyncio.wait_for(task, 2)
        assert calls == ["fetch"]
        assert len(event.sent) == 1
        assert isinstance(event.sent[0][0], Video)
        assert plugin.text_renderer.calls == []
        assert result.extra['_video_card_suppressed']
        if mode == "timeout":
            assert cancelled.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["failure", "timeout", "empty", "invalid"])
def test_comment_failure_modes_preserve_body_and_video(tmp_path, mode, native):
    assert_comment_failure_preserves_video(tmp_path, mode, native)


def assert_forward_failure_preserves_card(tmp_path):
    plugin = make_plugin(tmp_path)
    video_path = tmp_path / "fallback-video.mp4"
    video_path.write_bytes(b"video")
    doc = document()
    calls = []

    async def comments():
        calls.append("fetch")
        return doc

    class RejectForward(CaptureEvent):
        def __init__(self):
            super().__init__()
            self.forward_attempts = 0

        async def send(self, chain):
            if isinstance(chain[0], Nodes):
                self.forward_attempts += 1
                raise RuntimeError("forward unavailable")
            await super().send(chain)

    event = RejectForward()
    result = ParseResult(Platform("weibo", "微博"), text="body",
                         contents=[VideoContent(video_path)],
                         extra={"render_text_card": True,
                                "text_card_comment_factory": comments})
    asyncio.run(plugin._send_parse_result(event, result))
    assert calls == ["fetch"]
    assert event.forward_attempts == 1
    assert len(event.sent) == 2
    assert isinstance(event.sent[0][0], Video)
    assert isinstance(event.sent[1][0], Reply)
    assert isinstance(event.sent[1][1], Image)
    assert plugin.text_renderer.calls[0]["comment_document"] is doc


@pytest.mark.parametrize('platform', ['zhihu', 'douyin', 'bilibili', 'xiaohongshu', 'weibo', 'kuaishou', 'acfun', 'wechat_channels'])
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('with_comments', [False, True])
def test_video_never_resends_source_image(tmp_path, platform, native, with_comments):
    plugin = make_plugin(tmp_path)
    vp, ip = tmp_path / 'v.mp4', tmp_path / 'cover.jpg'
    vp.write_bytes(b'video')
    ip.write_bytes(b'original')
    video, image = VideoContent(vp), ImageContent(ip)
    async def comments():
        return document() if with_comments else CommentDocument(DOUYIN_THEME, '', '', '99', [])
    result = ParseResult(Platform(platform, platform), title='title', text='body', contents=[video, image],
        delivery=DeliveryPlan([DeliveryBatch([video]), DeliveryBatch([image])]) if native else None,
        extra={'native_delivery': native, 'render_text_card': True, 'text_card_comment_factory': comments})
    event = CaptureEvent()
    asyncio.run(plugin._send_parse_result(event, result))
    assert isinstance(event.sent[0][0], Video)
    assert len(event.sent) == (2 if with_comments else 1)
    assert str(ip) not in str(event.sent)
    assert len(plugin.text_renderer.calls) == int(with_comments)


def test_forward_failure_preserves_unified_card(tmp_path):
    assert_forward_failure_preserves_card(tmp_path)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_native_media_only_plan_preserves_body_on_render_failure(tmp_path, kind):
    plugin = make_plugin(tmp_path)

    class BrokenRenderer:
        async def render_card(self, *_args, **_kwargs):
            raise RuntimeError("renderer offline")

    plugin.text_renderer = BrokenRenderer()
    path = tmp_path / ("source.jpg" if kind == "image" else "source.mp4")
    path.write_bytes(b"source")
    source = ImageContent(path) if kind == "image" else VideoContent(path)
    result = ParseResult(Platform("weibo", "微博"), title="post title", text="body must survive",
                         contents=[source], delivery=DeliveryPlan([DeliveryBatch([source])]),
                         extra={"native_delivery": True, "render_text_card": True})
    event = CaptureEvent()
    asyncio.run(plugin._send_parse_result(event, result))
    parts = [part for chain in event.sent for part in chain] + forwarded_parts(event)
    assert any(isinstance(part, Plain) and "body must survive" in part.text for part in parts) == (kind == 'image')
    expected_type = Image if kind == "image" else Video
    assert any(isinstance(part, expected_type) for part in parts)
    if kind == "video":
        assert isinstance(event.sent[0][0], Video)


def test_native_forward_and_card_rejection_still_attempts_original_image(tmp_path):
    plugin = make_plugin(tmp_path)
    source_path = tmp_path / "original.jpg"
    source_path.write_bytes(b"original")
    source = ImageContent(source_path)

    class RejectCard(CaptureEvent):
        async def send(self, chain):
            if isinstance(chain[0], Nodes):
                raise RuntimeError("forward rejected")
            if isinstance(chain[0], Image) and str(source_path) not in chain[0].file:
                raise RuntimeError("card rejected")
            await super().send(chain)

    result = ParseResult(Platform("weibo", "微博"), text="body", contents=[source],
                         delivery=DeliveryPlan([DeliveryBatch([source])]),
                         extra={"native_delivery": True, "render_text_card": True})
    event = RejectCard()
    asyncio.run(plugin._send_parse_result(event, result))
    assert any(isinstance(chain[0], Image) and str(source_path) in chain[0].file for chain in event.sent)


def test_cancel_image_parse_cancels_pending_comment_work(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        comment_started = asyncio.Event()
        comment_cancelled = asyncio.Event()
        download_started = asyncio.Event()

        async def comments():
            comment_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                comment_cancelled.set()

        async def blocked_download(content):
            download_started.set()
            await asyncio.Event().wait()

        plugin._download_content = blocked_download
        source = ImageContent(tmp_path / "original.jpg")
        result = ParseResult(Platform("weibo", "微博"), text="body", contents=[source],
                             extra={"render_text_card": True, "text_card_comment_factory": comments})
        task = asyncio.create_task(plugin._send_parse_result(CaptureEvent(), result))
        await asyncio.wait_for(comment_started.wait(), 1)
        await asyncio.wait_for(download_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert comment_cancelled.is_set(), "aborted parse must not retain detached comment/render task"
    asyncio.run(run())
