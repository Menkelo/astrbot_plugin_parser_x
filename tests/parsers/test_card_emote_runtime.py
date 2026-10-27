"""Body emotes only resolve displayed tokens, never retry failed images in Chromium."""
import asyncio
from types import SimpleNamespace

from core.card_service import CardServiceMixin


class Host(CardServiceMixin):
    pass


def result():
    return SimpleNamespace(
        platform=SimpleNamespace(name='coolapk'), title='[笑哭]', text='hello[笑哭]',
        extra={'card_emotes': {'[笑哭]': 'https://example.invalid/one.png', '[unused]': 'https://example.invalid/two.png'}, 'card_emotes_pending': True},
        extra_info=None,
    )


def test_only_displayed_body_emote_is_requested_and_inlined():
    host = Host()
    calls = []
    class Resolver:
        async def resolve_urls(self, platform, sources):
            calls.append((platform, sources))
            return {'[笑哭]': 'data:image/png;base64,AAAA'}
        async def resolve(self, platform, tokens):
            assert not tokens
            return {}
    host.emote_resolver = Resolver()
    assert asyncio.run(host._resolve_card_emotes(result())) == {'[笑哭]': 'data:image/png;base64,AAAA'}
    assert calls == [('coolapk', {'[笑哭]': 'https://example.invalid/one.png'})]


def test_failed_managed_body_emotes_leave_no_remote_image():
    host = Host()
    class Resolver:
        async def resolve(self, platform, tokens):
            return {}
    host.emote_resolver = Resolver()
    assert asyncio.run(host._resolve_card_emotes(result())) == {}
