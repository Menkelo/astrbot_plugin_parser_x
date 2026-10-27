"""XHS: a stale configured Cookie must never break page parsing.

Live evidence: with the configured cookie the note page redirects to
/website-login/error ("分享链接失效"), while the same URL parsed anonymously
returns a normal note. Page requests therefore stay anonymous; the cookie is
only used by the comment API which needs it for signing.
"""
import asyncio
import json

import pytest

from astrbot_plugin_parser_x.core.parsers.xiaohongshu import XiaoHongShuParser


class FakeDownloader:
    def download_img(self, _url, **_kwargs):
        async def done():
            return None

        return asyncio.ensure_future(done())

    def download_video(self, _url, **_kwargs):
        async def done():
            return None

        return asyncio.ensure_future(done())


def make_parser(tmp_path, cookie="a1=test-a1; web_session=test-session"):
    return XiaoHongShuParser(
        {"cache_dir": str(tmp_path), "cookies": {"xiaohongshu_cookie": cookie}},
        FakeDownloader(),
    )


def test_note_page_requests_do_not_send_configured_cookie(tmp_path):
    parser = make_parser(tmp_path)
    seen = []

    async def fake_get(url, *, headers=None, **kwargs):
        seen.append(dict(headers or {}))
        return type("Resp", (), {
            "status_code": 200,
            "text": "<html></html>",
            "url": url,
            "headers": {},
        })()

    parser.http_get = fake_get
    asyncio.run(parser._request_html(
        "https://www.xiaohongshu.com/explore/note123?xsec_token=t",
        headers=parser.headers,
    ))
    assert seen, "page request was not issued"
    assert all(
        "cookie" not in {key.lower() for key in headers} for headers in seen
    ), seen


def test_comment_api_still_receives_cookie(tmp_path):
    parser = make_parser(tmp_path)
    assert parser.comment_cookie == "a1=test-a1; web_session=test-session"
