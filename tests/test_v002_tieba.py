"""Offline tests: synthetic identities, shapes observed anonymously 2026-10-26.
No raw user data or AGPL implementation is embedded in these fixtures.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import pytest
from astrbot_plugin_parser_x.core.parsers.tieba import TiebaParser, ParseException


class Downloader:
    def __init__(self):
        self.calls = []
    def download_img(self, url, **kwargs):
        self.calls.append(("image", url, kwargs))
        return Path("/tmp/image")
    def download_video(self, url, **kwargs):
        self.calls.append(("video", url, kwargs))
        return Path("/tmp/video")


@pytest.fixture
def parser(tmp_path):
    return TiebaParser({"cache_dir": str(tmp_path), "comments": {"enabled": False}}, Downloader())


def payload(content=None, video=None):
    """Minimal anonymized structural fixture, not a copied API response."""
    thread = {"id": "123", "post_id": "456", "title": "主题",
              "origin_thread_info": {"content": [{"type": 0, "text": "转发来源，不是首楼"}]}}
    if video is not None:
        thread["video_info"] = video
    return {"error_code": 0, "thread": thread,
            "post_list": [{"id": "456", "floor": "1", "author_id": "42", "time": "1700000000",
                "content": content if content is not None else [{"type": "0", "text": "正文"}]}],
            "user_list": [{"id": "42", "name": "内部名", "name_show": "楼主", "portrait": "portrait42"}]}


@pytest.mark.asyncio
async def test_first_post_vertical_slice_preserves_order_and_ignores_later_floor(parser):
    data = payload([{"type": "0", "text": "前文"},
        {"type": "3", "origin_src": "https://imgsrc.baidu.com/a.jpg"},
        {"type": "0", "text": "中间"},
        {"type": "3", "origin_src": "https://imgsrc.baidu.com/b.jpg"},
        {"type": "0", "text": "后文"}])
    data["post_list"].insert(0, {"floor": 2, "content": [{"type": 0, "text": "不得当正文"}]})
    async def fetch(tid, page=1):
        assert (tid, page) == ("123", 1)
        return data
    parser._fetch_page = fetch
    result = await parser.parse(*parser.search_url("https://tieba.baidu.com/p/123?pn=9"))
    assert result.text == "前文\n中间\n后文"
    assert result.url == "https://tieba.baidu.com/p/123"
    assert result.author.name == "楼主"
    assert result.author.description == "贴吧用户 42"
    assert result.timestamp == 1700000000
    assert result.extra["text_card_avatar"] == "https://tb.himg.baidu.com/sys/portrait/item/portrait42"
    assert [x["type"] for x in result.extra["text_card_flow"]] == ["text", "image", "text", "image", "text"]
    assert len(result.contents) == 2
    assert "text_card_comment_factory" not in result.extra


@pytest.mark.parametrize("change", ["no_thread_id", "other_thread", "no_first", "two_first",
    "wrong_post_id", "no_content_list", "no_author", "ambiguous_author", "wrong_thread_author", "api_error"])
def test_identity_failures_never_use_origin_or_replies(parser, change):
    data = payload()
    if change == "no_thread_id": del data["thread"]["id"]
    if change == "other_thread": data["thread"]["id"] = "999"
    if change == "no_first": data["post_list"][0]["floor"] = 2
    if change == "two_first": data["post_list"].append(deepcopy(data["post_list"][0]))
    if change == "wrong_post_id": data["thread"]["post_id"] = "999"
    if change == "no_content_list": data["post_list"][0]["content"] = "not-list"
    if change == "no_author": data["user_list"] = []
    if change == "ambiguous_author": data["user_list"].append(deepcopy(data["user_list"][0]))
    if change == "wrong_thread_author": data["thread"]["author"] = {"id": "999", "name": "其他人"}
    if change == "api_error": data["error_code"] = 4
    with pytest.raises(ParseException):
        parser._build_result(data, "123")
    assert parser.downloader.calls == []


def test_missing_first_floor_is_not_replaced_by_second(parser):
    data = payload()
    data["post_list"] = [{"floor": 2, "content": [{"type": 0, "text": "二楼"}]}]
    with pytest.raises(ParseException, match="首楼"):
        parser._build_result(data, "123")


def test_native_video_comes_from_same_thread_only(parser):
    data = payload([{"type": "0", "text": "原生视频"}, {"type": "5",
        "link": "https://tb-video.bdstatic.com/movie.mp4", "src": "https://imgsrc.baidu.com/cover.jpg",
        "during_time": "12", "e_type": "15"}])
    result = parser._build_result(data, "123")
    assert result.has_video and result.contents[0].duration == 12
    assert parser.downloader.calls[0][0:2] == ("video", "https://tb-video.bdstatic.com/movie.mp4")
    data["thread"]["id"] = "999"
    with pytest.raises(ParseException, match="帖子"):
        parser._build_result(data, "123")


@pytest.mark.parametrize("url", [
    "https://tieba.baidu.com/f?kw=python", "https://tieba.baidu.com/live/123",
    "https://evil.tieba.baidu.com/p/123", "https://evil.test/https://tieba.baidu.com/p/123",
    "https://tieba.baidu.com/p/123abc", "https://tieba.baidu.com/p/123/live",
    "https://tieba.baidu.com/p/123?type=live", "https://tieba.baidu.com.evil/p/123",
])
def test_routes_only_public_post_urls(parser, url):
    with pytest.raises(ParseException): parser.search_url(url)


@pytest.mark.asyncio
async def test_page_request_signing_and_no_authentication(parser):
    calls = []
    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return payload()
    parser._request_json = request
    parser.headers["Cookie"] = "must-not-leak"
    await parser._fetch_page("123")
    assert len(calls) == 1  # No TBS/page_pc and no retries.
    method, url, kwargs = calls[0]
    assert method == "POST" and url == "https://tieba.baidu.com/c/f/pb/page"
    fields = dict(kwargs["data"])
    sign = fields.pop("sign")
    assert fields == {"_client_type": "2", "_client_version": "12.35.1.0", "kz": "123",
                      "lz": "1", "pn": "1", "rn": "2", "with_floor": "0"}
    assert sign == hashlib.md5(("".join(f"{k}={v}" for k, v in sorted(fields.items())) + "tiebaclient!!!").encode()).hexdigest().upper()
    assert "Cookie" not in parser._public_headers()


@pytest.mark.asyncio
async def test_request_rejects_unapproved_hosts_before_network(parser):
    for url in ["https://evil.test/c/f/pb/page", "http://tieba.baidu.com/c/f/pb/page",
                "https://tieba.baidu.com/dc/common/tbs", "https://tieba.baidu.com/c/f/pb/page_pc"]:
        with pytest.raises(ParseException, match="主机"):
            await parser._request_json("POST", url)


def test_payload_media_host_and_cookie_boundary(parser):
    parser.headers["Cookie"] = "must-not-leak"
    data = payload([{"type": 0, "text": "安全文字"},
        {"type": 3, "origin_src": "http://127.0.0.1/a.jpg"},
        {"type": 3, "origin_src": "https://imgsrc.baidu.com/a.jpg"}])
    with pytest.raises(ParseException, match="媒体"):
        parser._build_result(data, "123")
    assert parser.downloader.calls == []


@pytest.mark.asyncio
async def test_public_html_root_comments_factory_limit_author_and_failure(parser):
    import html, json
    from astrbot_plugin_parser_x.core.comment_settings import CommentSettings
    parser.comment_settings = CommentSettings.from_config({"comments": {"display_count": 1}}, "tieba")
    fields = {"author": {"user_id": 42, "user_name": "楼主", "portrait": "portrait42"},
              "content": {"thread_id": "123", "post_id": 22, "post_no": 2, "date": "2026-10-26"}}
    page = '<div class="l_post" data-field="' + html.escape(json.dumps(fields), quote=True) + '\"><div class="d_post_content">这是有价值的根回复内容</div></div>'
    calls = []
    async def get(method, url, **kw):
        calls.append(url)
        return page.encode()
    parser._request_bytes = get
    result = parser._build_result(payload(), "123")
    factory = result.extra["text_card_comment_factory"]
    document = await factory()
    assert len(document.entries) == 1
    assert document.entries[0].author.nickname == "楼主" and document.entries[0].author.is_up
    assert document.entries[0].author.avatar.endswith("/portrait42")
    assert document.entries[0].first_reply is None and len(calls) == 1
    async def fail(*args, **kwargs): raise TimeoutError()
    parser._request_bytes = fail
    assert await factory() is None and result.text == "正文"


@pytest.mark.asyncio
async def test_real_card_service_consumes_tieba_comments(parser, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from astrbot_plugin_parser_x.core.card_service import CardServiceMixin
    from astrbot_plugin_parser_x.core.comment_settings import CommentSettings
    from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument, CommentTheme
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    parser.comment_settings = CommentSettings.from_config({}, "tieba")
    document = CommentDocument(CommentTheme("贴吧"), "", "", "根回复", [])
    calls, rendered = [], []
    async def fetch(self, tid, author_id=""):
        calls.append((tid, author_id)); return document
    monkeypatch.setattr(TiebaCommentFeed, "fetch_document", fetch)
    async def render(**kwargs): rendered.append(kwargs)
    class Service(CardServiceMixin): pass
    service = Service()
    service.config, service.cache_dir = {}, tmp_path
    service.text_renderer = SimpleNamespace(render_card=render)
    result = parser._build_result(payload(), "123")
    await service._build_text_card_content(result)
    assert calls == [("123", "42")]
    assert rendered[0]["comment_document"] is document and rendered[0]["text"] == "正文"


@pytest.mark.asyncio
async def test_root_comments_pagination_bound_dedup_and_skip_first_floor(parser):
    import html, json
    from astrbot_plugin_parser_x.core.comment_settings import CommentSettings
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    parser.comment_settings = CommentSettings.from_config({"comments": {"display_count": 20}}, "tieba")
    calls = []
    async def get(method, url, **kw):
        page = int(url.rsplit("=", 1)[1]); calls.append(page); output = ""
        for number in (1, 2, page + 2):
            field = {"author": {"user_id": 7, "user_name": "答主"}, "content": {"thread_id": "123", "post_id": number, "post_no": number}}
            output += '<div class="l_post" data-field="' + html.escape(json.dumps(field), quote=True) + '\"><div class="d_post_content">充分讨论第' + str(number) + '层的问题与观点</div></div>'
        return (output + '<a href="?pn=' + str(page + 1) + '\">next</a>').encode()
    parser._request_bytes = get
    doc = await TiebaCommentFeed(parser).fetch_document("123")
    assert calls == [1, 2, 3] and len(doc.entries) == 4
    assert all(entry.meta_items != ["1楼"] for entry in doc.entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "foreign", "mixed", "page2_foreign"])
async def test_comment_identity_failure_discards_entire_document(parser, mode):
    import html, json
    from astrbot_plugin_parser_x.core.comment_settings import CommentSettings
    from astrbot_plugin_parser_x.core.parsers.tieba_comment import TiebaCommentFeed
    parser.comment_settings = CommentSettings.from_config({"comments": {"enabled": True, "display_count": 20}}, "tieba")
    def floor(tid, post_id):
        meta = {"post_id": post_id, "post_no": post_id}
        if tid is not None:
            meta["thread_id"] = tid
        fields = {"author": {"user_id": 42, "user_name": "合成作者"}, "content": meta}
        return '<div class="l_post" data-field="' + html.escape(json.dumps(fields), quote=True) + '"><div class="d_post_content">合成有效根回复：足够长的讨论内容用于评论身份测试。</div></div>'
    calls = []
    async def get(method, url, **kw):
        calls.append(url)
        if mode == "missing": return floor(None, 2).encode()
        if mode == "foreign": return floor("999", 2).encode()
        if mode == "mixed": return (floor("123", 2) + floor("999", 3)).encode()
        if url.endswith("pn=1"):
            return (floor("123", 2) + '<a href="?pn=2">next</a>').encode()
        return floor("999", 3).encode()
    parser._request_bytes = get
    result = parser._build_result(payload(), "123")
    assert await result.extra["text_card_comment_factory"]() is None
    assert result.text == "正文"
    assert len(calls) == (2 if mode == "page2_foreign" else 1)


def test_origin_media_never_leaks_and_video_without_text(parser):
    data = payload([] , {"video_url": "https://tb-video.bdstatic.com/a.mp4"})
    data["thread"]["origin_thread_info"]["media"] = [{"big_pic": "https://imgsrc.baidu.com/not-ours.jpg"}]
    result = parser._build_result(data, "123")
    assert result.has_video and len(result.contents) == 1
    assert "转发来源" not in (result.text or "")


def test_untrusted_malformed_media_and_duration_are_safe(parser):
    data = payload([None, {"type": 0, "text": "有效正文"}],
        {"video_url": "https://tb-video.bdstatic.com/movie.mp4", "video_duration": "unknown"})
    result = parser._build_result(data, "123")
    assert result.text == "有效正文" and result.has_video and result.contents[0].duration == 0


def test_emote_is_not_a_fullsize_body_image(parser):
    result = parser._build_result(payload([{"type": "0", "text": "正文"},
        {"type": "2", "text": "image_emoticon", "c": "微笑", "src": "https://imgsrc.baidu.com/emote.jpg"}]), "123")
    assert not result.contents and "image_emoticon" not in result.text
    assert "微笑" in result.text


@pytest.mark.parametrize("fragment,video", [
    ({"type": 3, "origin_src": "https://imgsrc.baidu.com:invalid/a"}, None),
    ({"type": "5", "link": "https://unverified.example/movie.mp4"}, None),
    ({"type": 0, "text": "正文"}, {"video_url": "https://unverified.example/movie.mp4"}),
])
def test_unsupported_body_media_fails_before_any_download(parser, fragment, video):
    with pytest.raises(ParseException, match="媒体"):
        parser._build_result(payload([{"type": 0, "text": "正文"}, fragment], video), "123")
    assert parser.downloader.calls == []


def test_allowed_media_download_headers_are_anonymous(parser):
    parser.headers["Cookie"] = "must-not-leak"
    parser._build_result(payload([{"type": 3, "origin_src": "https://imgsrc.baidu.com/a.jpg"}]), "123")
    assert all("Cookie" not in call[2]["ext_headers"] for call in parser.downloader.calls)


@pytest.mark.parametrize("url", ["http://imgsrc.baidu.com/a.jpg", "https://imgsrc.baidu.com:80/a.jpg",
    "https://imgsrc.baidu.com.evil/a.jpg", "https://user@imgsrc.baidu.com/a.jpg",
    "https://tb-video.bdstatic.com.evil/a.mp4", "https://127.0.0.1/a.jpg"])
def test_media_requires_tls_and_exact_allowlisted_host(parser, url):
    assert parser._media_url(url) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("tid,page", [("123", 2), ("123", 0), ("123", 999), ("x", 1)])
async def test_first_floor_fetch_never_pages_or_accepts_invalid_id(parser, tid, page):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid request reached transport")
    parser._request_json = unexpected
    with pytest.raises(ParseException):
        await parser._fetch_page(tid, page)


@pytest.mark.asyncio
async def test_stream_limit_timeout_redirect_and_cookie_jar(parser, monkeypatch):
    from astrbot_plugin_parser_x.core.parsers import tieba
    observed = {}
    class Stream:
        async def iter_chunked(self, size):
            for _ in range(33): yield b"x" * 65536
    class Response:
        status, content_length, content = 200, None, Stream()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kw): observed.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, method, url, **kw): observed.update(kw); return Response()
    monkeypatch.setattr(tieba.aiohttp, "ClientSession", Session)
    with pytest.raises(ParseException, match="大小"):
        await parser._request_json("POST", "https://tieba.baidu.com/c/f/pb/page")
    assert observed["timeout"].total == 15
    assert isinstance(observed["cookie_jar"], tieba.aiohttp.DummyCookieJar)
    assert observed["allow_redirects"] is False and observed["trust_env"] is False
    assert observed.get("ssl", True) is not False
    Response.status = 301
    with pytest.raises(ParseException, match="301"):
        await parser._request_json("POST", "https://tieba.baidu.com/c/f/pb/page")
