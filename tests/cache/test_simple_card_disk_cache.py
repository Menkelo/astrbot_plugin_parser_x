"""Persistent card reuse is disk-only; disabling comments disables cards."""
import asyncio

import pytest

from astrbot_plugin_parser_x.core.async_cache import AsyncSingleFlight
from .test_comment_card_cache import plugin, result


def test_singleflight_retains_no_completed_values():
    async def run():
        cache = AsyncSingleFlight()
        calls = []
        async def produce():
            calls.append(1)
            await asyncio.sleep(0)
            return len(calls)
        assert await asyncio.gather(*(cache.get('key', produce) for _ in range(4))) == [1] * 4
        assert await cache.get('key', produce) == 2
        assert not getattr(cache, '_entries', {})
        await cache.close()
    asyncio.run(run())

def test_card_download_does_not_refresh_mtime_but_source_media_does(tmp_path):
    import os
    from astrbot_plugin_parser_x.core.data import ImageContent
    async def run():
        obj = plugin(tmp_path)
        card = await obj._build_text_card_content(result(None))
        os.utime(card.path_task, (100, 100))
        assert (await obj._download_content(card))[1] == card.path_task
        assert card.path_task.stat().st_mtime == 100
        source = tmp_path / 'source.png'
        source.write_bytes(b'source')
        os.utime(source, (100, 100))
        await obj._download_content(ImageContent(source))
        assert source.stat().st_mtime > 100
    asyncio.run(run())


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('kind', ['image', 'video'])
def test_disabled_comments_preserve_source_and_native_text(tmp_path, native, kind):
    from ..delivery.test_unified_delivery import CaptureEvent, forwarded_parts, make_plugin
    from astrbot.api.message_components import Image, Plain, Video
    from astrbot_plugin_parser_x.core.data import (
        DeliveryBatch, DeliveryPlan, ImageContent, ParseResult, Platform, VideoContent,
    )
    obj = make_plugin(tmp_path)
    obj.config['comments'] = {'enabled': False}
    source_path = tmp_path / ('original.png' if kind == 'image' else 'original.mp4')
    source_path.write_bytes(b'source')
    source = ImageContent(source_path) if kind == 'image' else VideoContent(source_path)
    async def comments():
        pytest.fail('disabled comments must not fetch')
    parsed = ParseResult(Platform('weibo', '微博'), text='native body survives',
                         contents=[source],
                         delivery=DeliveryPlan([DeliveryBatch([source])]) if native else None,
                         extra={'render_text_card': True, 'native_delivery': native,
                                'text_card_comment_factory': comments})
    event = CaptureEvent()
    asyncio.run(obj._send_parse_result(event, parsed))
    parts = [part for chain in event.sent for part in chain] + forwarded_parts(event)
    assert any(isinstance(part, Plain) and 'native body survives' in part.text for part in parts)
    expected = Image if kind == 'image' else Video
    assert any(isinstance(part, expected) and str(source_path) in part.file for part in parts)
    assert obj.text_renderer.calls == []


def test_comments_disabled_skips_card_even_when_disk_is_warm(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        assert await obj._build_text_card_content(result(None)) is not None
        obj.config['comments'] = {'enabled': False}
        assert await obj._build_text_card_content(result(None)) is None
        assert len(obj.render_calls) == 1
    asyncio.run(run())


def test_disk_hit_across_instances_precedes_comment_fetch(tmp_path):
    async def run():
        calls = []
        from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument, WEIBO_THEME
        async def comments():
            calls.append(1)
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        first = plugin(tmp_path)
        card = await first._build_text_card_content(result(comments))
        second = plugin(tmp_path)
        reused = await second._build_text_card_content(result(comments))
        assert reused.path_task == card.path_task
        assert calls == [1]
        assert second.render_calls == []
    asyncio.run(run())
