import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_parser_x.core.card_service import CardServiceMixin
from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument
from astrbot_plugin_parser_x.core.async_cache import AsyncSingleFlight


class Harness(CardServiceMixin):
    config = {}


def result(video=True, factory=None):
    return SimpleNamespace(has_video=video, extra={'text_card_comment_factory': factory},
        platform=SimpleNamespace(name='test', display_name='测试'), author=None,
        title='标题', text='简介', extra_info=None, timestamp=None, url='https://example.com',
        formatted_datetime=None)


@pytest.mark.parametrize('video', [True, False])
@pytest.mark.parametrize('state', ['empty', 'error', 'one'])
def test_actual_comment_entries_gate_only_video_cards(tmp_path, video, state):
    async def run():
        h = Harness()
        h.cache_dir = tmp_path
        h.comment_document_cache = AsyncSingleFlight()
        doc = CommentDocument(None, '', '', '总数999', [object()] if state == 'one' else [])
        factory = AsyncMock(side_effect=RuntimeError('offline')) if state == 'error' else AsyncMock(return_value=doc)
        r = result(video, factory)
        h.text_renderer = SimpleNamespace(render_card=AsyncMock())
        card, complete = await h._build_text_card_content_uncached(r, include_comments=True, cache_key='test')
        suppressed = video and state != 'one'
        assert (card is None) == suppressed
        assert bool(r.extra.get('_video_card_suppressed')) == suppressed
        assert h.text_renderer.render_card.await_count == (0 if suppressed else 1)
    asyncio.run(run())


def test_no_factory_video_cannot_reuse_disk_card(tmp_path):
    async def run():
        p = tmp_path / 'old.png'
        p.write_bytes(b'old-card')
        r = result()
        r.extra['_disk_card_path'] = p
        h = Harness()
        assert await h._build_text_card_content(r) is None
        assert r.extra['_video_card_suppressed']
    asyncio.run(run())
