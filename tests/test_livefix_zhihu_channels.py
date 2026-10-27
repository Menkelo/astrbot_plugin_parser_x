"""Offline live-fix regressions: synthetic credentials, isolated HTTP sessions."""
import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.parsers.zhihu import ZhihuParser
from astrbot_plugin_parser_x.core.parsers.wechat_channels import WechatChannelsParser
from astrbot_plugin_parser_x.core.exception import ParseException


def make_parser(cls, tmp_path, cookie=""):
    key = "zhihu_cookie" if cls is ZhihuParser else "wechat_channels_cookie"
    return cls({"cache_dir": str(tmp_path), "cookies": {key: cookie}}, object())


def test_zhihu_answer_uses_www_api_and_exact_cookie_scope(tmp_path):
    parser = make_parser(ZhihuParser, tmp_path, "auth=synthetic")
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {
            "type": "answer", "id": 22, "question": {"id": 11, "title": "question"},
            "author": {"name": "author"}, "content": "<p>full body</p>"})

    parser.http_get = get
    async def assemble(*args, **kwargs):
        assert kwargs['answer_id'] == '22'
        return args
    parser._assemble = assemble
    result = asyncio.run(parser.parse_answer({"question": "11", "answer": "22"}))
    assert result[0] == "<p>full body</p>"
    assert calls[0][0] == "https://www.zhihu.com/api/v4/answers/22"
    assert calls[0][1]["headers"]["Cookie"] == "auth=synthetic"
    assert calls[0][1]["allow_redirects"] is False
    assert parser._zhihu_headers("https://zhuanlan.zhihu.com/p/22")["Cookie"] == "auth=synthetic"
    for url in ["https://api.zhihu.com/v4/answers/22", "https://www.zhihu.com.evil/api/v4/answers/22",
                "http://www.zhihu.com/api/v4/answers/22", "https://www.zhihu.com/api/v4/answers/22/evil",
                "https://u@www.zhihu.com/api/v4/answers/22", "https://www.zhihu.com:444/api/v4/answers/22"]:
        assert "Cookie" not in parser._zhihu_headers(url)


def test_zhihu_headers_do_not_override_browser_user_agent(tmp_path):
    """A short UA overrides impersonate=chrome120 and Zhihu answers with HTTP 403."""
    parser = make_parser(ZhihuParser, tmp_path, "auth=synthetic")
    for url in ["https://www.zhihu.com/api/v4/answers/22", "https://zhuanlan.zhihu.com/p/22"]:
        headers = parser._zhihu_headers(url)
        user_agent = headers.get("User-Agent")
        if user_agent is None:
            continue
        assert "Chrome/" in user_agent, user_agent
        assert "Mozilla/5.0 (" in user_agent, user_agent
        assert headers["Cookie"] == "auth=synthetic"


@pytest.mark.parametrize("cookie", ["", "auth=synthetic"])
@pytest.mark.parametrize("non_json", [False, True])
def test_zhihu_errors_do_not_mislabel_cookie_requests(tmp_path, cookie, non_json):
    parser = make_parser(ZhihuParser, tmp_path, cookie)

    def bad_json():
        raise ValueError("synthetic")

    async def get(*args, **kwargs):
        return SimpleNamespace(status_code=200 if non_json else 403, json=bad_json)

    parser.http_get = get
    with pytest.raises(ParseException) as caught:
        asyncio.run(parser.parse_answer({"question": "11", "answer": "22"}))
    assert "匿名" not in str(caught.value)
    assert "未尝试签名或登录" not in str(caught.value)
    assert "auth=synthetic" not in str(caught.value)


@pytest.mark.parametrize("raw", [" a=1;\r\n b=two=three\r\n", "a=1;\nb=two=three", "a=1;\rb=two=three"])
def test_channels_normalizes_pasted_cookie_lines(tmp_path, raw):
    parser = make_parser(WechatChannelsParser, tmp_path, raw)
    assert parser.yuanbao_cookie == "a=1; b=two=three"
    assert "Cookie" not in parser.headers


