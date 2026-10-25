"""Offline REST V2 contracts; never use the deployed plugin import path."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.comment_settings import CommentSettings


def make_feed(pages, *, count=10, enabled=True, cookie=""):
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        value = pages[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(json=lambda: value, raise_for_status=lambda: None)

    parser = SimpleNamespace(
        client=SimpleNamespace(post=post), headers={"Cookie": "unrelated-secret"},
        config={}, kuaishou_cookie=cookie,
        comment_settings=CommentSettings.from_config(
            {"comments": {"enabled": enabled, "display_count": count}}, "kuaishou"),
    )
    module = importlib.import_module("astrbot_plugin_parser_x.core.parsers.kuaishou_comment")
    return module.KuaishouCommentFeed(parser), calls


def raw(identity=1, text="有意义的评论", **fields):
    return {"comment_id": identity, "author_id": "owner", "author_name": "读者",
            "content": text, **fields}


@pytest.mark.asyncio
async def test_structured_photo_id_precedes_url_and_comments_are_lazy(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo, CdnUrl
    from astrbot_plugin_parser_x.core.data import VideoContent
    import msgspec
    parser = KuaiShouParser({"cache_dir": str(tmp_path), "cookies": {"kuaishou_cookie": "raw=value"}}, None)
    photo = msgspec.json.decode(b'{"caption":"body","timestamp":0,"id":"structured","userId":"owner"}', type=Photo)
    calls = []
    async def fetch(photo_id, *, owner_id=""):
        calls.append((photo_id, owner_id))
    assert hasattr(parser, "comment_feed"), "Kuaishou has no comment integration"
    parser.comment_feed.fetch_document = fetch
    parser.create_video_content = lambda *args, **kwargs: VideoContent(None, None)
    photo.main_mv_urls = [CdnUrl(cdn='test', url='https://cdn.example/video.mp4')]
    result = parser._build_result_from_photo(photo, "https://www.kuaishou.com/short-video/urlid")
    assert calls == []
    await result.extra["text_card_comment_factory"]()
    assert calls == [("structured", "owner")]
    assert result.extra["comment_timeout"] == parser.comment_settings.timeout
    assert parser.kuaishou_cookie == "raw=value"
    assert "Cookie" not in parser.ios_headers


@pytest.mark.parametrize("url, expected", [
    ("https://www.kuaishou.com/short-video/3xabc?photoId=query", "3xabc"),
    ("https://v.m.chenzhongtech.com/fw/photo/3xabc", "3xabc"),
    ("https://www.kuaishou.com/?photoId=3xquery", "3xquery"),
    ("https://cdn.example.com/short-video/notphoto?photoId=cdn", ""),
    ("https://v.kuaishou.com/sharetoken", ""),
])
def test_resolved_firstparty_url_id_fallback(tmp_path, url, expected):
    from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo, CdnUrl
    from astrbot_plugin_parser_x.core.data import VideoContent
    parser = KuaiShouParser({"cache_dir": str(tmp_path)}, None)
    calls = []
    async def fetch(identity, *, owner_id=""):
        calls.append(identity)
    assert hasattr(parser, "comment_feed"), "Kuaishou has no comment integration"
    parser.comment_feed.fetch_document = fetch
    parser.create_video_content = lambda *args, **kwargs: VideoContent(None, None)
    result = parser._build_result_from_photo(Photo(caption="body", timestamp=0,
        main_mv_urls=[CdnUrl(cdn='test', url='https://cdn.example/video.mp4')]), url)
    factory = result.extra.get("text_card_comment_factory")
    assert callable(factory) is bool(expected)
    if expected:
        asyncio.run(factory())
        assert calls == [expected]


@pytest.mark.asyncio
async def test_regex_fallback_uses_resolved_url_not_cdn_id(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser
    calls = []
    class Downloader:
        async def download_video(self, url, **kwargs):
            calls.append((url, kwargs))
            return tmp_path / "video.mp4"
    parser = KuaiShouParser({"cache_dir": str(tmp_path), "cookies": {"kuaishou_cookie": "secret"}}, Downloader())
    responses = iter([
        SimpleNamespace(status_code=302, headers={"Location": "https://www.kuaishou.com/short-video/canonical"}),
        SimpleNamespace(status_code=200, text='{"srcNoMark":"https://cdn.example/video/randomid.mp4"}'),
    ])
    async def get(*args, **kwargs):
        return next(responses)
    parser._session = SimpleNamespace(get=get)
    fetched = []
    async def fetch(photo_id, *, owner_id=""):
        fetched.append(photo_id)
    parser.comment_feed.fetch_document = fetch
    key, match = parser.search_url("https://v.kuaishou.com/sharetoken")
    result = await parser.parse(key, match)
    assert fetched == []
    assert callable(result.extra.get("text_card_comment_factory"))
    await result.extra["text_card_comment_factory"]()
    assert fetched == ["canonical"]
    await result.contents[0].get_path()
    assert calls[0][0] == "https://cdn.example/video/randomid.mp4"
    assert all(k.lower() != "cookie" for k in calls[0][1]["ext_headers"])


def test_disabled_parser_does_not_attach_factory(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo, CdnUrl
    from astrbot_plugin_parser_x.core.data import VideoContent
    parser = KuaiShouParser({"cache_dir": str(tmp_path), "comments": {"enabled": False}}, None)
    result = parser._build_result_from_photo(Photo(caption="body", timestamp=0),
                                             "https://www.kuaishou.com/short-video/3xabc")
    assert not callable(result.extra.get("text_card_comment_factory"))


def test_known_timestamp_and_legacy_names_preserve_text_without_inventing_media():
    feed, _ = make_feed([{"result": 1, "rootCommentsV2": [{
        "commentId": "12", "authorId": "owner", "authorName": "旧格式名字",
        "content": "第一行[未知表情]\n第二行", "timestamp": 1700000000000,
        "avatar": "https://example.com/unverified.jpg", "likeCount": 999,
    }]}])
    doc = asyncio.run(feed.fetch_document("photo", owner_id="owner"))
    entry = doc.entries[0]
    assert entry.author.nickname == "旧格式名字" and entry.author.is_up
    assert entry.time_text == feed.time_text(1700000000)
    assert any(part.kind == "line-break" for part in entry.content)
    assert "[未知表情]" in "".join(part.text for part in entry.content)
    assert entry.images == [] and entry.author.avatar == "" and entry.like_text == "0"


def test_v2_root_normalization_and_explicit_cookie_only():
    feed, calls = make_feed([{"result": 1, "commentCountV2": 12,
                              "rootCommentsV2": [raw()], "pcursorV2": "no_more"}],
                            cookie="raw=value")
    doc = asyncio.run(feed.fetch_document("3xphoto", owner_id="owner"))
    assert doc.theme.platform_name == "快手"
    assert doc.total_text == "12 条评论"
    entry = doc.entries[0]
    assert entry.author.nickname == "读者" and entry.author.is_up
    assert entry.author.avatar == "" and entry.like_text == "0" and not entry.pinned
    assert entry.content[0].text == "有意义的评论"
    assert calls[0][0] == "https://www.kuaishou.com/rest/v/photo/comment/list"
    assert calls[0][1]["json"] == {"photoId": "3xphoto", "pcursor": ""}
    assert calls[0][1]["headers"]["Cookie"] == "raw=value"
    assert all(key.lower() != "cookie" for key in feed.comment_filter.headers)
    assert feed.parser.headers == {"Cookie": "unrelated-secret"}


def test_pagination_refills_filtered_slots_deduplicates_and_stops_repeated_cursor():
    feed, calls = make_feed([
        {"result": 1, "rootCommentsV2": [raw(1, "@朋友"), raw(2, "第一条内容")], "pcursorV2": "next"},
        {"result": 1, "rootCommentsV2": [raw(2, "第一条内容"), raw(3, "第二条内容")], "pcursorV2": "next"},
    ], count=3)
    doc = asyncio.run(feed.fetch_document("photo"))
    assert [e.content[0].text for e in doc.entries] == ["第一条内容", "第二条内容"]
    assert [c[1]["json"]["pcursor"] for c in calls] == ["", "next"]


def test_root_pagination_is_bounded():
    feed, calls = make_feed([{"result": 1, "rootCommentsV2": [raw(text="@朋友")],
                              "pcursorV2": str(i)} for i in range(3)])
    assert asyncio.run(feed.fetch_document("photo")) is None
    assert len(calls) == 3


def test_sublist_selects_first_valid_reply_and_sends_integer_root_id():
    feed, calls = make_feed([
        {"result": 1, "rootCommentsV2": [raw("17", hasSubComments=True)]},
        {"result": 1, "subCommentsV2": [raw(18, "@朋友")], "pcursorV2": "next"},
        {"result": 1, "subCommentsV2": [raw(19, "作者的有效回复")], "pcursorV2": "more"},
    ])
    doc = asyncio.run(feed.fetch_document("photo", owner_id="owner"))
    reply = doc.entries[0].first_reply
    assert reply is not None and reply.author.is_up
    assert reply.content[0].text == "作者的有效回复"
    assert calls[1][0].endswith("/comment/sublist")
    assert calls[1][1]["json"] == {"photoId": "photo", "rootCommentId": 17, "pcursor": ""}
    assert calls[2][1]["json"]["pcursor"] == "next"


@pytest.mark.parametrize("page", [None, [], {"result": 50, "rootCommentsV2": [raw()]},
                                  RuntimeError("secret-cookie-body")])
def test_failed_fetch_isolated_without_logging_response_secrets(page, caplog):
    feed, _ = make_feed([page])
    assert asyncio.run(feed.fetch_document("photo")) is None
    assert "secret-cookie-body" not in caplog.text


@pytest.mark.parametrize("field", ["rootCommentsV2", "subCommentsV2"])
def test_malformed_comment_collections_are_isolated(field):
    pages = [{"result": 1, "rootCommentsV2": 7}]
    if field == "subCommentsV2":
        pages = [{"result": 1, "rootCommentsV2": [raw(hasSubComments=True)]},
                 {"result": 1, "subCommentsV2": 7}]
    feed, _ = make_feed(pages)
    doc = asyncio.run(feed.fetch_document("photo"))
    assert doc is None if field == "rootCommentsV2" else doc.entries[0].first_reply is None


def test_disabled_or_missing_photo_id_never_requests():
    feed, calls = make_feed([], enabled=False)
    assert asyncio.run(feed.fetch_document("photo")) is None
    feed, calls = make_feed([])
    assert asyncio.run(feed.fetch_document("")) is None
    assert calls == []


def test_failed_reply_does_not_hide_root():
    feed, _ = make_feed([{"result": 1, "rootCommentsV2": [raw(hasSubComments=True)]},
                         RuntimeError("secret-cookie-body")])
    doc = asyncio.run(feed.fetch_document("photo"))
    assert len(doc.entries) == 1 and doc.entries[0].first_reply is None


def test_sublist_repeated_cursor_is_bounded():
    feed, calls = make_feed([
        {"result": 1, "rootCommentsV2": [raw(hasSubComments=True)]},
        {"result": 1, "subCommentsV2": [raw(2, "@朋友")], "pcursorV2": "next"},
        {"result": 1, "subCommentsV2": [raw(3, "@朋友")], "pcursorV2": "next"},
    ])
    doc = asyncio.run(feed.fetch_document("photo"))
    assert doc.entries[0].first_reply is None
    assert len(calls) == 3
