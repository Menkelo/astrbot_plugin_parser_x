"""Independent NetEase contracts: synthetic fixtures, never user credentials."""
import asyncio
import importlib
import importlib.util
from pathlib import Path

import pytest

MODULE = "astrbot_plugin_parser_x.core.parsers.netease"



@pytest.mark.parametrize("route", ["song?id=42", "#/song?id=42", "m/song?id=42", "song?foo=x&id=42"])
def test_supported_single_song_routes(tmp_path, route):
    instance = parser(tmp_path, [detail(), {"code": 200, "data": []}])
    assert parse(instance, "https://music.163.com/" + route).title == "公开歌曲"


@pytest.mark.parametrize("route", ["playlist?id=42", "mv?id=42", "song?id=1&id=42", "song?id=-42", "song?id=42#evil", "song?id=42.evil"])
def test_reject_invalid_song_routes(tmp_path, route):
    from astrbot_plugin_parser_x.core.exception import ParseException
    instance = parser(tmp_path, [detail()])
    with pytest.raises(ParseException):
        parse(instance, "https://music.163.com/" + route)


def test_reject_wrong_song_detail_identity(tmp_path):
    from astrbot_plugin_parser_x.core.exception import ParseException
    instance = parser(tmp_path, [detail(99)])
    with pytest.raises(ParseException):
        parse(instance)


def test_untrusted_cover_is_never_sent_to_card_renderer(tmp_path):
    data = detail()
    data["songs"][0]["album"]["picUrl"] = "http://127.0.0.1/private"
    instance = parser(tmp_path, [data, {"code": 200, "data": []}])
    assert "text_card_media" not in parse(instance).extra



def parser(tmp_path, responses):
    assert importlib.util.find_spec(MODULE) is not None, "NetEase parser missing"
    cls = importlib.import_module(MODULE).NeteaseParser
    class Downloader:
        def __init__(self):
            self.urls = []
        def download_audio(self, url, **kwargs):
            self.urls.append(url)
            return Path("/tmp/netease-test.mp3")
    instance = cls({"cache_dir": str(tmp_path)}, Downloader())
    instance.calls = []
    async def request(path, params=None):
        instance.calls.append((path, params))
        value = responses[0] if len(responses) == 1 else responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    instance._request_json = request
    return instance


def detail(song_id=42):
    return {"code": 200, "songs": [{"id": song_id, "name": "公开歌曲",
        "artists": [{"name": "歌手甲"}, {"name": "歌手乙"}],
        "album": {"name": "专辑", "picUrl": "https://p1.music.126.net/cover.jpg"},
        "duration": 180000}]}


def parse(instance, url="https://music.163.com/song?id=42"):
    keyword, match = instance.search_url(url)
    return asyncio.run(instance.parse(keyword, match))


def test_details_survive_unavailable_audio(tmp_path):
    instance = parser(tmp_path, [detail(), {"code": 200, "data": [{"id": 42, "url": None, "code": 403}]}])
    result = parse(instance)
    assert result.title == "公开歌曲"
    assert result.author.name == "歌手甲 / 歌手乙"
    assert result.url == "https://music.163.com/song?id=42"
    assert result.extra["text_card_media"] == "https://p1.music.126.net/cover.jpg"
    assert result.extra["audio_status"] == "unavailable"
    assert not result.contents


# Public response sample: https://github.com/Binaryify/NeteaseCloudMusicApi/issues/1750
# freeTrialInfo {start:0,end:30} accompanies audio.time=30040 milliseconds.
@pytest.mark.parametrize("trial", [None, {"start": 0, "end": 30}, {"start": 45, "end": 75}])
def test_official_audio_uses_shared_downloader_and_marks_trial(tmp_path, trial):
    from astrbot_plugin_parser_x.core.data import AudioContent
    audio = {"id": 42, "code": 200, "url": "https://m801.music.126.net/song.mp3", "freeTrialInfo": trial}
    instance = parser(tmp_path, [detail(), {"code": 200, "data": [audio]}])
    result = parse(instance)
    assert isinstance(result.contents[0], AudioContent)
    assert result.contents[0].duration == (30 if trial else 180)
    assert instance.downloader.urls == [audio["url"]]
    assert result.extra["audio_status"] == ("preview" if trial else "available")
    assert result.extra["delivery_text_card_consume_non_video"] is False
    if trial:
        assert "试听" in result.extra["info"]


