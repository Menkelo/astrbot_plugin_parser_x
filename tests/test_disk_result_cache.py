from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import patch

import pytest

from astrbot_plugin_parser_x.core.data import (
    AudioContent, Author, DeliveryBatch, DeliveryPlan, DynamicContent, FileContent,
    GraphicsContent, ImageContent, ParseResult, Platform, VideoContent,
)
from astrbot_plugin_parser_x.core.disk_result_cache import DiskResultCache


def test_text_roundtrip_survives_reload_with_exact_caller_key(tmp_path):
    result = ParseResult(Platform("demo", "Demo"), text="persisted")
    cache = DiskResultCache(tmp_path)
    assert asyncio.run(cache.save("config-a/input", result))
    loaded = DiskResultCache(tmp_path).load("config-a/input")
    assert loaded.text == "persisted"
    assert loaded.platform == result.platform
    assert DiskResultCache(tmp_path).load("config-b/input") is None
    assert DiskResultCache(tmp_path).load("config-a/input/other") is None
    manifest = next(tmp_path.rglob("*.json"))
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_media_future_delivery_repost_and_safe_extra_roundtrip(tmp_path):
    path = tmp_path / "media.jpg"
    path.write_bytes(b"complete media")

    async def run():
        future = asyncio.get_running_loop().create_future()
        future.set_result(path)
        image = ImageContent(future)
        result = ParseResult(
            Platform("demo", "Demo"), author=Author("author", future, "bio"),
            contents=[image, AudioContent(path, 2.5), FileContent(path, "file"),
                      VideoContent(future, future, 7.5, True),
                      DynamicContent(path, path, image), GraphicsContent(path, "text", "alt")],
            delivery=DeliveryPlan([DeliveryBatch(["hello", image], "forward", True)]),
            repost=ParseResult(Platform("other", "Other"), contents=[ImageContent(path)]),
            extra={"nested": [1, True, None, {"card": future}],
                   "comment_image_task_factory": lambda: None,
                   "cookies": "secret", "nested_secret": {"Cookie": "secret"}},
        )
        cache = DiskResultCache(tmp_path)
        assert await cache.save("key", result)
        loaded = cache.load("key")
        assert loaded.author.avatar == path
        assert loaded.contents[0].path_task == path
        assert loaded.contents[3].cover == path
        assert loaded.contents[3].is_file_upload is True
        assert loaded.contents[4].fallback_image.path_task == path
        assert loaded.delivery.batches[0].parts[1] is loaded.contents[0]
        assert loaded.delivery.batches[0].mode == "forward"
        assert loaded.repost.contents[0].path_task == path
        assert loaded.extra == {"nested": [1, True, None, {"card": path}], "nested_secret": {}}
        assert future.result() == path
        assert '"secret"' not in next(tmp_path.rglob("*.json")).read_text()

    asyncio.run(run())


def test_ttl_uses_write_time_not_reads_or_mtime(tmp_path):
    cache = DiskResultCache(tmp_path)
    with patch("astrbot_plugin_parser_x.core.disk_result_cache.time.time", return_value=1000):
        assert asyncio.run(cache.save("key", ParseResult(Platform("demo", "Demo"))))
    manifest = next(tmp_path.rglob("*.json"))
    os.utime(manifest, (999999, 999999))
    with patch("astrbot_plugin_parser_x.core.disk_result_cache.time.time", return_value=1400):
        assert DiskResultCache(tmp_path).load("key") is not None
    with patch("astrbot_plugin_parser_x.core.disk_result_cache.time.time", return_value=1000 + 86400):
        assert cache.load("key") is None


@pytest.mark.parametrize("mutation", ["truncated", "version", "nan", "schema", "future"])
def test_bad_manifests_reject_without_raising(tmp_path, mutation):
    cache = DiskResultCache(tmp_path)
    assert asyncio.run(cache.save("key", ParseResult(Platform("demo", "Demo"))))
    path = next(tmp_path.rglob("*.json"))
    data = json.loads(path.read_text())
    if mutation == "truncated":
        path.write_text('{"version":')
    else:
        if mutation == "version":
            data["version"] = 999
        elif mutation == "nan":
            data["written_at"] = float("nan")
        elif mutation == "future":
            data["written_at"] = time.time() + 1000
        else:
            data["result"]["fields"]["platform"] = 10
        path.write_text(json.dumps(data))
    assert cache.load("key") is None