@pytest.mark.parametrize("raw", ["a=1;\r\nX-Evil: injected", "a=1\r\nCookie: evil=1", "a=1\x00", "a=1\t", "bad name=1", 42])
def test_channels_rejects_header_injection_cookie(tmp_path, raw):
    assert make_parser(WechatChannelsParser, tmp_path, raw).yuanbao_cookie == ""


def feed_result(err_msg):
    return {"errCode": 0, "data": {"errMsg": err_msg,
        "feedInfo": {"title": "feed", "videoUrl": "https://example.com/video.mp4"},
        "authorInfo": {"nickname": "author"}}}


def test_channels_feed_info_accepts_explicit_zero_error_type(tmp_path):
    parser = make_parser(WechatChannelsParser, tmp_path)
    result = feed_result({"type": 0})
    async def post(*args, **kwargs):
        return SimpleNamespace(json=lambda: result)
    parser.http_post = post
    assert asyncio.run(parser._feed_info({}, referer="", page_url="")) is result


@pytest.mark.parametrize("bad", [{"type": 1}, {"type": "0"}, {"type": False}, {}, [], False, 0, "denied", {"unknown": 0}, {"type": 0, "unexpected": 1}])
def test_channels_feed_info_rejects_unknown_error_shapes(tmp_path, bad):
    parser = make_parser(WechatChannelsParser, tmp_path)
    async def post(*args, **kwargs):
        return SimpleNamespace(json=lambda: feed_result(bad))
    parser.http_post = post
    with pytest.raises(ParseException):
        asyncio.run(parser._feed_info({}, referer="", page_url=""))


@pytest.mark.parametrize("err_msg", [{"type": 0}, None, ""])
def test_channels_build_result_accepts_confirmed_success(tmp_path, err_msg):
    parser = make_parser(WechatChannelsParser, tmp_path)
    parser.create_video_content = lambda *args, **kwargs: "video"
    parser.create_author = lambda *args, **kwargs: "author"
    parser.result = lambda **kwargs: kwargs
    result = parser._build_result(feed_result(err_msg), "share1", "https://weixin.qq.com/sph/share1")
    assert result["contents"] == ["video"]


@pytest.mark.parametrize("bad", [{"type": 1}, {"type": "0"}, {"type": False}, {}, [], False, 0, "denied", {"unknown": 0}, {"type": 0, "unexpected": 1}])
def test_channels_build_result_rejects_unknown_error_shapes(tmp_path, bad):
    parser = make_parser(WechatChannelsParser, tmp_path)
    parser.create_video_content = lambda *args, **kwargs: "video"
    parser.create_author = lambda *args, **kwargs: "author"
    parser.result = lambda **kwargs: kwargs
    with pytest.raises(ParseException):
        parser._build_result(feed_result(bad), "share1", "https://weixin.qq.com/sph/share1")


@pytest.mark.parametrize("entry", ["feed", "build"])
@pytest.mark.parametrize("code", [None, False, "0", 1])
def test_channels_rejects_unknown_top_level_error_codes(tmp_path, entry, code):
    parser = make_parser(WechatChannelsParser, tmp_path)
    result = feed_result({"type": 0})
    result["errCode"] = code
    parser.create_video_content = lambda *args, **kwargs: "video"
    parser.create_author = lambda *args, **kwargs: "author"
    parser.result = lambda **kwargs: kwargs
    async def post(*args, **kwargs):
        return SimpleNamespace(json=lambda: result)
    parser.http_post = post
    with pytest.raises(ParseException):
        if entry == "feed":
            asyncio.run(parser._feed_info({}, referer="", page_url=""))
        else:
            parser._build_result(result, "share1", "https://weixin.qq.com/sph/share1")


def fake_session(monkeypatch, status=200):
    calls = []
    class Session:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=status)
        async def close(self):
            calls.append(("close", {}))
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", Session)
    return calls


