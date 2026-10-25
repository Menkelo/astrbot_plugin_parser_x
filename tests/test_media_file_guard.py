"""Sending must never hand the platform a media path whose file is gone.

A cached parse or a long-lived delivery plan can keep a resolved ``Path`` whose
file the cache cleaner (24h, 00:00) or a concurrent re-download removed. Passing
that path to ``event.send`` surfaces as a Node-side
``ENOENT: no such file or directory, realpath`` warning from the bot backend,
which users see as "媒体发送失败" even though parsing succeeded.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from astrbot_plugin_parser_x.core.data import (
    DynamicContent,
    ImageContent,
    MediaContent,
    VideoContent,
)
from astrbot_plugin_parser_x.core.download import Downloader
from astrbot_plugin_parser_x.main import ParserXPlugin


def _write(path: Path, payload: bytes = b"x" * 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _plugin() -> ParserXPlugin:
    # _download_content only needs the module-level logger and data classes.
    return object.__new__(ParserXPlugin)


@pytest.mark.asyncio
async def test_resolved_path_that_vanished_is_not_sent(tmp_path: Path) -> None:
    media = _write(tmp_path / "BV1missing-1.mp4")
    content = VideoContent(media)
    assert await content.get_path() == media

    media.unlink()

    cont, path, error = await _plugin()._download_content(content)

    assert path is None
    assert error == "[下载失败: 媒体文件已不存在: BV1missing-1.mp4]"
    assert cont is content


@pytest.mark.asyncio
async def test_task_path_that_vanished_is_not_sent(tmp_path: Path) -> None:
    media = _write(tmp_path / "douyin_123.mp4")
    content = ImageContent(asyncio.create_task(_return_then_delete(media)))

    _, path, error = await _plugin()._download_content(content)

    assert path is None
    assert error == "[下载失败: 媒体文件已不存在: douyin_123.mp4]"


async def _return_then_delete(path: Path) -> Path:
    path.unlink()
    return path


@pytest.mark.asyncio
async def test_existing_media_is_returned_and_mtime_refreshed(tmp_path: Path) -> None:
    media = _write(tmp_path / "a.jpg")
    stale = time.time() - 3 * 24 * 3600
    os.utime(media, (stale, stale))

    _, path, error = await _plugin()._download_content(MediaContent(media))

    assert path == media
    assert error is None
    assert media.stat().st_mtime > stale + 3600


@pytest.mark.asyncio
async def test_missing_live_photo_falls_back_to_still_image(tmp_path: Path) -> None:
    live = _write(tmp_path / "live.mp4")
    still = _write(tmp_path / "cover.jpg")
    content = DynamicContent(live, fallback_image=ImageContent(still))
    live.unlink()

    _, path, error = await _plugin()._download_content(content)

    assert path == still
    assert error is None


@pytest.mark.asyncio
async def test_ytdlp_fallback_keeps_the_previous_file_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = _write(tmp_path / "keep-me.mp4", b"o" * 4096)

    def failing_download(_self, _urls):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(
        "astrbot_plugin_parser_x.core.download.yt_dlp.YoutubeDL",
        _FakeYoutubeDLFactory(failing_download),
    )

    with pytest.raises(RuntimeError):
        await downloader._download_with_ytdlp_fallback(
            "https://example.com/v.mp4", target, {}
        )

    assert target.is_file()
    assert target.stat().st_size == 4096


@pytest.mark.asyncio
async def test_ytdlp_fallback_swaps_the_new_file_in_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = _write(tmp_path / "old.mp4", b"o" * 100)
    produced: list[Path] = []

    def successful_download(self, _urls):
        outtmpl = Path(self.params["outtmpl"])
        produced.append(outtmpl)
        outtmpl.write_bytes(b"n" * 8192)

    monkeypatch.setattr(
        "astrbot_plugin_parser_x.core.download.yt_dlp.YoutubeDL",
        _FakeYoutubeDLFactory(successful_download),
    )

    result = await downloader._download_with_ytdlp_fallback(
        "https://example.com/v.mp4", target, {}
    )

    assert result == target
    assert target.read_bytes() == b"n" * 8192
    assert produced and produced[0] != target
    assert not produced[0].exists()


class _FakeYoutubeDL:
    def __init__(self, factory: "_FakeYoutubeDLFactory", params: dict) -> None:
        self._factory = factory
        self.params = params

    def __enter__(self) -> "_FakeYoutubeDL":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def download(self, urls) -> None:
        self._factory.behaviour(self, urls)


class _FakeYoutubeDLFactory:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour

    def __call__(self, params: dict) -> _FakeYoutubeDL:
        return _FakeYoutubeDL(self, params)