@pytest.mark.parametrize("invalid", ["outside", "empty", "missing", "symlink", "failed", "cancelled", "unknown", "cycle"])
def test_invalid_snapshots_are_never_published(tmp_path, invalid):
    root = tmp_path / "cache"
    root.mkdir()
    path = root / "media"
    path.write_bytes(b"valid")
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")

    async def run():
        media = path
        extra = {}
        if invalid == "outside":
            media = outside
        elif invalid == "empty":
            path.write_bytes(b"")
        elif invalid == "missing":
            path.unlink()
        elif invalid == "symlink":
            path.unlink()
            path.symlink_to(outside)
        elif invalid in ("failed", "cancelled"):
            media = asyncio.get_running_loop().create_future()
            if invalid == "failed":
                media.set_exception(RuntimeError("failed download"))
            else:
                media.cancel()
        elif invalid == "unknown":
            extra["bad"] = object()
        else:
            extra["bad"] = extra
        cache = DiskResultCache(root)
        assert not await cache.save("key", ParseResult(Platform("d", "D"), contents=[ImageContent(media)], extra=extra))
        assert cache.load("key") is None
        assert not list(root.rglob("*.json"))

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["truncate", "delete", "empty"])
def test_missing_or_size_changed_media_rejects_reload(tmp_path, damage):
    path = tmp_path / "media"
    path.write_bytes(b"complete")
    cache = DiskResultCache(tmp_path)
    assert asyncio.run(cache.save("key", ParseResult(Platform("d", "D"), contents=[ImageContent(path)])))
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(b"x" if damage == "truncate" else b"")
    assert DiskResultCache(tmp_path).load("key") is None


def test_invalid_typed_result_is_not_published(tmp_path):
    result = ParseResult(Platform("d", "D"))
    result.title = object()
    cache = DiskResultCache(tmp_path)
    assert not asyncio.run(cache.save("key", result))
    result.title = 123
    assert not asyncio.run(cache.save("key", result))


def test_manifest_directory_symlink_escape_is_rejected(tmp_path):
    root, outside = tmp_path / "cache", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "disk_results").symlink_to(outside, target_is_directory=True)
    cache = DiskResultCache(root)
    assert not asyncio.run(cache.save("key", ParseResult(Platform("d", "D"))))
    assert not list(outside.iterdir())


def test_failed_atomic_replace_keeps_previous_snapshot(tmp_path):
    cache = DiskResultCache(tmp_path)
    assert asyncio.run(cache.save("key", ParseResult(Platform("d", "D"), text="old")))
    with patch("astrbot_plugin_parser_x.core.disk_result_cache.os.replace", side_effect=OSError("disk full")):
        assert not asyncio.run(cache.save("key", ParseResult(Platform("d", "D"), text="new")))
    assert cache.load("key").text == "old"
    assert not list(tmp_path.rglob(".pending-*"))


def test_pending_download_is_shielded_from_save_cancellation(tmp_path):
    async def run():
        path = tmp_path / "media"
        path.write_bytes(b"complete")
        started, release = asyncio.Event(), asyncio.Event()

        async def download():
            started.set()
            await release.wait()
            return path

        media = asyncio.create_task(download())
        save = asyncio.create_task(DiskResultCache(tmp_path).save("key", ParseResult(Platform("d", "D"), contents=[ImageContent(media)])))
        await started.wait()
        await asyncio.sleep(0)
        assert not save.done()
        save.cancel()
        with pytest.raises(asyncio.CancelledError):
            await save
        assert not media.cancelled()
        release.set()
        assert await media == path
        assert DiskResultCache(tmp_path).load("key") is None
        assert await DiskResultCache(tmp_path).save("key", ParseResult(Platform("d", "D"), contents=[ImageContent(media)]))

    asyncio.run(run())
