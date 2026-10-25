"""Regression coverage for creation/write-bounded card and result retention."""
import asyncio
import os
import time

from unittest.mock import patch
import re

import pytest

from astrbot_plugin_parser_x.core.disk_result_cache import DiskResultCache
from .test_comment_card_cache import plugin, result


@pytest.mark.parametrize('reuse', ['stable', 'marker'])
def test_card_reuse_expires_at_24h_without_cleanup(tmp_path, reuse):
    async def run():
        obj = plugin(tmp_path)
        parsed = result(None)
        card = await obj._build_text_card_content(parsed)
        old = time.time() - 86401
        os.utime(card.path_task, (old, old))
        await obj._build_text_card_content(parsed if reuse == 'marker' else result(None))
        assert len(obj.render_calls) == 2
        assert card.path_task.stat().st_mtime > old
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['save', 'load'])
def test_fresh_result_manifest_cannot_extend_card_retention(tmp_path, stage):
    async def run():
        obj = plugin(tmp_path)
        parsed = result(None)
        card = await obj._build_text_card_content(parsed)
        cache = DiskResultCache(tmp_path)
        if stage == 'load':
            assert await cache.save('key', parsed)
        old = time.time() - 86401
        os.utime(card.path_task, (old, old))
        if stage == 'save':
            assert not await cache.save('key', parsed)
        assert cache.load('key') is None
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['save', 'load'])
def test_explicit_card_marker_age_checked_even_for_other_filename(tmp_path, stage):
    async def run():
        path = tmp_path / 'marked.png'
        path.write_bytes(b'card')
        parsed = result(None)
        parsed.extra['_disk_card_path'] = path
        cache = DiskResultCache(tmp_path)
        if stage == 'load':
            assert await cache.save('key', parsed)
        old = time.time() - 86401
        os.utime(path, (old, old))
        if stage == 'save':
            assert not await cache.save('key', parsed)
        assert cache.load('key') is None
    asyncio.run(run())


@pytest.mark.parametrize('age,reused', [(86399, True), (86400, False), (86401, False), (-1, False)])
@pytest.mark.parametrize('marker', [False, True])
def test_card_retention_boundary_and_reads_do_not_touch_write_time(tmp_path, age, reused, marker):
    async def run():
        obj = plugin(tmp_path)
        parsed = result(None)
        card = await obj._build_text_card_content(parsed)
        written = card.path_task.stat().st_mtime
        with patch('astrbot_plugin_parser_x.core.cache_policy.time.time', return_value=written + age):
            await obj._build_text_card_content(parsed if marker else result(None))
        assert len(obj.render_calls) == (1 if reused else 2)
        if reused:
            assert card.path_task.stat().st_mtime == written
    asyncio.run(run())


def test_expired_result_does_not_rebind_older_card(tmp_path):
    from ..delivery.test_unified_delivery import CaptureEvent, make_plugin, document

    async def run():
        calls = []

        class Parser:
            async def parse(self, keyword, searched):
                calls.append('parse')
                async def comments():
                    calls.append('comments')
                    return document()
                parsed = result(comments)
                parsed.extra['render_text_card'] = True
                return parsed

        match = re.search(r'https://.*', 'https://example/1?token=a')
        first = make_plugin(tmp_path)
        parsed = await first._parse_with_cache(Parser(), 'test', match, match.group())
        await first._send_parse_result(CaptureEvent(), parsed)
        path = parsed.extra['_disk_card_path']
        old = time.time() - 86401
        os.utime(path, (old, old))
        fresh = make_plugin(tmp_path)
        with patch('astrbot_plugin_parser_x.core.disk_result_cache.time.time', return_value=time.time() + 86401):
            restored = await fresh._parse_with_cache(Parser(), 'test', match, match.group())
        await fresh._send_parse_result(CaptureEvent(), restored)
        assert calls == ['parse', 'comments', 'parse', 'comments']
        assert len(fresh.text_renderer.calls) == 1
        assert path.stat().st_mtime > old
    asyncio.run(run())


def test_shared_behavior_version_invalidates_cards_and_result_keys(tmp_path):
    from astrbot_plugin_parser_x.core import cache_policy

    async def run():
        class Parser:
            async def parse(self, keyword, searched):
                return result(None)

        async def keys():
            obj = plugin(tmp_path)
            match = re.search(r'https://.*', 'https://example/1?token=a')
            parsed = await obj._parse_with_cache(Parser(), 'test', match, match.group())
            card = await obj._build_text_card_content(parsed)
            return parsed.extra['_disk_result_key'], card.path_task

        before = await keys()
        assert before == await keys()
        with patch.object(cache_policy, 'CACHE_BEHAVIOR_VERSION', 'test-new-behavior', create=True):
            after = await keys()
        assert before[0] != after[0]
        assert before[1] != after[1]
    asyncio.run(run())
