from types import SimpleNamespace

from core.parsers.douyin.comment_feed import DouyinCommentFeed


def test_comment_image_prefers_explicit_browser_format_over_heic_image_url():
    feed = DouyinCommentFeed(SimpleNamespace(config={}, headers={}, cookies=''))
    urls = [
        'https://p26-sign.douyinpic.com/test~tplv-7.image?x-signature=original',
        'https://p11-sign.douyinpic.com/test~tplv-7.image?x-signature=other',
        'https://p26-sign.douyinpic.com/test~tplv-7.jpeg?x-signature=jpeg%2Bsignature',
    ]
    entry = feed.adapt_comment({'text': '', 'user': {'nickname': '不羡江中仙'},
        'image_list': [{'origin_url': {'url_list': urls}}]}, {}, {})
    assert entry is not None
    assert entry.images == [urls[2]]


def test_comment_image_keeps_unknown_url_when_no_explicit_browser_format():
    url = 'https://cdn.example/test.image?signature=unchanged'
    assert DouyinCommentFeed._image_from_object({'url_list': [url]}) == url
