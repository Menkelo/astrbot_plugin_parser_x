"""Real zvideo object ids must never be treated as lens playback ids."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_parser_x.core.parsers.zhihu import ZhihuParser, ParseException

OBJECT_ID = '2031756098479334275'
LENS_ID = '2031756097627894832'
COVER = 'https://picx.zhimg.com/cover.jpg'


def make_parser():
    parser = ZhihuParser({}, SimpleNamespace(download_img=lambda *a, **k: None))
    parser.create_video_content = lambda url, cover, **kw: (url, cover, kw)
    parser.result = lambda **kw: SimpleNamespace(**kw)
    return parser


def payload():
    return {'id': OBJECT_ID, 'type': 'zvideo', 'title': '人类幼崽搞笑视频',
            'author': {'name': '十六盘点'}, 'image_url': COVER,
            'video': {'video_id': LENS_ID, 'playlist': {
                'ld': {'play_url': 'https://vdn3.vzuu.com/low.mp4'},
                'hd': {'play_url': 'https://vdn3.vzuu.com/high.mp4', 'duration': 312}}}}


def test_real_zvideo_uses_object_metadata_and_lowercase_playlist():
    p = make_parser()
    p.http_get = AsyncMock(return_value=SimpleNamespace(status_code=200, json=payload))
    p._video_lookup = AsyncMock(side_effect=AssertionError('must not fetch object id from lens'))
    r = asyncio.run(p.parse_zvideo({'video': OBJECT_ID}))
    assert r.title == '人类幼崽搞笑视频'
    assert r.author.name == '十六盘点'
    assert r.contents[0][0].endswith('/high.mp4')
    assert r.extra['text_card_media'] == COVER
    p.http_get.assert_awaited_once_with(f'https://www.zhihu.com/api/v4/zvideos/{OBJECT_ID}')


def test_zvideo_fallback_uses_distinct_lens_id():
    p = make_parser()
    d = payload()
    d['video']['playlist'] = {}
    p.http_get = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: d))
    p._video_content = AsyncMock(return_value='resolved-video')
    asyncio.run(p.parse_zvideo({'video': OBJECT_ID}))
    p._video_content.assert_awaited_once_with(LENS_ID, COVER)


@pytest.mark.parametrize('change', [{'id': '1'}, {'type': 'answer'}, {'invisible': True}])
def test_rejects_wrong_or_hidden_object(change):
    p = make_parser()
    d = payload()
    d.update(change)
    p.http_get = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: d))
    with pytest.raises(ParseException):
        asyncio.run(p.parse_zvideo({'video': OBJECT_ID}))


def test_collection_is_not_registered_or_trusted():
    p = make_parser()
    with pytest.raises(ParseException):
        p.search_url('https://www.zhihu.com/collection/20000000')
    assert not p._is_content_url('https://www.zhihu.com/api/v4/collections/20000000')
    assert p._is_content_url(f'https://www.zhihu.com/api/v4/zvideos/{OBJECT_ID}')
    assert not p._is_content_url(f'https://www.zhihu.com/api/v4/zvideos/{OBJECT_ID}/anything')
