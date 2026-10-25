"""Independent review regressions for cache admission and disk validation."""
import asyncio
from .test_comment_card_cache import plugin, result
from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument, WEIBO_THEME


def test_factory_appearing_does_not_reuse_body_only_card(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        calls = []
        async def factory():
            calls.append(1)
            return CommentDocument(WEIBO_THEME, '1', '', '0', [])
        await obj._build_text_card_content(result(None))
        await obj._build_text_card_content(result(factory))
        assert calls == [1]
        assert len(obj.render_calls) == 2
    asyncio.run(run())


def test_zero_byte_card_is_rendered_again(tmp_path):
    async def run():
        obj = plugin(tmp_path)
        card = await obj._build_text_card_content(result(None))
        card.path_task.write_bytes(b'')
        card = await obj._build_text_card_content(result(None))
        assert card.path_task.stat().st_size > 0
        assert len(obj.render_calls) == 2
    asyncio.run(run())
