import asyncio
from types import SimpleNamespace

from core.card_service import CardServiceMixin
from core.parsers.xiaohongshu import XiaoHongShuParser


def test_xhs_topic_protocol_markers_are_not_displayed():
    raw = '[失望R]\n#异色[话题]# #洛克王国世界[话题]#\n#普通话题'
    assert XiaoHongShuParser._normalize_note_text(raw) == '[失望R]\n#异色 #洛克王国世界\n#普通话题'
    assert XiaoHongShuParser._normalize_note_text('原文[话题]不是标签') == '原文[话题]不是标签'


def test_xhs_body_emote_without_comment_catalog_uses_official_static_source():
    calls = []
    class Host(CardServiceMixin):
        pass
    class Resolver:
        async def resolve_urls(self, platform, sources):
            calls.append((platform, sources))
            return {'[失望R]': 'data:image/png;base64,AAAA'}
        async def resolve(self, platform, tokens):
            assert not tokens
            return {}
    host = Host()
    host.emote_resolver = Resolver()
    result = SimpleNamespace(platform=SimpleNamespace(name='xiaohongshu'),
                             title='标题', text='正文[失望R]', extra={}, extra_info=None)
    assert asyncio.run(host._resolve_card_emotes(result)) == {'[失望R]': 'data:image/png;base64,AAAA'}
    assert calls == [('xiaohongshu', {'[失望R]': 'https://picasso-static.xiaohongshu.com/fe-platform/b862c8f94da375f55805a97c152efeeb5099c149.png'})]
