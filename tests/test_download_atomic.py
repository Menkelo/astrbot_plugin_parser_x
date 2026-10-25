"""Offline regressions for download publication and same-destination races."""
import asyncio
from pathlib import Path
import threading

import pytest

from astrbot_plugin_parser_x.core.download import Downloader
from astrbot_plugin_parser_x.core.exception import DownloadException, SizeLimitException


@pytest.mark.asyncio
async def test_same_destination_waits_for_complete_download(tmp_path, monkeypatch):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def download(url, path, name, headers, limit):
        calls.append(path)
        path.write_bytes(b"a" * 200)
        entered.set()
        await release.wait()
        path.write_bytes(b"a" * 400)
        return path

    monkeypatch.setattr(downloader, "_download_generic", download)
    first = downloader.streamd("https://offline/a", file_name="same.mp4")
    await entered.wait()
    second = downloader.streamd("https://offline/a", file_name="same.mp4")
    try:
        await asyncio.sleep(0)
        assert not second.done(), "a partial cache file must not count as complete"
        assert not (tmp_path / "same.mp4").exists()
    finally:
        release.set()
        await asyncio.gather(first, second)
        await downloader.close()
    assert len(calls) == 1
    assert (tmp_path / "same.mp4").read_bytes() == b"a" * 400


class Response:
    status = status_code = 200

    def __init__(self, chunks, length=None, error=None):
        self.chunks, self.error = chunks, error
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.content = self

    async def aiter_content(self, chunk_size):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    def iter_chunked(self, chunk_size):
        return self.aiter_content(chunk_size)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def save(downloader, backend, response, target, monkeypatch, limit=1):
    if backend == "curl":
        return await downloader._save_response_to_file(response, target, target.name, limit)

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url):
            return response

    monkeypatch.setattr("astrbot_plugin_parser_x.core.download.aiohttp.ClientSession", Session)
    return await downloader._download_with_aiohttp("https://offline/a", target, target.name, {}, limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["curl", "aiohttp"])
async def test_truncated_transfer_preserves_previous_file(tmp_path, monkeypatch, backend):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = tmp_path / "keep.mp4"
    target.write_bytes(b"old" * 100)
    try:
        with pytest.raises(DownloadException):
            await save(downloader, backend, Response([b"n" * 200], length=400), target, monkeypatch)
        assert target.read_bytes() == b"old" * 100
        assert list(tmp_path.iterdir()) == [target]
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_cached_file_obeys_callers_size_limit(tmp_path):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = tmp_path / "large.mp4"
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    try:
        with pytest.raises(SizeLimitException):
            await downloader.streamd("https://offline/a", file_name=target.name, max_size_mb=1)
        assert target.exists()
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_cancelled_ytdlp_cleans_files_after_worker_finishes(tmp_path, monkeypatch):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class YDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def download(self, urls):
            entered.set()
            release.wait(3)
            Path(self.opts["outtmpl"]).write_bytes(b"x" * 200)
            finished.set()

    monkeypatch.setattr("astrbot_plugin_parser_x.core.download.yt_dlp.YoutubeDL", YDL)
    task = asyncio.create_task(downloader._download_with_ytdlp_fallback("https://offline/a", tmp_path / "a.mp4", {}))
    await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(finished.wait, 3)
        await asyncio.sleep(0)
        assert not list(tmp_path.iterdir()), "cancelled worker must not leak staging files"
    finally:
        release.set()
        await downloader.close()


@pytest.mark.asyncio
async def test_stream_validates_fallback_output_before_publication(tmp_path, monkeypatch):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    async def download(url, path, *args):
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        return path
    monkeypatch.setattr(downloader, "_download_generic", download)
    try:
        with pytest.raises(SizeLimitException):
            await downloader.streamd("https://offline/a", file_name="big.mp4", max_size_mb=1)
        assert not list(tmp_path.iterdir())
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["curl", "aiohttp"])
@pytest.mark.parametrize("failure", ["cancel", "error", "oversize", "tiny", "overrun"])
async def test_failed_response_never_replaces_previous_file(tmp_path, monkeypatch, backend, failure):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = tmp_path / "old.mp4"
    target.write_bytes(b"old" * 100)
    responses = {
        "cancel": Response([b"x" * 200], error=asyncio.CancelledError()),
        "error": Response([b"x" * 200], error=OSError("offline failure")),
        "oversize": Response([b"x" * (1024 * 1024 + 1)]),
        "tiny": Response([b"x"]),
        "overrun": Response([b"x" * 300], length=200),
    }
    errors = {"cancel": asyncio.CancelledError, "error": OSError, "oversize": SizeLimitException}
    try:
        with pytest.raises(errors.get(failure, DownloadException)):
            await save(downloader, backend, responses[failure], target, monkeypatch)
        assert target.read_bytes() == b"old" * 100
        assert list(tmp_path.iterdir()) == [target]
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["curl", "aiohttp"])
@pytest.mark.parametrize("length", [None, 200])
async def test_complete_response_is_atomically_published(tmp_path, monkeypatch, backend, length):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    target = tmp_path / "new.mp4"
    try:
        await save(downloader, backend, Response([b"x" * 100, b"y" * 100], length), target, monkeypatch)
        assert target.read_bytes() == b"x" * 100 + b"y" * 100
        assert list(tmp_path.iterdir()) == [target]
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_cancelled_owner_releases_destination_to_waiter(tmp_path, monkeypatch):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    entered = asyncio.Event()
    calls = []
    async def download(url, path, *args):
        calls.append(path)
        path.write_bytes(b"x" * 200)
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()
        return path
    monkeypatch.setattr(downloader, "_download_generic", download)
    first = downloader.streamd("https://offline/a", file_name="same.mp4")
    await entered.wait()
    second = downloader.streamd("https://offline/a", file_name="same.mp4")
    first.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(second, 2)
        assert result.read_bytes() == b"x" * 200
        assert len(calls) == 2 and calls[0] != calls[1]
        assert list(tmp_path.iterdir()) == [result]
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["curl", "aiohttp"])
async def test_decoded_body_is_not_compared_with_encoded_wire_length(tmp_path, monkeypatch, backend):
    downloader = Downloader({"cache_dir": str(tmp_path)})
    response = Response([b"x" * 300], length=50)
    response.headers["Content-Encoding"] = "gzip"
    try:
        target = tmp_path / "decoded.mp4"
        await save(downloader, backend, response, target, monkeypatch)
        assert target.stat().st_size == 300
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_destination_lock_is_shared_across_downloader_instances(tmp_path, monkeypatch):
    first_downloader = Downloader({"cache_dir": str(tmp_path)})
    second_downloader = Downloader({"cache_dir": str(tmp_path)})
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def download(url, path, *args):
        calls.append(path)
        path.write_bytes(b"x" * 200)
        entered.set()
        await release.wait()
        return path
    monkeypatch.setattr(first_downloader, "_download_generic", download)
    monkeypatch.setattr(second_downloader, "_download_generic", download)
    first = first_downloader.streamd("https://offline/a", file_name="same.mp4")
    await entered.wait()
    second = second_downloader.streamd("https://offline/a", file_name="same.mp4")
    try:
        await asyncio.sleep(0)
        assert not second.done()
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
        await first_downloader.close()
        await second_downloader.close()
    assert len(calls) == 1
