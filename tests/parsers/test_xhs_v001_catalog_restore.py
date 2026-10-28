"""v0.0.1 小红书首帧库不得再被不工作的 PAG 路径替代。"""
import asyncio
from types import SimpleNamespace

from core.emote_runtime import EmoteResolver
from core.platform_emotes import fallback_emote_map
from core.xiaohongshu_emote_data import XIAOHONGSHU_EMOTE_DATA_URI
from core.unified_renderer import UnifiedCardRenderer
from core.comment_render_base import CommentCanvasBase


def test_all_22_versioned_frames_resolve_offline(tmp_path):
    catalog = fallback_emote_map('xiaohongshu')
    assert len(XIAOHONGSHU_EMOTE_DATA_URI) == 22
    assert all(catalog[k] == v for k, v in XIAOHONGSHU_EMOTE_DATA_URI.items())
    resolver = EmoteResolver(SimpleNamespace(cache_dir=tmp_path))
    result = asyncio.run(resolver.resolve('xiaohongshu', set(XIAOHONGSHU_EMOTE_DATA_URI)))
    assert result == XIAOHONGSHU_EMOTE_DATA_URI


def test_regular_static_fallback_is_a_runtime_source(tmp_path):
    resolver = EmoteResolver(SimpleNamespace(cache_dir=tmp_path))
    assert resolver.sources('xiaohongshu').static['[微笑R]'].startswith('https://')


def test_emote_css_has_no_global_white_background():
    import re
    from core import unified_renderer, comment_render_base
    for css, selector in [(unified_renderer._UNIFIED_CSS, 'comment-emote'), (comment_render_base._CSS, 'emote')]:
        rule = re.search(r'\.' + selector + r'\{([^}]+)\}', css).group(1)
        assert 'background:' not in rule
        assert 'padding:' not in rule
