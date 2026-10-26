"""Offline replay of the captured feed74573212 page; never contacts Coolapk/QQ."""
from html import unescape
from pathlib import Path
import re
import socket

import pytest

from astrbot_plugin_parser_x.core.parsers.coolapk import CoolapkParser


URL = 'https://www.coolapk.com/feed/74573212'
AVATAR = 'http://avatar.coolapk.com/data/001/10/21/36_avatar_middle.jpg?t=1789956905'
COVER = 'http://image.coolapk.com/feed_cover/2026/1024/07/1102136_42beb22d_6402_8789_165@1843x829.jpg'
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'coolapk_74573212.html'


class Downloads:
    def __init__(self):
        self.urls = []

    def download_img(self, url, **kwargs):
        self.urls.append(url)
        return Path('/tmp/offline-coolapk.jpg')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('offline Coolapk tests must not open network sockets')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)


@pytest.fixture
def parser(tmp_path):
    return CoolapkParser({'cache_dir': str(tmp_path)}, Downloads())


def test_real_feed74573212_has_author_avatar_platform_and_independent_cover(parser):
    result = parser._parse_html(FIXTURE.read_text(encoding='utf-8'), URL)
    assert result.author is not None
    assert result.author.name == 'Check6'
    assert result.platform.name == 'coolapk'
    assert result.platform.display_name == '酷安'
    assert result.extra['text_card_avatar'] == AVATAR
    assert result.extra['text_card_media'] == COVER
    assert result.extra['text_card_media_fit'] == 'cover'
    assert len(result.contents) == 21
    assert COVER not in parser.downloader.urls
    assert AVATAR not in parser.downloader.urls


def test_long_article_single_image_stays_between_paragraphs_without_implicit_cover(parser):
    image = 'https://image.coolapk.com/feed/inline.jpg'
    html = f'''<div class="message-title">长文</div>
        <div class="feed-article-message"><p>前言</p><img src="{image}"><p>后记</p></div>'''
    result = parser._parse_html(html, URL)
    assert result.extra['text_card_flow'] == [
        {'type': 'text', 'text': '前言'},
        {'type': 'image', 'url': image},
        {'type': 'text', 'text': '后记'},
    ]
    assert 'text_card_media' not in result.extra
    assert not result.extra.get('text_card_detach_images')
    assert result.extra['delivery_text_card_consume_non_video'] is True
    assert parser.downloader.urls == [image]


@pytest.mark.parametrize('cover', [None, COVER])
def test_long_article_trailing_images_remain_in_flow(parser, cover):
    images = ['https://image.coolapk.com/feed/one.jpg',
              'https://image.coolapk.com/feed/two.jpg']
    html = (f'<div class="message-cover"><img src="{cover}"></div>' if cover else '')
    html += '<div class="message-title">长文</div><div class="feed-article-message"><p>正文</p>'
    html += ''.join(f'<img src="{image}">' for image in images) + '</div>'
    result = parser._parse_html(html, URL)
    assert result.extra['text_card_flow'] == [
        {'type': 'text', 'text': '正文'},
        *[{'type': 'image', 'url': image} for image in images],
    ]
    assert result.extra.get('text_card_media') == cover
    assert not result.extra.get('text_card_detach_images')
    assert result.extra['delivery_text_card_consume_non_video'] is True
    assert result.extra['image_post_card_in_forward'] is True
    assert parser.downloader.urls == images


def test_long_article_explicit_cover_does_not_remove_matching_body_image(parser):
    html = f'''<div class="message-cover"><img src="{COVER}"></div>
        <div class="message-title">长文</div>
        <div class="feed-article-message"><p>正文</p><img src="{COVER}"><p>结语</p></div>'''
    result = parser._parse_html(html, URL)
    assert result.extra['text_card_media'] == COVER
    assert result.extra['text_card_flow'] == [
        {'type': 'text', 'text': '正文'}, {'type': 'image', 'url': COVER},
        {'type': 'text', 'text': '结语'},
    ]
    assert parser.downloader.urls == [COVER]


def test_real_article_keeps_all_21_images_and_paragraphs_in_dom_order(parser):
    html = FIXTURE.read_text(encoding='utf-8')
    # Independent fixture oracle: only the article region, not site chrome,
    # avatar, cover, or the JavaScript photo viewer's duplicate image list.
    article = html.split('<div class="feed-article-message"', 1)[1].split('<script>', 1)[0]
    article = article.split('>', 1)[1]
    expected = []
    for match in re.finditer(r'<p\b[^>]*>(.*?)</p>|<img\b[^>]*src="([^"]+)"', article, re.S):
        paragraph, image = match.groups()
        if image:
            expected.append({'type': 'image', 'url': 'https:' + image if image.startswith('//') else image})
        else:
            text = re.sub(r'<br\s*/?>', '\n', paragraph)
            text = unescape(re.sub(r'<[^>]*>', '', text))
            text = '\n'.join(line.strip() for line in text.splitlines() if line.strip())
            expected.append({'type': 'text', 'text': text})
    expected_images = [block['url'] for block in expected if block['type'] == 'image']
    assert len(expected_images) == 21
    result = parser._parse_html(html, URL)
    assert result.extra['text_card_flow'] == expected
    assert parser.downloader.urls == expected_images
    assert len(result.contents) == 21
    assert not result.extra.get('text_card_detach_images')
    assert result.extra['delivery_text_card_consume_non_video'] is True


def test_short_feed_retains_existing_single_image_promotion(parser):
    image = 'https://image.coolapk.com/feed/short.jpg'
    result = parser._parse_html(f'<div class="feed-message">短帖</div>'
        f'<div class="message-image-group"><img src="{image}"></div>', URL)
    assert result.extra['text_card_media'] == image
    assert result.extra['text_card_flow'] == [{'type': 'text', 'text': '短帖'}]
