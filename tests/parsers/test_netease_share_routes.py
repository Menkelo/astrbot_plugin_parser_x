"""NetEase app shares must reach the existing single-song parser."""
import pytest
from astrbot_plugin_parser_x.core.parsers.netease import NeteaseParser
from astrbot_plugin_parser_x.core.utils import extract_json_url


@pytest.mark.parametrize('url,keyword', [
    ('https://163cn.tv/bjBU0F4U', '163cn.tv'),
    ('https://y.music.163.com/m/song?id=3444912369&uct2=share', 'music.163.com'),
    ('https://music.163.com/song?id=186016', 'music.163.com'),
    ('https://music.163.com/#/song?id=186016', 'music.163.com'),
])
def test_share_routes(url, keyword):
    key, match = NeteaseParser.search_url(url)
    assert key == keyword
    assert match.group(0) == url


def test_short_link_stops_at_nonbreaking_space():
    _, match = NeteaseParser.search_url('分享歌曲: https://163cn.tv/bjBU0F4U\xa0(来自@网易云音乐)')
    assert match.group(0) == 'https://163cn.tv/bjBU0F4U'


def test_audio_filename():
    assert NeteaseParser._audio_filename('苏也莉', '春泥 Afro House') == '苏也莉 - 春泥 Afro House.mp3'
    assert NeteaseParser._audio_filename('A / B', '曲名:测试?') == 'A _ B - 曲名_测试_.mp3'
    assert NeteaseParser._audio_filename('', '', preview=True) == '未知歌手 - 未知歌曲（试听）.mp3'


def test_audio_delivery_preserves_cache_path():
    from pathlib import Path
    from astrbot_plugin_parser_x.core.data import AudioContent
    from astrbot_plugin_parser_x.core.delivery import DeliveryMixin
    path = Path('/tmp/hashed.mp3')
    content = AudioContent(path, 238, '苏也莉 - 春泥 Afro House.mp3')
    segment = DeliveryMixin._convert_to_seg(None, content, path)
    assert segment.name == content.name
    assert segment.file_ == str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [True, False])
async def test_audio_only_without_cards_or_comments(available):
    from pathlib import Path
    from types import SimpleNamespace
    from astrbot_plugin_parser_x.core.data import AudioContent

    calls = []
    async def request(path, params):
        calls.append(path)
        if path == '/api/song/detail/':
            return {'code': 200, 'songs': [{'id': 1, 'name': '歌曲', 'artists': [{'name': '歌手'}],
                                          'album': {'name': '专辑'}, 'duration': 1000}]}
        return {'code': 200, 'data': [{'id': 1, 'code': 200,
                                     'url': 'https://m1.music.126.net/a.mp3' if available else None}]}
    parser = SimpleNamespace(_request_json=request, _media_url=NeteaseParser._media_url,
                             _audio_filename=NeteaseParser._audio_filename,
                             create_audio_content=lambda url, duration: AudioContent(Path('/tmp/a.mp3'), duration),
                             result=NeteaseParser.result)
    _, match = NeteaseParser.search_url('https://music.163.com/song?id=1')
    result = await NeteaseParser.parse_song(parser, match)
    assert result.extra['render_text_card'] is False
    assert 'text_card_comment_factory' not in result.extra
    assert len(calls) == 2
    assert len(result.contents) == int(available)
    assert result.text == ('' if available else result.extra['info'])


@pytest.mark.asyncio
@pytest.mark.parametrize('cookie,expected', [('', None), ('MUSIC_U=synthetic', 'MUSIC_U=synthetic'),
                                           ('bad\r\ninjected', None), (None, None)])
async def test_login_cookie_scoped_to_official_api(monkeypatch, cookie, expected):
    import aiohttp
    from types import SimpleNamespace
    requests = []
    class Response:
        status = 200
        url = 'https://music.163.com/api/song/detail/'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def iter_chunked(self, size):
            yield b'{"code":200,"songs":[]}'
        @property
        def content(self): return self
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    parser = SimpleNamespace(config={'cookies': {'netease_cookie': cookie, 'bili_ck': 'unrelated'}})
    await NeteaseParser._request_json(parser, '/api/song/detail/', {'ids': '[1]'})
    url, kwargs = requests[0]
    assert url == 'https://music.163.com/api/song/detail/'
    assert kwargs['headers'].get('Cookie') == expected
    assert kwargs['allow_redirects'] is False
    with pytest.raises(Exception, match='路径不允许'):
        await NeteaseParser._request_json(parser, 'https://untrusted.example/api')
    assert len(requests) == 1


def test_json_share_short_link_is_supported():
    url = 'https://163cn.tv/bjBU0F4U'
    assert extract_json_url({'meta': {'music': {'jumpUrl': url}}}) == url