def test_channels_transport_enforces_cookie_origin_and_session_isolation(tmp_path, monkeypatch):
    parser = make_parser(WechatChannelsParser, tmp_path, "auth=synthetic")
    calls = fake_session(monkeypatch)
    for url in [parser.YUANBAO_API, parser.FEED_INFO_API]:
        asyncio.run(parser.http_post(url, headers={"cOoKiE": "caller=evil", "Accept": "application/json"},
            cookies={"other": "evil"}, allow_redirects=True, verify=False))
    initializations = [opts for key, opts in calls if key == "init"]
    assert len(initializations) == 2
    assert all(opts["verify"] is True and opts["discard_cookies"] is True for opts in initializations)
    requests = [(key, opts) for key, opts in calls if key.startswith("https:")]
    for url, opts in requests:
        cookies = {key.lower(): value for key, value in opts["headers"].items() if key.lower() == "cookie"}
        assert cookies == ({"cookie": "auth=synthetic"} if url == parser.YUANBAO_API else {})
        assert "cookies" not in opts
        assert opts["allow_redirects"] is False
    assert len([key for key, _ in calls if key == "close"]) == 2


@pytest.mark.parametrize("url", [
    "http://yuanbao.tencent.com/api/weixin/get_parse_result",
    "https://yuanbao.tencent.com.evil/api/weixin/get_parse_result",
    "https://yuanbao.tencent.com/api/weixin/get_parse_result/evil",
    "https://u@yuanbao.tencent.com/api/weixin/get_parse_result",
    "https://yuanbao.tencent.com:444/api/weixin/get_parse_result",
    "https://channels.weixin.qq.com/other",
    "https://channels.weixin.qq.com.evil/finder-preview/api/feed/get_feed_info",
])
def test_channels_transport_rejects_untrusted_origins_before_session(tmp_path, monkeypatch, url):
    parser = make_parser(WechatChannelsParser, tmp_path, "auth=synthetic")
    calls = fake_session(monkeypatch)
    with pytest.raises(ParseException):
        asyncio.run(parser.http_post(url))
    assert calls == []


def test_zhihu_transport_isolated_verified_and_no_redirects(tmp_path, monkeypatch):
    parser = make_parser(ZhihuParser, tmp_path, "auth=synthetic")
    calls = []
    class Session:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=302)
        async def close(self):
            calls.append(("close", {}))
    monkeypatch.setattr("astrbot_plugin_parser_x.core.parsers.zhihu.AsyncSession", Session)
    for _ in range(2):
        response = asyncio.run(parser.http_get("https://www.zhihu.com/api/v4/answers/22",
            headers={"Cookie": "caller=evil"}, allow_redirects=True))
        assert response.status_code == 302
    assert len([key for key, _ in calls if key == "init"]) == 2
    assert len([key for key, _ in calls if key == "close"]) == 2
    for key, opts in calls:
        if key == "init":
            assert opts["verify"] is True and opts["discard_cookies"] is True
            assert opts["allow_redirects"] is False
        elif key.startswith("https:"):
            assert opts["verify"] is True and opts["allow_redirects"] is False
            assert opts["headers"]["Cookie"] == "auth=synthetic"
    with pytest.raises(ParseException):
        asyncio.run(parser.http_get("https://api.zhihu.com/v4/answers/22"))
    assert len(calls) == 6


def test_channels_feed_accepts_http_201(tmp_path, monkeypatch):
    parser = make_parser(WechatChannelsParser, tmp_path)
    fake_session(monkeypatch, status=201)
    response = asyncio.run(parser.http_post(parser.FEED_INFO_API, json={}))
    assert response.status_code == 201


@pytest.mark.parametrize("status", [200, 201, 302, 400, 500])
def test_channels_yuanbao_still_requires_http_200(tmp_path, monkeypatch, status):
    parser = make_parser(WechatChannelsParser, tmp_path)
    fake_session(monkeypatch, status=status)
    if status == 200:
        assert asyncio.run(parser.http_post(parser.YUANBAO_API, json={})).status_code == 200
    else:
        with pytest.raises(ParseException):
            asyncio.run(parser.http_post(parser.YUANBAO_API, json={}))
