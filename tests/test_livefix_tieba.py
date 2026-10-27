import pytest
from astrbot_plugin_parser_x.core.parsers.tieba import TiebaParser


def test_whitespace_only_body_has_no_card_flow():
    r = parser()._build_result(payload([{'type': 0, 'text': '\n'}]), '123')
    assert not r.extra.get('text_card_flow')
    assert not r.text


def test_label_less_emote_is_kept_as_inline_image():
    r = parser()._build_result(payload([{'type': 2, 'text': 'image_emoticon38'}]), '123')
    assert r.extra['card_emotes']['[image_emoticon38]'].endswith('/image_emoticon38.png')


def test_portrait_query_is_stripped_and_safe_https_host_is_used():
    from astrbot_plugin_parser_x.core.parsers.tieba_media import portrait_url
    assert portrait_url('tb.1.safe_token?t=123') == 'https://himg.bdimg.com/sys/portrait/item/tb.1.safe_token'
    for bad in ('../secret', '%2e%2e%2fsecret', 'abc/def', 'https://evil/a', '..', 'abc#x', 'abc\\x'):
        assert portrait_url(bad) == ''


def fake_downloader():
    """Minimal stand-in so create_author/create_video_content link real tasks."""
    from pathlib import Path
    from types import SimpleNamespace

    class StubTask:
        def __init__(self, suffix):
            self._path = Path('/tmp') / 'fake' / suffix
            self.cancelled = False

        def done(self):
            return True

        def result(self, *args, **kwargs):
            return self._path

        def cancel(self):
            self.cancelled = True

        def add_done_callback(self, callback):
            callback(self)

    return SimpleNamespace(
        download_img=lambda url, **kwargs: StubTask('avatar.img'),
        download_video=lambda url, **kwargs: StubTask('video.mp4'),
    )


def parser():
    from types import SimpleNamespace
    p = object.__new__(TiebaParser)
    p.comment_settings = SimpleNamespace(enabled=False, display_count=3, timeout=5)
    p.config = {}
    p.headers = {}
    p.downloader = fake_downloader()
    return p


def payload(content):
    return {'error_code': 0, 'thread': {'id': '123', 'post_id': '10', 'title': 'test'},
            'post_list': [{'id': '10', 'floor': 1, 'author_id': '7', 'content': content}],
            'user_list': [{'id': '7', 'name': 'user', 'portrait': 'safe?t=12'}]}


@pytest.mark.asyncio
async def test_client_comments_attach_observed_nested_reply_and_bound_request():
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    p = parser()
    p.comment_settings.enabled = True
    calls = []
    async def request(method, url, **kwargs):
        calls.append(kwargs['data'])
        return {'error_code': 0, 'thread': {'id': '123'},
                'user_list': [{'id': 7, 'name_show': 'root', 'portrait': 'safe?t=1'},
                              {'id': 8, 'name_show': 'child', 'portrait': 'other?t=2'}],
                'post_list': [{'id': 11, 'floor': 2, 'author_id': 7,
                    'content': [{'type': 0, 'text': 'root text'}],
                    'sub_post_list': {'pid': 11, 'sub_post_list': [
                        {'id': 12, 'author_id': 8, 'content': [{'type': 0, 'text': 'child text'}]}]}}]}
    p._request_json = request
    async def html_forbidden(*args, **kwargs):
        raise AssertionError('working client path must not use HTML')
    p._request_bytes = html_forbidden
    doc = await TiebaCommentFeed(p).fetch_document('123', '7')
    assert doc is not None
    assert doc.entries[0].first_reply.content[0].text == 'child text'
    assert doc.entries[0].first_reply.author.avatar.endswith('/other')
    assert doc.entries[0].author.is_up
    assert len(calls) == 1
    assert calls[0]['with_floor'] == '1'
    assert int(calls[0]['rn']) <= 30


@pytest.mark.asyncio
async def test_comment_transport_failure_logs_status_and_retains_post(caplog):
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    from astrbot_plugin_parser_x.core.exception import ParseException
    p = parser()
    p.comment_settings.enabled = True
    async def failure(*args, **kwargs):
        raise ParseException('贴吧接口 HTTP 403')
    p._request_json = failure
    p._request_bytes = failure
    with caplog.at_level('WARNING'):
        assert await TiebaCommentFeed(p).fetch_document('123') is None
    assert 'HTTP 403' in caplog.text
    assert p._build_result(payload([{'type': 0, 'text': 'body'}]), '123') is not None


def test_type2_inline_tokens_merge_with_text_and_keep_newlines():
    result = parser()._build_result(payload([
        {'type': 0, 'text': 'before\n'},
        {'type': 2, 'c': '你懂的', 'text': 'image_emoticon68', 'is_native_app': 0},
        {'type': 0, 'text': 'after\nlast'},
    ]), '123')
    assert result.text == 'before\n[你懂的]after\nlast'
    assert result.extra['text_card_flow'] == [{'type': 'text', 'text': result.text}]
    assert result.extra['card_emotes']['[你懂的]'] == 'https://tb2.bdstatic.com/tb/editor/images/client/image_emoticon68.png'
    assert not result.contents


def test_unknown_emote_asset_stays_literal_not_arbitrary_image():
    result = parser()._build_result(payload([
        {'type': 2, 'c': 'unknown', 'text': '../evil', 'src': 'http://evil/large.png'},
    ]), '123')
    assert result.text == '[unknown]'
    assert 'card_emotes' not in result.extra
    assert not result.contents


@pytest.mark.asyncio
async def test_client_comment_thread_mismatch_is_not_rendered():
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    p = parser()
    async def request(*args, **kwargs):
        return {'error_code': 0, 'thread': {'id': 'wrong'}, 'post_list': [], 'user_list': []}
    p._request_json = request
    with pytest.raises(ValueError, match='identity'):
        await TiebaCommentFeed(p)._fetch_client('123', '')


def test_root_portrait_uses_shared_normalization():
    result = parser()._build_result(payload([{'type': 0, 'text': 'body'}]), '123')
    assert result.extra['text_card_avatar'] == 'https://himg.bdimg.com/sys/portrait/item/safe'


def test_allowlisted_video_http_is_upgraded_without_changing_resource():
    url = 'http://tb-video.bdstatic.com/video/a.mp4?sign=x%2Fy&v=1'
    assert TiebaParser._media_url(url) == 'https' + url[4:]


@pytest.mark.parametrize('url', [
    'http://imgsrc.baidu.com/a.jpg', 'http://tb-video.bdstatic.com.evil/a',
    'http://u@tb-video.bdstatic.com/a', 'http://tb-video.bdstatic.com:80/a',
    'http://tb-video.baidupcs.com/a', 'https://tb-video.bdstatic.com:444/a',
])
def test_media_boundary_stays_closed(url):
    assert TiebaParser._media_url(url) == ''

def test_title_only_post_without_content_is_still_parseable():
    """Live thread 8932713622 returns content=None for title-only posts."""
    body = payload(None)
    body['thread']['title'] = 'title only'
    result = parser()._build_result(body, '123')
    assert result.title == 'title only'
    assert result.text is None
    assert not result.contents


def test_null_content_still_attaches_video_from_thread():
    body = payload(None)
    body['thread']['video_info'] = {'video_url': 'https://tb-video.bdstatic.com/a.mp4',
        'thumbnail_url': 'https://tb2.bdstatic.com/cover.jpg', 'video_duration': 12}
    result = parser()._build_result(body, '123')
    assert len(result.contents) == 1
