"""Real-share routing and persisted video-comment delivery regressions."""
import asyncio
from types import SimpleNamespace

from astrbot_plugin_parser_x.core.data import VideoContent
from astrbot_plugin_parser_x.core.disk_result_cache import DiskResultCache
from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo, CdnUrl
from astrbot_plugin_parser_x.main import ParserXPlugin


def test_f_shortlink_routes_through_pc_redirect_to_mobile_photo(tmp_path):
    """The observed /f URL is already routed; its token is not a photo id."""
    async def run():
        parser = KuaiShouParser({'cache_dir': str(tmp_path)}, None)
        resolved = 'https://www.kuaishou.com/short-video/3xmmnz8yiftpsyi?shareToken=X-bBjMJo6Mvaaoa'
        final = resolved.replace('www.kuaishou.com/short-video/', 'm.gifshow.com/fw/photo/')
        calls = []
        async def get(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return SimpleNamespace(status_code=302, headers={'Location': resolved})
            return SimpleNamespace(status_code=200, url=final, text='window.INIT_STATE={"photo":{"result":1,"photo":{"caption":"作品","timestamp":0,"userId":1945543329,"mainMvUrls":[{"cdn":"cdn","url":"https://cdn.example/video.mp4"}]}}}</script>')
        parser._session = SimpleNamespace(get=get)
        parser.create_video_content = lambda *args, **kwargs: VideoContent(None, None)
        identities = []
        async def fetch(identity, *, owner_id=''):
            identities.append((identity, owner_id))
        parser.comment_feed.fetch_document = fetch
        keyword, match = parser.search_url('https://www.kuaishou.com/f/X-bBjMJo6Mvaaoa')
        result = await parser.parse(keyword, match)
        assert calls == ['https://www.kuaishou.com/f/X-bBjMJo6Mvaaoa', resolved]
        assert result.title == '作品'
        assert identities == []
        await result.extra['text_card_comment_factory']()
        assert identities == [('3xmmnz8yiftpsyi', '1945543329')]
    asyncio.run(run())


def test_persisted_kuaishou_video_comment_card_survives_missing_factory(tmp_path):
    """The message entry loads disk snapshots before invoking the parser."""
    async def run():
        video = tmp_path / 'video.mp4'
        video.write_bytes(b'video')
        card = tmp_path / 'text_card_kuaishou_complete.png'
        card.write_bytes(b'complete comment card')
        parser = KuaiShouParser({'cache_dir': str(tmp_path)}, None)
        parser.create_video_content = lambda *args, **kwargs: VideoContent(video, None)
        result = parser._build_result_from_photo(Photo(
            caption='答应我看到最后', timestamp=0,
            main_mv_urls=[CdnUrl(cdn='cdn', url='https://cdn.example/video.mp4')],
        ), 'https://v.m.chenzhongtech.com/fw/photo/3xpkhctg8zvz7vk')
        result.extra.update(_disk_card_path=card, _disk_has_comments=True)
        cache = DiskResultCache(tmp_path)
        assert await cache.save('kuaishou-real-message', result)
        restored = cache.load('kuaishou-real-message')
        assert restored is not None and restored.has_video
        assert not callable(restored.extra.get('text_card_comment_factory'))
        assert restored.extra['_disk_has_comments'] is True
        plugin = ParserXPlugin.__new__(ParserXPlugin)
        plugin.config = {'comments': {'enabled': True}}
        plugin.cache_dir = tmp_path
        delivered = await plugin._build_text_card_content(restored)
        assert delivered is not None, 'disk-restored Kuaishou comment card was suppressed before its marker was read'
        assert await delivered.get_path() == card
        assert not restored.extra.get('_video_card_suppressed')
    asyncio.run(run())
