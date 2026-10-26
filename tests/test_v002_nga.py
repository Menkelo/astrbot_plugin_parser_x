"""Independent anonymous NGA contract tests; synthetic payloads unless stated."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.parsers.nga import NgaParser
from core.parsers.base import ParseException

TID = "47697417"


def payload():
    return {"code": 0, "tsubject": "公开主题", "tauthor": "楼主",
            "attachPrefix": "https://img.nga.cn/attachments/", "vrows": 3,
            "result": [{"tid": int(TID), "pid": 0, "lou": 0,
                        "content": "前[b]文[/b]<br/>[img]./mon_202610/a.jpg[/img]后文",
                        "postdatetimestamp": 1791618404,
                        "author": {"username": "楼主"}}]}


@pytest.fixture
def parser(tmp_path):
    return NgaParser({"cache_dir": str(tmp_path)}, SimpleNamespace(
        download_img=lambda url, **kw: Path(tmp_path / "image.jpg")))


@pytest.mark.asyncio
async def test_public_thread_builds_ordered_card_using_existing_downloader(parser):
    parser._fetch_thread = AsyncMock(return_value=payload())
    result = await parser.parse_thread("https://bbs.nga.cn/read.php?tid=" + TID)
    assert result.title == "公开主题"
    assert result.author.name == "楼主"
    assert result.text == "前文\n后文"
    assert len(result.contents) == 1
    assert not result.has_video
    assert result.extra["nga_body_flow"] == [
        ("text", "前文\n"), ("image", "https://img.nga.cn/attachments/mon_202610/a.jpg"),
        ("text", "后文")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "oversize", "redirect", "invalid_json"])
async def test_fetch_uses_bounded_anonymous_post_without_session_cookies(parser, mode):
    import json
    calls = []

    class Response:
        status = 302 if mode == "redirect" else 200
        headers = {}
        content = SimpleNamespace()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    async def chunks(size):
        if mode == "oversize":
            yield b"x" * (parser.MAX_RESPONSE_BYTES + 1)
        elif mode == "invalid_json":
            yield b"<html>guest blocked</html>"
        else:
            yield json.dumps(payload()).encode()
    Response.content.iter_chunked = chunks

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 12
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    from unittest.mock import patch
    with patch("core.parsers.nga.aiohttp.ClientSession", Session):
        if mode == "ok":
            assert (await parser._fetch_thread(TID))["code"] == 0
        else:
            with pytest.raises(ParseException):
                await parser._fetch_thread(TID)
    url, kwargs = calls[0]
    assert url == "https://ngabbs.com/app_api.php?__lib=post&__act=list"
    assert kwargs["data"] == {"tid": TID, "page": "1"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Cookie"].startswith("guestJs=")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_comment_factory_prioritizes_hot_posts_dedupes_and_limits(parser):
    data = payload()
    def reply(pid, text):
        return {"tid": int(TID), "pid": pid, "lou": pid, "content": text,
                "author": {"username": "回帖者"}, "vote_good": 9}
    data["result"] += [reply(1, "普通回帖内容"), reply(2, "热门回帖内容"),
                       reply(3, "第二条普通回帖")]
    data["hot_post"] = [reply(2, "热门回帖内容")]
    parser.config["comments"] = {"display_count": 2}
    parser._fetch_thread = AsyncMock(return_value=data)
    result = await parser.parse_thread("https://ngabbs.com/read.php?tid=" + TID)
    document = await result.extra["text_card_comment_factory"]()
    assert document.theme.platform_name == "NGA"
    assert len(document.entries) == 2
    assert document.entries[0].content[0].text == "热门回帖内容"
    assert document.entries[1].content[0].text == "普通回帖内容"
    assert document.entries[0].like_text == "9"
    parser._fetch_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_comment_failure_returns_none_without_losing_body(parser):
    parser._fetch_thread = AsyncMock(return_value=payload())
    result = await parser.parse_thread("https://nga.178.com/read.php?tid=" + TID)
    from unittest.mock import patch
    with patch("core.parsers.nga_comment.NgaCommentFeed.build_document", AsyncMock(side_effect=ValueError("bad comment"))):
        assert await result.extra["text_card_comment_factory"]() is None
    assert result.text == "前文\n后文"


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://img.nga.cn:bad/x", "https://img.nga.cn:444/x", "https://img.nga.cn.evil/x", "https://user@img.nga.cn/x"])
def test_rejects_untrusted_and_malformed_image_urls(url):
    from core.parsers.nga import image_url
    assert image_url(url, "https://img.nga.cn/attachments/") == ""


@pytest.mark.parametrize("host", ["bbs.nga.cn", "nga.178.com", "ngabbs.com"])
@pytest.mark.asyncio
async def test_registered_hosts_dispatch_public_thread(parser, host):
    parser.config["comments"] = {"enabled": False}
    parser._fetch_thread = AsyncMock(return_value=payload())
    keyword, match = parser.search_url("https://" + host + "/read.php?tid=" + TID)
    result = await parser.parse(keyword, match)
    assert result.url == "https://bbs.nga.cn/read.php?tid=" + TID
    assert "text_card_comment_factory" not in result.extra


@pytest.mark.parametrize("change", [{"tid": 42}, {"pid": 42}, {"lou": 1}])
@pytest.mark.asyncio
async def test_mismatched_first_post_is_rejected(parser, change):
    data = payload()
    data["result"][0].update(change)
    parser._fetch_thread = AsyncMock(return_value=data)
    with pytest.raises(ParseException, match="首帖身份"):
        await parser.parse_thread("https://bbs.nga.cn/read.php?tid=" + TID)


@pytest.mark.parametrize("url", ["https://bbs.nga.cn:bad/read.php?tid=42", "https://evil/read.php?tid=42", "https://bbs.nga.cn.evil/read.php?tid=42", "https://bbs.nga.cn/read.php?tid=42&tid=43", "https://user@bbs.nga.cn/read.php?tid=42", "https://bbs.nga.cn/thread.php?tid=42"])
@pytest.mark.asyncio
async def test_untrusted_thread_urls_make_no_request(parser, url):
    parser._fetch_thread = AsyncMock()
    with pytest.raises(ParseException):
        await parser.parse_thread(url)
    parser._fetch_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_removes_duplicate_comments(parser):
    data = payload()
    data["result"] += [{"tid": int(TID), "pid": pid, "lou": pid,
                         "content": "重复但有实际信息的内容", "author": {}}
                        for pid in [1, 2]]
    parser._fetch_thread = AsyncMock(return_value=data)
    result = await parser.parse_thread("https://bbs.nga.cn/read.php?tid=" + TID)
    document = await result.extra["text_card_comment_factory"]()
    assert len(document.entries) == 1


@pytest.mark.asyncio
async def test_nested_inline_comments_do_not_replace_root_replies(parser):
    data = payload()
    data["result"] += [
        {"tid": int(TID), "pid": 1, "lou": 1, "comment_to_id": "123",
         "content": "不是根回复的内联评论", "author": {}},
        {"tid": int(TID), "pid": 2, "lou": 2, "comment_to_id": "",
         "content": "公开根回复的实际内容", "author": {}}]
    parser._fetch_thread = AsyncMock(return_value=data)
    result = await parser.parse_thread("https://bbs.nga.cn/read.php?tid=" + TID)
    document = await result.extra["text_card_comment_factory"]()
    assert len(document.entries) == 1
    assert document.entries[0].content[0].text == "公开根回复的实际内容"


@pytest.mark.asyncio
async def test_real_card_service_receives_nga_comments(parser, tmp_path):
    from core.card_service import CardServiceMixin
    from core.comment_canvas import CommentDocument

    data = payload()
    data["result"].append({"tid": int(TID), "pid": 1, "lou": 1,
                           "content": "进入统一渲染器的根回复", "author": {}})
    parser._fetch_thread = AsyncMock(return_value=data)
    result = await parser.parse_thread("https://bbs.nga.cn/read.php?tid=" + TID)
    service = CardServiceMixin()
    service.config = {}
    service.cache_dir = tmp_path
    renders = []

    async def render(**kwargs):
        renders.append(kwargs)
        kwargs["out_path"].write_bytes(b"stub-rendered-card")

    service.text_renderer = SimpleNamespace(render_card=render)
    card = await service._build_text_card_content(result)
    assert card is not None
    assert (await card.get_path()).is_file()
    document = renders[0]["comment_document"]
    assert isinstance(document, CommentDocument)
    assert document.entries[0].content[0].text == "进入统一渲染器的根回复"
    assert renders[0]["text"] == "前文\n后文"
    assert renders[0]["content_blocks"] == result.extra["text_card_flow"]
    await service._build_text_card_content(result)
    assert len(renders) == 1
    parser._fetch_thread.assert_awaited_once()


def test_import_is_from_isolated_package():
    import core.parsers.nga as module
    root = Path(__file__).resolve().parent
    if root.name == "tests":
        root = root.parent
    assert Path(module.__file__).resolve().is_relative_to(root)
