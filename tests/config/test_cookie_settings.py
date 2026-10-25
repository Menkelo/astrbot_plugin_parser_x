"""Cookie settings contracts; synthetic credentials and no live requests."""
import asyncio
from http.cookiejar import MozillaCookieJar
from types import SimpleNamespace

import pytest

from core.config_policy import migrate_config
from core.download import VideoInfo
from core.parsers.ytdlp import AcFunParser, YtDlpParser
from core.result_cache import _config_signature


class Downloader:
    def __init__(self, tmp_path):
        self.path = tmp_path / "video.mp4"
        self.path.write_bytes(b"video")
        self.extracted_cookiefile = None
        self.downloaded_cookiefile = None

    async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
        self.extracted_cookiefile = cookiefile
        return VideoInfo(title="demo", uploader="author", duration=1)

    def download_video(self, url, **kwargs):
        self.downloaded_cookiefile = kwargs.get("cookiefile")

        async def download():
            return self.path

        return asyncio.create_task(download())


@pytest.mark.asyncio
async def test_acfun_raw_cookie_reaches_metadata_and_both_ytdlp_stages(tmp_path):
    downloader = Downloader(tmp_path)
    parser = AcFunParser({"cache_dir": str(tmp_path), "cookies": {
        "acfun_cookie": "  auth_key=synthetic=value; uid=42\r\n",
        "bili_ck": "unrelated=never-forward",
    }}, downloader)
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, text="")

    parser.http_get = get
    keyword, match = parser.search_url("https://www.acfun.cn/v/ac42")
    result = await parser.parse(keyword, match)
    assert await result.contents[0].get_path() == downloader.path
    assert calls[0][1].get("headers", {}).get("Cookie") == "auth_key=synthetic=value; uid=42"
    path = downloader.extracted_cookiefile
    assert path is not None
    assert downloader.downloaded_cookiefile == path
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert {(cookie.domain, cookie.name, cookie.value) for cookie in jar} == {
        (".acfun.cn", "auth_key", "synthetic=value"), (".acfun.cn", "uid", "42"),
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(key.lower() == "cookie" for key in parser.headers)


@pytest.mark.asyncio
async def test_acfun_cookie_reaches_comment_api_but_not_image_headers(tmp_path):
    parser = AcFunParser({"cache_dir": str(tmp_path), "cookies": {
        "acfun_cookie": "auth_key=synthetic", "bili_ck": "unrelated=secret",
    }}, Downloader(tmp_path))
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: {"result": 0, "rootComments": []})

    parser.http_get = get
    await parser.comment_feed.fetch_document("42")
    assert calls[0][1]["headers"].get("Cookie") == "auth_key=synthetic"
    assert not any(key.lower() == "cookie" for key in parser.comment_feed.comment_filter.headers)
    assert not any(key.lower() == "cookie" for key in parser.headers)


def test_migration_removes_only_retired_cookie_file_setting():
    cookies = {"acfun_cookie": "auth_key=synthetic", "bili_ck": "keep",
               "custom_cookie": "keep", "ytdlp_cookie_file": ["obsolete.txt"]}
    config = {"cookies": cookies}
    migrate_config(config)
    assert config["cookies"] is cookies
    assert cookies == {"acfun_cookie": "auth_key=synthetic", "bili_ck": "keep",
                       "custom_cookie": "keep"}
    assert migrate_config(config) is False


def test_retired_file_setting_is_not_read_by_generic_ytdlp(tmp_path):
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("legacy contents")
    parser = YtDlpParser({"cache_dir": str(tmp_path), "cookies": {
        "ytdlp_cookie_file": [str(obsolete)], "acfun_cookie": "auth_key=synthetic",
    }}, Downloader(tmp_path))
    assert parser._cookie_file() is None


def test_result_cache_does_not_fingerprint_retired_cookie_files(tmp_path):
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("legacy")
    config = {"cookies": {"ytdlp_cookie_file": [str(obsolete)],
                          "acfun_cookie": "auth_key=synthetic"}}
    before = _config_signature(config)
    obsolete.write_text("changed legacy file size")
    assert _config_signature(config) == before
    config["cookies"]["acfun_cookie"] = "auth_key=changed"
    assert _config_signature(config) != before


def test_acfun_cookie_file_is_recreated_after_cache_cleanup(tmp_path):
    parser = AcFunParser({"cache_dir": str(tmp_path), "cookies": {
        "acfun_cookie": "auth_key=synthetic",
    }}, Downloader(tmp_path))
    first = parser._cookie_file()
    first.unlink()
    replacement = parser._cookie_file()
    assert replacement.is_file()
    assert replacement.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("cookies", [{}, {"acfun_cookie": ""}, {"acfun_cookie": None},
                                     {"acfun_cookie": []}, None])
def test_acfun_missing_or_non_string_cookie_remains_anonymous(tmp_path, cookies):
    parser = AcFunParser({"cache_dir": str(tmp_path), "cookies": cookies}, Downloader(tmp_path))
    assert parser._cookie_file() is None
    assert not any(key.lower() == "cookie" for key in parser.acfun_headers())
    assert not any(key.lower() == "cookie" for key in parser.comment_feed._headers())
    assert not list(tmp_path.glob("acfun_cookies_*.txt"))


@pytest.mark.asyncio
async def test_acfun_cookie_reaches_subcomment_and_emote_api(tmp_path):
    parser = AcFunParser({"cache_dir": str(tmp_path), "cookies": {
        "acfun_cookie": "auth_key=synthetic",
    }}, Downloader(tmp_path))
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: {"result": 0, "subComments": []})

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: {"result": 0, "emotionPackageList": []})

    parser.http_get = get
    parser._session = SimpleNamespace(post=post)
    entry = parser.comment_feed._entry({"content": "root comment"})
    await parser.comment_feed._populate_reply("42", "1", entry,
                                              {"subCommentCount": 1}, {})
    await parser.comment_feed._resolve_emotes([
        parser.comment_feed._entry({"content": "hello [emot=acfun,1/]"}),
    ])
    assert {url.rsplit("/", 1)[-1] for url, _ in calls} == {"sublist", "getUserEmotion"}
    assert all(kwargs["headers"]["Cookie"] == "auth_key=synthetic" for _, kwargs in calls)
