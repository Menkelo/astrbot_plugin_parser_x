import pytest
from core.parsers.ytdlp import AcFunParser
from core.download import VideoInfo


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_acfun_comments_lazy_and_follow_global_switch(tmp_path, enabled):
    class Downloader:
        async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
            return VideoInfo(title='Demo', uploader='Author', description='Body', duration=1)
        async def download_video(self, url, **kwargs):
            path = tmp_path / 'video.mp4'
            path.write_bytes(b'video')
            return path
    parser = AcFunParser({'cache_dir': str(tmp_path), 'comments': {'enabled': enabled, 'display_count': 7}}, Downloader())
    calls = []
    async def fetch(source_id):
        calls.append(source_id)
        return None
    try:
        assert hasattr(parser, 'comment_feed'), 'AcFun has no comment feed integration'
        parser.comment_feed.fetch_document = fetch
        key, match = parser.search_url('https://www.acfun.cn/v/ac48922032?foo=bar')
        result = await parser.parse(key, match)
        assert calls == []
        factory = result.extra.get('text_card_comment_factory')
        assert callable(factory) is enabled
        if enabled:
            await factory()
            assert calls == ['48922032']
            assert result.extra['comment_timeout'] == 90
            assert parser.comment_settings.display_count == 7
        assert (await result.contents[0].get_path()).read_bytes() == b'video'
    finally:
        await parser.close_session()
