"""Offline regressions for the 0.0.2 Douyin SSR/detail fallback boundary."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.parsers.douyin import DouyinParser
from core.parsers.base import ParseException, SkipParseException

VID = "123456789"
AWEME = {"aweme_id": VID, "desc": "fixture", "create_time": 1,
         "author": {"nickname": "author"}, "images": [
             {"url_list": ["https://example.com/post.jpg"]}]}


def page(data):
    return "<script>window._ROUTER_DATA = " + json.dumps(data) + ";</script>"


@pytest.fixture
def parser(tmp_path):
    instance = DouyinParser({"cache_dir": str(tmp_path)}, SimpleNamespace(
        download_img=lambda *args, **kwargs: None))
    instance.cookies = "offline-cookie"
    return instance


@pytest.mark.asyncio
async def test_missing_ssr_script_uses_existing_detail_assembly(parser):
    calls = []

    async def get(url, **kwargs):
        calls.append(url)
        if "/aweme/detail/" in url:
            return SimpleNamespace(status_code=200, text=json.dumps({"aweme_detail": AWEME}))
        return SimpleNamespace(status_code=200, url=url, text="<html>no script</html>")

    parser.http_get = get
    result = await parser.parse_video("https://m.douyin.com/share/video/" + VID, VID)
    assert result.title == "fixture"
    assert result.author.name == "author"
    assert len(result.contents) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_race_runs_detail_once_after_all_ssr_failures(parser):
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text="no script"))
    parser._parse_via_detail_api = AsyncMock(return_value="detail result")
    result = await parser._race_parse_video(["one", "two", "three"], VID)
    assert result == "detail result"
    assert parser._fetch_router_resp.await_count == 3
    parser._parse_via_detail_api.assert_awaited_once_with(VID)


@pytest.mark.asyncio
async def test_assembly_value_error_does_not_retry_detail(parser):
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text=page(AWEME)))
    error = ValueError("media assembly failed")
    parser._build_result_from_aweme = Mock(side_effect=error)
    parser._parse_via_detail_api = AsyncMock()
    with pytest.raises(ValueError) as caught:
        await parser.parse_video("one", VID)
    assert caught.value is error
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_aweme_schema_falls_back_once(parser):
    invalid = {**AWEME, "author": "wrong shape"}
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text=page(invalid)))
    parser._parse_via_detail_api = AsyncMock(return_value="detail result")
    assert await parser.parse_video("one", VID) == "detail result"
    parser._parse_via_detail_api.assert_awaited_once_with(VID)


@pytest.mark.asyncio
async def test_race_data_failure_with_network_failure_falls_back_once(parser):
    async def fetch(url):
        if url == "timeout":
            raise TimeoutError("offline timeout")
        return SimpleNamespace(text="no script")

    parser._fetch_router_resp = fetch
    parser._parse_via_detail_api = AsyncMock(return_value="detail result")
    assert await parser._race_parse_video(["timeout", "empty"], VID) == "detail result"
    parser._parse_via_detail_api.assert_awaited_once_with(VID)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 410])
async def test_detail_terminal_http_status_is_not_retried(parser, status):
    parser.http_get = AsyncMock(return_value=SimpleNamespace(status_code=status))
    with pytest.raises(ParseException):
        await parser._parse_via_detail_api(VID)
    assert parser.http_get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("html", [
    "no script", "<script>window._ROUTER_DATA = {bad};</script>",
    page(None), page([]), page({"loaderData": {}}),
    page({**AWEME, "author": None}), page({**AWEME, "desc": []}),
    page({**AWEME, "images": "not a list"}),
])
@pytest.mark.parametrize("race", [False, True])
async def test_unusable_ssr_falls_back_exactly_once(parser, html, race):
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text=html))
    parser._parse_via_detail_api = AsyncMock(return_value="detail result")
    result = (await parser._race_parse_video(["one", "two", "three"], VID)
              if race else await parser.parse_video("one", VID))
    assert result == "detail result"
    parser._parse_via_detail_api.assert_awaited_once_with(VID)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [False, True])
async def test_normal_ssr_does_not_add_detail_request(parser, race):
    parser.http_get = AsyncMock(return_value=SimpleNamespace(
        status_code=200, url="https://m.douyin.com/share/video/" + VID,
        text=page({"loaderData": {"aweme": AWEME}})))
    parser._parse_via_detail_api = AsyncMock()
    result = (await parser._race_parse_video(["one", "two", "three"], VID)
              if race else await parser.parse_video("one", VID))
    assert result.title == "fixture"
    assert result.author.name == "author"
    assert len(result.contents) == 1
    parser._parse_via_detail_api.assert_not_awaited()
    assert parser.http_get.await_count == (3 if race else 1)


@pytest.mark.asyncio
async def test_race_waits_for_valid_ssr_before_detail(parser):
    invalid_done = asyncio.Event()

    async def fetch(url):
        if url == "empty":
            invalid_done.set()
            return SimpleNamespace(text="no script")
        await invalid_done.wait()
        await asyncio.sleep(0)
        return SimpleNamespace(text=page(AWEME))

    parser._fetch_router_resp = fetch
    parser._parse_via_detail_api = AsyncMock()
    assert (await parser._race_parse_video(["empty", "valid"], VID)).title == "fixture"
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("status", [401, 403, 404, 410])
async def test_terminal_share_status_never_calls_detail(parser, race, status):
    parser.http_get = AsyncMock(return_value=SimpleNamespace(status_code=status))
    parser._parse_via_detail_api = AsyncMock()
    with pytest.raises(ParseException, match=f"Status: {status}"):
        if race:
            await parser._race_parse_video(["one", "two"], VID)
        else:
            await parser.parse_video("one", VID)
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ParseException("作品已删除"),
    ParseException("权限不足"), SkipParseException(), asyncio.CancelledError()])
@pytest.mark.parametrize("race", [False, True])
async def test_control_and_terminal_errors_preserve_identity(parser, error, race):
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text=page(AWEME)))
    parser._process_router_resp = Mock(side_effect=error)
    parser._parse_via_detail_api = AsyncMock()
    with pytest.raises(type(error)) as caught:
        if race:
            await parser._race_parse_video(["one", "two"], VID)
        else:
            await parser.parse_video("one", VID)
    assert caught.value is error
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_race_terminal_error_blocks_data_fallback(parser):
    error = ParseException("作品已删除")

    async def fetch(url):
        if url == "deleted":
            raise error
        return SimpleNamespace(text="no script")

    parser._fetch_router_resp = fetch
    parser._parse_via_detail_api = AsyncMock()
    with pytest.raises(ParseException) as caught:
        await parser._race_parse_video(["empty", "deleted"], VID)
    assert caught.value is error
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_cancellation_drains_all_workers(parser):
    started = set()
    cancelled = set()
    ready = asyncio.Event()

    async def fetch(url):
        started.add(url)
        if len(started) == 3:
            ready.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.add(url)

    parser._fetch_router_resp = fetch
    parser._parse_via_detail_api = AsyncMock()
    task = asyncio.create_task(parser._race_parse_video(["one", "two", "three"], VID))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == started == {"one", "two", "three"}
    parser._parse_via_detail_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ParseException("detail failed"), SkipParseException(),
                                   asyncio.CancelledError()])
async def test_detail_failure_is_not_reentered_by_race(parser, error):
    parser._fetch_router_resp = AsyncMock(return_value=SimpleNamespace(text="no script"))
    parser._parse_via_detail_api = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        await parser._race_parse_video(["one", "two", "three"], VID)
    assert caught.value is error
    parser._parse_via_detail_api.assert_awaited_once_with(VID)


@pytest.mark.asyncio
async def test_detail_images_filter_keeps_existing_aid_workaround(parser):
    parser.http_get = AsyncMock(side_effect=[
        SimpleNamespace(status_code=200, text=json.dumps({
            "filter_detail": {"filter_reason": "images_base"}})),
        SimpleNamespace(status_code=200, text=json.dumps({"aweme_detail": AWEME})),
    ])
    assert (await parser._parse_via_detail_api(VID)).title == "fixture"
    assert [call.kwargs["params"] for call in parser.http_get.await_args_list] == [
        {"aweme_id": VID}, {"aweme_id": VID, "aid": "6383"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status_code": 1, "status_msg": "作品已删除"},
    {"filter_detail": {"filter_reason": "permission_denied"}},
])
async def test_detail_terminal_payload_does_not_retry(parser, payload):
    parser.http_get = AsyncMock(return_value=SimpleNamespace(
        status_code=200, text=json.dumps(payload)))
    with pytest.raises(ParseException):
        await parser._parse_via_detail_api(VID)
    assert parser.http_get.await_count == 1


@pytest.mark.asyncio
async def test_missing_cookie_fails_without_detail_http_request(parser):
    parser.cookies = ""
    parser.http_get = AsyncMock(return_value=SimpleNamespace(
        status_code=200, url="one", text="no script"))
    with pytest.raises(ParseException, match="douyin_ck"):
        await parser.parse_video("one", VID)
    assert parser.http_get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", [False, True])
async def test_race_success_or_skip_drains_losing_worker(parser, skip):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch(url):
        if url == "slow":
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        await started.wait()
        if skip:
            raise SkipParseException()
        return SimpleNamespace(text=page(AWEME))

    parser._fetch_router_resp = fetch
    parser._parse_via_detail_api = AsyncMock()
    if skip:
        with pytest.raises(SkipParseException):
            await parser._race_parse_video(["fast", "slow"], VID)
    else:
        assert (await parser._race_parse_video(["fast", "slow"], VID)).title == "fixture"
    assert cancelled.is_set()
    parser._parse_via_detail_api.assert_not_awaited()


def test_import_uses_isolated_package():
    import core.parsers.douyin as module
    assert Path(module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
