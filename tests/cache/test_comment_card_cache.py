"""Comment fetching and rendering are work reuse, never reply suppression."""
import asyncio

import pytest
from types import SimpleNamespace

from astrbot_plugin_parser_x.core.async_cache import AsyncSingleFlight
from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument, WEIBO_THEME
from astrbot_plugin_parser_x.core.data import Author, ParseResult, Platform
from astrbot_plugin_parser_x.main import ParserXPlugin


def plugin(tmp_path):
    obj = ParserXPlugin.__new__(ParserXPlugin)
    obj.config = {}
    obj.cache_dir = tmp_path
    obj.render_calls = []

    async def render(**kwargs):
        obj.render_calls.append(kwargs)
        await asyncio.sleep(0)
        kwargs['out_path'].write_bytes(b'image')
    obj.text_renderer = SimpleNamespace(render_card=render)
    return obj


def result(factory):
    return ParseResult(Platform('weibo', '微博'), text='full body', url='https://example/1',
                       extra={'text_card_comment_factory': factory})


def test_repeated_and_concurrent_cards_reuse_comment_fetch_and_render(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        calls = []

        async def comments():
            calls.append(1)
            await asyncio.sleep(0)
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])

        cards = await asyncio.gather(*(obj._build_text_card_content(result(comments)) for _ in range(6)))
        cards.append(await obj._build_text_card_content(result(comments)))
        assert len(calls) == 1
        assert len(obj.render_calls) == 1
        assert all(card is not None and card.path_task.is_file() for card in cards)
        assert len({id(card) for card in cards}) == len(cards)
    asyncio.run(run())


def test_adding_comment_factory_does_not_reuse_body_only_card(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        async def comments():
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        body = result(None)
        body.extra.clear()
        await obj._build_text_card_content(body)
        await obj._build_text_card_content(result(comments))
        assert obj.render_calls[-1]['comment_document'] is not None
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing', 'empty'])
def test_missing_or_empty_disk_image_refetches_and_renders_again(tmp_path, damage):
    async def run():
        obj = plugin(tmp_path)
        calls = []
        async def comments():
            calls.append(1)
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        card = await obj._build_text_card_content(result(comments))
        if damage == 'missing':
            card.path_task.unlink()
        else:
            card.path_task.write_bytes(b'')
        await obj._build_text_card_content(result(comments))
        assert len(obj.render_calls) == 2
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['none', 'exception', 'invalid', 'timeout'])
def test_failed_comments_retry_without_negative_caching(tmp_path, failure):
    async def run():
        obj = plugin(tmp_path)
        calls = []
        async def comments():
            calls.append(1)
            if len(calls) == 1:
                if failure == 'exception':
                    raise RuntimeError('temporary failure')
                if failure == 'timeout':
                    raise asyncio.TimeoutError()
                return [] if failure == 'invalid' else None
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        fallback = await obj._build_text_card_content(result(comments))
        assert obj.render_calls[-1]['comment_document'] is None
        assert 'temporary_' in fallback.path_task.name
        # Even a new instance must not accept an incomplete attempt as stable.
        obj = plugin(tmp_path)
        complete = await obj._build_text_card_content(result(comments))
        assert 'temporary_' not in complete.path_task.name
        await obj._build_text_card_content(result(comments))
        assert len(calls) == 2
        assert len(obj.render_calls) == 1
        assert obj.render_calls[-1]['comment_document'] is not None
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['document', 'render'])
def test_cancelling_first_waiter_does_not_cancel_shared_work(tmp_path, stage):
    async def run():
        obj = plugin(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        async def comments():
            if stage == 'document':
                started.set()
                await release.wait()
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        original_render = obj.text_renderer.render_card
        async def render(**kwargs):
            if stage == 'render':
                started.set()
                await release.wait()
            await original_render(**kwargs)
        obj.text_renderer.render_card = render
        first = asyncio.create_task(obj._build_text_card_content(result(comments)))
        await started.wait()
        second = asyncio.create_task(obj._build_text_card_content(result(comments)))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        card = await second
        assert card.path_task.is_file()
        assert len(obj.render_calls) == 1
        await obj.comment_render_cache.close()
        await obj.comment_document_cache.close()
    asyncio.run(run())


def test_render_failure_retries_without_retaining_successful_document(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        calls = []
        async def comments():
            calls.append(1)
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        original = obj.text_renderer.render_card
        async def broken(**kwargs):
            raise RuntimeError('render failed')
        obj.text_renderer.render_card = broken
        with pytest.raises(RuntimeError, match='render failed'):
            await obj._build_text_card_content(result(comments))
        obj.text_renderer.render_card = original
        assert await obj._build_text_card_content(result(comments)) is not None
        assert len(calls) == 2
    asyncio.run(run())


def test_singleflight_closes_pending_work():
    async def run():
        cache = AsyncSingleFlight()
        async def value():
            return 1
        started = asyncio.Event()
        async def blocked():
            started.set()
            await asyncio.Event().wait()
        waiter = asyncio.create_task(cache.get('pending', blocked))
        await started.wait()
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with pytest.raises(RuntimeError, match='closed'):
            await cache.get('a', value)
    asyncio.run(run())


@pytest.mark.parametrize('change', ['platform', 'info_title', 'layout', 'meta', 'settings',
                                  'body', 'url', 'platform_key', 'author', 'comments_off'])
def test_every_render_input_invalidates_output_identity(tmp_path, change):
    async def run():
        obj = plugin(tmp_path)
        async def comments():
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        first = result(comments)
        before = await obj._build_text_card_content(first)
        second = result(comments)
        if change == 'platform':
            second.extra['card_platform_name'] = 'new label'
        elif change == 'settings':
            obj.config['comments'] = {'limit': 3}
        elif change == 'body':
            second.text = 'different full body'
        elif change == 'url':
            second.url = 'https://example/2'
        elif change == 'platform_key':
            second.platform = Platform('other', 'other')
        elif change == 'author':
            second.author = Author('other author')
        elif change == 'comments_off':
            pass
        else:
            second.extra[{'info_title': 'card_info_title', 'layout': 'text_card_layout',
                          'meta': 'text_card_meta'}[change]] = 'new value'
        after = await obj._build_text_card_content(second, include_comments=change != 'comments_off')
        assert before.path_task != after.path_task
        assert len(obj.render_calls) == 2
    asyncio.run(run())
