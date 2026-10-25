"""媒体发送失败的一次兜底重试：换新文件（新 md5）再发，仍失败才发提示。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from astrbot.api.message_components import Video

from astrbot_plugin_parser_x.core.data import ParseResult, Platform
from astrbot_plugin_parser_x.main import ParserXPlugin

UNCONFIRMED = (
    "ActionFailed status='failed', retcode=100, data=None, wording='group message delivery"
    " was not confirmed: QQ returned no message receipt; check the conversation before"
    " retrying'"
)
RICH_MEDIA = "ActionFailed status='failed', retcode=1200, wording='rich media transfer failed'"


class Event:
    """记录发送链；按顺序抛出预置异常。"""

    def __init__(self, failures=()):
        self.sent = []
        self._failures = list(failures)

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return text

    async def send(self, chain):
        if self._failures:
            raise RuntimeError(self._failures.pop(0))
        self.sent.append(chain)
        return None


def result_for(url="https://bilibili.com/BV1QHnq6LEqv"):
    return ParseResult(Platform("bili", "B站"), url=url)


def build_plugin(tmp_path, monkeypatch, *, remux=None, transcode=None):
    """构造只带发送所需属性的插件实例，并按需替换两个重建助手。"""
    calls: list[str] = []
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {}
    plugin.cache_dir = tmp_path

    async def default_remux(input_path: Path) -> Path:
        calls.append("remux")
        out = input_path.with_name(f"{input_path.stem}_fresh.mp4")
        out.write_bytes(b"remuxed-bytes")
        return out

    async def default_transcode(input_path: Path) -> Path:
        calls.append("transcode")
        out = input_path.with_name(f"{input_path.stem}_h264.mp4")
        out.write_bytes(b"transcoded-bytes")
        return out

    plugin._remux_fresh_copy = remux or default_remux
    plugin._transcode_to_h264 = transcode or default_transcode
    return plugin, calls


def video_at(tmp_path, name="clip.mp4", payload=b"original-bytes"):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_unconfirmed_delivery_retries_with_fresh_file_and_skips_tip(tmp_path, monkeypatch):
    async def run():
        source = video_at(tmp_path)
        plugin, calls = build_plugin(tmp_path, monkeypatch)
        event = Event(failures=[UNCONFIRMED])

        await plugin._send_video_segment(event, result_for(), Video(str(source)))

        assert calls == ["remux"]
        assert event.sent and not isinstance(event.sent[-1], str)
        assert isinstance(event.sent[-1][0], Video)
        assert source.read_bytes() == b"remuxed-bytes", "原文件被新字节自愈替换"
        assert not (tmp_path / "clip_fresh.mp4").exists(), "临时文件已清理"

    asyncio.run(run())


def test_retry_failure_falls_back_to_notice_with_original_link(tmp_path, monkeypatch):
    async def run():
        source = video_at(tmp_path)
        plugin, calls = build_plugin(tmp_path, monkeypatch)
        event = Event(failures=[UNCONFIRMED, "delivery was not confirmed", "send timeout"])

        await plugin._send_video_segment(
            event, result_for(url="https://bilibili.com/BV1QHnq6LEqv"), Video(str(source))
        )

        assert calls == ["remux", "transcode"]
        assert event.sent[-1] == (
            "⚠️ 媒体发送失败\n🔗 原链接: https://bilibili.com/BV1QHnq6LEqv"
        )

    asyncio.run(run())


def test_codec_failure_prefers_transcode_then_remux(tmp_path, monkeypatch):
    async def run():
        source = video_at(tmp_path)
        plugin, calls = build_plugin(tmp_path, monkeypatch)
        event = Event(failures=[RICH_MEDIA])

        await plugin._send_video_segment(event, result_for(), Video(str(source)))

        assert calls == ["transcode"]
        assert isinstance(event.sent[-1][0], Video)

    asyncio.run(run())


def test_rebuild_error_still_tries_other_strategy(tmp_path, monkeypatch):
    async def run():
        source = video_at(tmp_path)

        async def broken_remux(input_path):
            raise RuntimeError("ffmpeg not found")

        plugin, calls = build_plugin(tmp_path, monkeypatch, remux=broken_remux)
        event = Event(failures=[UNCONFIRMED])

        await plugin._send_video_segment(event, result_for(), Video(str(source)))

        assert calls == ["transcode"], "重新封装不可用时回落到转码"
        assert isinstance(event.sent[-1][0], Video)
        assert source.read_bytes() == b"transcoded-bytes"

    asyncio.run(run())


def test_missing_source_skips_retry_and_sends_notice(tmp_path, monkeypatch):
    async def run():
        missing = tmp_path / "gone.mp4"
        plugin, calls = build_plugin(tmp_path, monkeypatch)
        event = Event(failures=["ENOENT: no such file or directory, realpath 'gone.mp4'"])

        await plugin._send_video_segment(event, result_for(url="https://x/1"), Video(str(missing)))

        assert calls == [], "文件不存在时不做无意义的重建"
        assert event.sent[-1] == "⚠️ 媒体发送失败\n🔗 原链接: https://x/1"

    asyncio.run(run())


def test_notice_send_failure_does_not_raise(tmp_path, monkeypatch):
    async def run():
        source = video_at(tmp_path)

        class BrokenEvent(Event):
            async def send(self, chain):
                if isinstance(chain, str):
                    raise RuntimeError("group unavailable")
                return await super().send(chain)

        plugin, _ = build_plugin(tmp_path, monkeypatch)
        event = BrokenEvent(failures=[UNCONFIRMED, "still failing", "still failing"])

        await plugin._send_video_segment(event, result_for(), Video(str(source)))

    asyncio.run(run())


def test_remux_helper_changes_digest_with_real_ffmpeg(tmp_path):
    async def run():
        source = tmp_path / "sample.mp4"
        generator = asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(source),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            proc = await generator
        except FileNotFoundError:
            pytest.skip("容器内没有 ffmpeg")
        _, err = await proc.communicate()
        assert proc.returncode == 0, err.decode("utf-8", "replace")

        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}
        fresh = await plugin._remux_fresh_copy(source)
        try:
            assert fresh != source
            assert fresh.exists() and fresh.stat().st_size > 0
            assert hashlib.md5(fresh.read_bytes()).hexdigest() != hashlib.md5(source.read_bytes()).hexdigest()
        finally:
            fresh.unlink(missing_ok=True)

    asyncio.run(run())


def test_remux_helper_rejects_empty_output(tmp_path, monkeypatch):
    """ffmpeg 静默失败时不能把 0 字节文件当成功结果。"""

    async def run():
        source = video_at(tmp_path)
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}

        async def fake_exec(cmd):
            Path(cmd[-1]).write_bytes(b"")

        monkeypatch.setattr("astrbot_plugin_parser_x.core.delivery.exec_ffmpeg_cmd", fake_exec)
        with pytest.raises(RuntimeError):
            await plugin._remux_fresh_copy(source)

    asyncio.run(run())
