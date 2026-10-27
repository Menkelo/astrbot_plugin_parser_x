from types import SimpleNamespace

from core.parsers.weibo_comment import WeiboCommentFeed


def test_single_picture_is_preserved_in_root_and_reply():
    feed = WeiboCommentFeed(SimpleNamespace(config={}, headers={}, cookies=''))
    url = 'https://wx1.sinaimg.cn/large/picture.jpg'
    root = {'user': {'id': 1, 'screen_name': 'root'}, 'text': '',
            'pic': {'large': {'url': url}},
            'comments': [{'user': {'id': 2, 'screen_name': 'reply'},
                          'text': '评论配图', 'pic': {'large': {'url': url}}}]}
    entry = feed.adapt_comment(root, {}, {})
    assert entry is not None
    assert entry.images == [url]
    assert entry.first_reply.images == [url]


def test_picture_list_and_single_picture_are_deduplicated():
    feed = WeiboCommentFeed(SimpleNamespace(config={}, headers={}, cookies=''))
    pic = {'url': 'https://wx1.sinaimg.cn/large/picture.jpg', 'large': None}
    entry = feed.adapt_comment({'user': {'id': 1}, 'text': '正文',
                                'pic': pic, 'pics': [pic]}, {}, {})
    assert entry.images == [pic['url']]


def test_real_reply_picture_link_excludes_video_placeholder_and_icon():
    from urllib.parse import quote
    feed = WeiboCommentFeed(SimpleNamespace(config={}, headers={}, cookies=''))
    url = 'https://wx1.sinaimg.cn/large/008kWFp8ly1ii3dys2ic2j30xc0xc7po.jpg'
    text = ('正文 <a href="https://weibo.cn/sinaurl?u=' + quote(url, safe='')
            + '"><span><img src="https://h5.sinaimg.cn/upload/timeline_card_small_photo_default.png">'
              '</span><span>评论配图</span></a>')
    entry = feed.adapt_comment({'user': {'id': 1}, 'text': 'root', 'comments': [
        {'user': {'id': 2}, 'text': text, 'pics': [
            {'type': 'video', 'url': 'https://ss1.sinaimg.cn/large/&690'}]}]}, {}, {})
    assert entry.first_reply.images == [url]
    assert ''.join(part.text for part in entry.first_reply.content) == '正文 '
    assert not any('timeline_card_small_photo' in part.url for part in entry.first_reply.content)


def test_picture_link_does_not_follow_untrusted_target():
    from core.parsers.weibo_comment import _WeiboRichTextParser
    assert _WeiboRichTextParser._picture_target(
        'https://weibo.cn/sinaurl?u=https%3A%2F%2Fevil.example%2Flarge%2Fimage.jpg') is None