@pytest.mark.parametrize("fault", ["ok", "oversize", "redirect", "status", "identity", "shape"])
def test_anonymous_transport_is_bounded_and_checks_response(tmp_path, monkeypatch, fault):
    import json
    from types import SimpleNamespace
    from astrbot_plugin_parser_x.core.exception import ParseException
    instance = parser(tmp_path, [detail()])
    del instance._request_json
    captured = {}
    body = b"x" * (1024 * 1024 + 1) if fault == "oversize" else json.dumps([] if fault == "shape" else {"code": 200}).encode()
    class Content:
        async def iter_chunked(self, size):
            yield body
    class Response:
        status = 302 if fault == "redirect" else 403 if fault == "status" else 200
        url = "https://evil.test/api/song/detail/" if fault == "identity" else "https://music.163.com/api/song/detail/"
        headers = {}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            captured.update(kwargs)
            captured["url"] = url
            return Response()
    monkeypatch.setattr("aiohttp.ClientSession", Session)
    instance.headers["Cookie"] = "must-not-leak"
    if fault == "ok":
        assert asyncio.run(instance._request_json("/api/song/detail/", {"ids": "[42]"})) == {"code": 200}
        assert captured["allow_redirects"] is False
        assert captured["timeout"].total <= 15
        assert all(key.lower() not in {"cookie", "authorization"} for key in captured["headers"])
    else:
        with pytest.raises(ParseException):
            asyncio.run(instance._request_json("/api/song/detail/", {"ids": "[42]"}))


@pytest.mark.parametrize("audio", [TimeoutError(), {"code": 200, "data": [{"id": 999, "code": 200, "url": "https://m801.music.126.net/wrong.mp3"}]}, {"code": 200, "data": [{"id": 42, "code": 200, "url": "https://127.0.0.1/private"}]}])
def test_audio_failure_or_wrong_identity_preserves_details(tmp_path, audio):
    instance = parser(tmp_path, [detail(), audio])
    result = parse(instance)
    assert result.title == "公开歌曲"
    assert not result.contents
    assert not instance.downloader.urls


@pytest.mark.parametrize("failure", [False, True])
def test_comments_use_lazy_unified_document_and_failure_is_isolated(tmp_path, failure):
    from astrbot_plugin_parser_x.core.comment_canvas import CommentDocument
    instance = parser(tmp_path, [detail(), {"code": 200, "data": []},
        TimeoutError() if failure else {"code": 200, "total": 5, "hotComments": [
            {"commentId": 1, "content": "很喜欢这首歌的旋律", "likedCount": 12,
             "time": 1700000000000, "user": {"nickname": "听众", "avatarUrl": "https://p1.music.126.net/avatar.jpg"}},
            {"commentId": 2, "content": "很喜欢这首歌的旋律", "user": {"nickname": "重复"}},
        ]}])
    result = parse(instance)
    assert len(instance.calls) == 2
    document = asyncio.run(result.extra["text_card_comment_factory"]())
    assert len(instance.calls) == 3
    assert instance.calls[-1][0].endswith("R_SO_4_42")
    if failure:
        assert document is None
    else:
        assert isinstance(document, CommentDocument)
        assert len(document.entries) == 1
        assert document.entries[0].author.nickname == "听众"
        assert document.entries[0].like_text == "12"
        assert document.work_title == "公开歌曲"


def test_disabled_comments_do_not_attach_factory(tmp_path):
    instance = parser(tmp_path, [detail(), {"code": 200, "data": []}])
    instance.config["comments"] = {"enabled": False}
    result = parse(instance)
    assert "text_card_comment_factory" not in result.extra
