import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_parser_x.core.data import ImageContent, VideoContent
from astrbot_plugin_parser_x.core.parsers.weibo import WeiboParser


def make_parser(tmp_path, data):
    downloads = []

    def download(url, **kwargs):
        downloads.append(url)
        return Path(tmp_path) / url.rsplit('/', 1)[-1]

    parser = WeiboParser(
        {'cache_dir': str(tmp_path)},
        SimpleNamespace(download_video=download, download_img=download),
    )

    async def fetch(_bid):
        return data

    parser._fetch_status_data = fetch
    return parser, downloads


def parse(parser):
    return asyncio.run(parser._parse_weibo(re.search(r'(5354579627345002)', '5354579627345002')))


def status(**kwargs):
    return {
        'id': '5354579627345002',
        'text': '微博正文',
        'user': {'id': 123, 'screen_name': '作者', 'verified': True},
        'comments_count': 7,
        'attitudes_count': 8,
        'reposts_count': 9,
        **kwargs,
    }


def test_status_card_omits_metrics_and_author_badge(tmp_path):
    parser, _ = make_parser(tmp_path, status())
    extra = parse(parser).extra
    assert 'card_metrics' not in extra
    assert 'card_author_badge' not in extra
    assert extra['card_kind'] == '微博'


def _patch_cover_bytes(parser, payload: bytes):
    async def http_get(url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            content=payload,
            headers={'Content-Type': 'image/jpeg'},
            text='',
        )

    parser.http_get = http_get


def test_video_status_embeds_cover_because_weibo_images_need_referer(tmp_path):
    """微博图床无 Referer 会 403，头图必须在解析阶段内嵌成 data URI。"""
    data = status(
        page_info={
            'type': 'video',
            'page_pic': {'url': '//wx1.sinaimg.cn/orj480/video-cover.jpg'},
            'media_info': {'stream_url_hd': 'https://cdn.example/video.mp4', 'duration': 42},
        }
    )
    parser, _ = make_parser(tmp_path, data)
    _patch_cover_bytes(parser, b'\xff\xd8\xff\xe0cover-bytes')
    result = parse(parser)
    cover = result.extra.get('text_card_media') or ''
    assert cover.startswith('data:image/jpeg;base64,')


def test_video_status_keeps_remote_cover_when_embedding_fails(tmp_path):
    data = status(
        page_info={
            'type': 'video',
            'page_pic': {'url': '//wx1.sinaimg.cn/orj480/video-cover.jpg'},
            'media_info': {'stream_url_hd': 'https://cdn.example/video.mp4', 'duration': 42},
        }
    )
    parser, _ = make_parser(tmp_path, data)

    async def http_get(url, **kwargs):
        raise RuntimeError('cover unavailable')

    parser.http_get = http_get
    result = parse(parser)
    assert result.extra.get('text_card_media') is None or str(
        result.extra.get('text_card_media')
    ).startswith('https://wx1.sinaimg.cn/orj480/video-cover.jpg')


def test_video_status_uses_page_picture_cover_without_losing_media(tmp_path):
    data = status(
        page_info={
            'type': 'video',
            'page_pic': {'url': '//wx1.sinaimg.cn/large/video-cover.jpg'},
            'media_info': {'stream_url_hd': 'https://cdn.example/video.mp4', 'duration': 42},
        },
        pics=[{'large': {'url': 'https://wx1.sinaimg.cn/large/photo.jpg'}}],
    )
    parser, downloads = make_parser(tmp_path, data)
    result = parse(parser)
    assert result.extra.get('text_card_media') == 'https://wx1.sinaimg.cn/large/video-cover.jpg'
    assert not result.extra.get('image_post_card_in_forward')
    assert len(result.contents) == 2
    assert isinstance(result.contents[0], ImageContent)
    assert isinstance(result.contents[1], VideoContent)
    assert result.contents[1].duration == 42
    assert downloads == ['https://wx1.sinaimg.cn/large/photo.jpg', 'https://cdn.example/video.mp4']
    assert result.delivery is not None


def test_status_author_avatar_is_embedded_and_cached_with_weibo_headers(tmp_path):
    import base64

    parser, _ = make_parser(tmp_path, status(user={
        'screen_name': '作者',
        'avatar_hd': '//tvax1.sinaimg.cn/avatar.png',
        'avatar_large': 'https://tvax1.sinaimg.cn/lower-quality.png',
    }))
    parser.cookie = 'test-cookie'
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=')
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=png, headers={'Content-Type': 'image/png'})

    parser.http_get = get
    expected = 'data:image/png;base64,' + base64.b64encode(png).decode()
    assert parse(parser).extra['text_card_avatar'] == expected
    assert parse(parser).extra['text_card_avatar'] == expected
    assert len(calls) == 1
    assert calls[0][0] == 'https://tvax1.sinaimg.cn/avatar.png'
    assert calls[0][1]['headers']['Referer'] == 'https://m.weibo.cn/'
    assert calls[0][1]['headers']['Cookie'] == 'test-cookie'
    assert calls[0][1]['headers']['User-Agent'] == parser.headers['User-Agent']


def test_status_avatar_fetch_failure_retains_remote_fallback(tmp_path):
    parser, _ = make_parser(tmp_path, status(user={
        'screen_name': '作者', 'avatar_hd': 'https://tvax1.sinaimg.cn/avatar.png',
    }))

    async def get(_url, **_kwargs):
        return SimpleNamespace(status_code=403)

    parser.http_get = get
    assert parse(parser).extra['text_card_avatar'] == 'https://tvax1.sinaimg.cn/avatar.png'


def test_image_status_without_video_keeps_gallery_delivery(tmp_path):
    images = ['https://wx1.sinaimg.cn/large/one.jpg', 'https://wx1.sinaimg.cn/large/two.jpg']
    parser, downloads = make_parser(tmp_path, status(pics=[{'url': url} for url in images]))
    result = parse(parser)
    assert result.extra['text_card_media'] == images[0]
    assert result.extra['text_card_detach_images'] is True
    assert result.extra['delivery_text_card_consume_non_video'] is False
    assert result.extra['image_post_card_in_forward'] is True
    assert downloads == images
    assert len(result.contents) == 2
    assert all(isinstance(item, ImageContent) for item in result.contents)


def test_video_status_without_page_picture_has_no_invented_cover(tmp_path):
    parser, _ = make_parser(tmp_path, status(page_info={
        'type': 'video',
        'media_info': {'stream_url_hd': 'https://cdn.example/video.mp4'},
    }))
    result = parse(parser)
    assert not result.extra.get('text_card_media')
    assert isinstance(result.contents[0], VideoContent)
