"""AcFun public metadata corrects the generic yt-dlp card, without cookies."""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from core.download import VideoInfo
from core.parsers.ytdlp import AcFunParser, YtDlpParser

URL = "https://www.acfun.cn/v/ac48917867"
AVATAR = "https://tx-free-imgs.acfun.cn/avatar.jpg"
COVER = "https://imgs.acfun.cn/cover.jpg"


class Downloader:
    def __init__(self, tmp_path, **info):
        self.path = tmp_path / "video.mp4"
        self.path.write_bytes(b"video")
        self.info = VideoInfo(**{
            "title": "Demo", "uploader": "yt-dlp uploader", "description": "Body",
            "duration": 1, **info,
        })
        self.download_calls = []

    async def ytdlp_extract_info(self, url, cookiefile, **kwargs):
        return self.info

    def download_video(self, url, **kwargs):
        self.download_calls.append((url, kwargs))
        async def download():
            return self.path
        return asyncio.create_task(download())


def make_parser(tmp_path, **info):
    return AcFunParser(
        {"cache_dir": str(tmp_path), "comments": {"enabled": True}},
        Downloader(tmp_path, **info),
    )


async def parse(parser):
    key, match = parser.search_url(URL)
    result = await parser.parse(key, match)
    assert await result.contents[0].get_path() == parser.downloader.path
    return result


@pytest.mark.asyncio
async def test_public_metadata_corrects_author_and_body_card(tmp_path, monkeypatch):
    parser = make_parser(tmp_path)
    requests = []
    comments = []
    payload = {
        "user": {"name": "龙馍馍", "headUrl": AVATAR, "id": 123},
        "coverUrl": COVER,
        "description": "nested braces } and escaped quote: \"",
    }

    async def get(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            text="<script>window.videoInfo =\n" + json.dumps(payload, ensure_ascii=False)
            + '; window.other = {"not": "videoInfo"};</script>',
        )

    async def fetch_comments(source_id):
        comments.append(source_id)
        return None

    monkeypatch.setattr(parser, "http_get", get)
    monkeypatch.setattr(parser.comment_feed, "fetch_document", fetch_comments)
    result = await parse(parser)
    assert result.author.name == "龙馍馍"
    assert result.extra["text_card_avatar"] == AVATAR
    assert result.extra["text_card_media"] == COVER
    assert result.extra["text_card_media_fit"] == "cover"
    assert result.extra["adapter"] == "yt-dlp"
    assert result.extra["render_text_card"] is True
    assert result.title == "Demo"
    assert result.text == "Body"
    assert result.contents[0].duration == 1
    assert requests[0][0] == URL
    assert "cookie" not in str(requests[0][1]).lower()
    assert comments == []
    await result.extra["text_card_comment_factory"]()
    assert comments == ["48917867"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "http", "missing", "broken", "null", "user-null"])
async def test_unavailable_public_metadata_falls_back_to_uploader(tmp_path, monkeypatch, failure):
    parser = make_parser(tmp_path)

    async def get(url, **kwargs):
        if failure == "network":
            raise TimeoutError("offline")
        bodies = {
            "http": 'window.videoInfo={"user":{"name":"wrong"}};',
            "missing": "<html>No metadata</html>",
            "broken": "window.videoInfo = {broken};",
            "null": "window.videoInfo = null;",
            "user-null": 'window.videoInfo = {"user":null};',
        }
        return SimpleNamespace(status_code=503 if failure == "http" else 200, text=bodies[failure])

    monkeypatch.setattr(parser, "http_get", get)
    result = await parse(parser)
    assert result.author.name == "yt-dlp uploader"
    assert "text_card_avatar" not in result.extra
    assert "text_card_media" not in result.extra
    assert callable(result.extra["text_card_comment_factory"])


@pytest.mark.asyncio
async def test_invalid_public_user_does_not_hide_valid_cover(tmp_path, monkeypatch):
    parser = make_parser(tmp_path)

    async def get(url, **kwargs):
        return SimpleNamespace(status_code=200, text='window.videoInfo = '
                               + json.dumps({"user": ["invalid"], "coverUrl": COVER}) + ';')

    monkeypatch.setattr(parser, "http_get", get)
    result = await parse(parser)
    assert result.extra["text_card_media"] == COVER
    assert result.author.name == "yt-dlp uploader"


@pytest.mark.asyncio
@pytest.mark.parametrize("public_name", ["Unknown@real nickname", "", None, 42])
async def test_real_unknown_prefix_nickname_is_never_stripped(tmp_path, monkeypatch, public_name):
    parser = make_parser(tmp_path, uploader="Unknown@uploader nickname")

    async def get(url, **kwargs):
        return SimpleNamespace(status_code=200, text="window.videoInfo="
                               + json.dumps({"user": {"name": public_name}}) + ";")

    monkeypatch.setattr(parser, "http_get", get)
    result = await parse(parser)
    assert result.author.name == (
        public_name if isinstance(public_name, str) and public_name
        else "Unknown@uploader nickname"
    )


@pytest.mark.asyncio
async def test_non_acfun_ytdlp_retains_generic_author_without_public_request(tmp_path, monkeypatch):
    parser = YtDlpParser({"cache_dir": str(tmp_path)}, Downloader(tmp_path))
    # Exercise the shared adapter without registering a new platform parser.
    monkeypatch.setattr(YtDlpParser, "platform", AcFunParser.platform, raising=False)

    async def unexpected_request(*args, **kwargs):
        pytest.fail("generic yt-dlp must not request AcFun metadata")

    monkeypatch.setattr(parser, "http_get", unexpected_request)
    result = await parser._parse_ytdlp(re.search(r"https?://\S+", "https://example.com/video"))
    assert result.author.name == "Unknown@yt-dlp uploader"
    assert result.extra == {"adapter": "yt-dlp", "render_text_card": True}
    assert await result.contents[0].get_path() == parser.downloader.path
