"""Offline 0.0.2 XHS security and payload regression tests."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.parsers.xiaohongshu import (
    XiaoHongShuParser,
    ParseException,
)


@pytest.fixture
def parser(tmp_path):
    return XiaoHongShuParser({"cache_dir": str(tmp_path)}, None)


def test_share_tokens_are_bound_to_note_id(parser):
    parser.source_text = (
        "https://www.xiaohongshu.com/explore/first?xsec_token=first-token "
        "https://www.xiaohongshu.com/discovery/item/second"
        "?xsec_token=second%2Btoken%3D&xsec_source=app_share"
    )
    url = parser._ensure_note_url("https://www.xiaohongshu.com/explore/second", "second")
    assert parser._query_value(url, "xsec_token") == "second+token="
    assert parser._query_value(url, "xsec_source") == "app_share"
    assert parser._query_value(
        parser._ensure_note_url("https://www.xiaohongshu.com/explore/missing", "missing"),
        "xsec_token",
    ) == ""


@pytest.mark.parametrize("discovery", [False, True])
def test_comment_token_uses_payload_note_id(parser, discovery):
    parser.source_text = "https://www.xiaohongshu.com/explore/other?xsec_token=wrong"
    calls = []
    parser._comment_extra = lambda note_id, token, **kw: calls.append((note_id, token)) or {}
    note = {"noteId": "target", "type": "normal",
            "user": {"nickName" if discovery else "nickname": "author"}}
    final = "https://www.xiaohongshu.com/explore/other?xsec_token=wrong"
    if discovery:
        parser._process_discovery_data(note, {}, final)
    else:
        parser._process_explore_data(note, final)
    assert calls == [("target", "")]


def test_redirect_to_other_note_does_not_borrow_target_token(parser):
    parser.source_text = "https://www.xiaohongshu.com/explore/target?xsec_token=secret"
    other = "https://www.xiaohongshu.com/explore/other"
    assert parser._ensure_note_url(other, "target") == other


@pytest.mark.parametrize("state", [
    {"note": {"noteDetailMap": {"other": {"note": {"type": "normal"}}}}},
    {"note": {"noteDetailMap": {"target": {"note": {"noteId": "other", "type": "normal"}}}}},
    {"noteData": {"data": {"noteData": {"noteId": "other", "type": "normal"}}}},
    {"note": {"currentNote": {"id": "other", "type": "normal"}}},
    {"note": {"noteDetailMap": {"target": {
        "noteId": "other", "note": {"type": "normal"}}}}},
])
def test_state_rejects_explicit_other_note_id(parser, state):
    assert parser._extract_note_from_state(state, "target") == {}


def test_only_single_unambiguous_idless_h5_is_allowed(parser):
    anonymous = {"type": "normal", "title": "H5"}
    assert parser._extract_note_from_state(
        {"noteData": {"data": {"noteData": anonymous}}}, "target"
    ) == anonymous
    ambiguous = {"note": {"firstNote": anonymous, "currentNote": {
        "type": "normal", "title": "another H5"}}}
    assert parser._extract_note_from_state(ambiguous, "target") == {}
    conflicting = {"note": {"firstNote": anonymous, "currentNote": {
        "type": "normal", "noteId": "other"}}}
    assert parser._extract_note_from_state(conflicting, "target") == {}


@pytest.mark.parametrize("mobile", [False, True])
def test_trusted_document_uses_cookie_without_mutating_media_headers(tmp_path, mobile):
    parser = XiaoHongShuParser({"cache_dir": str(tmp_path), "cookies": {
        "xiaohongshu_cookie": "a1=fixture; web_session=fake"}}, None)
    headers = parser.ios_headers if mobile else parser.headers
    before = dict(headers)
    calls = []

    async def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, url=url, text="<html>note</html>")

    parser.http_get = fake_get
    asyncio.run(parser._request_html(
        "https://www.xiaohongshu.com/discovery/item/target", headers=headers))
    assert calls[0][1]["headers"].get("Cookie") == "a1=fixture; web_session=fake"
    assert headers == before
    assert not any(key.lower() == "cookie" for key in parser.headers)


def test_document_relative_redirect_is_followed_manually(parser):
    calls = []

    async def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(status_code=302, url=url, text="", headers={
                "Location": "/discovery/item/target"})
        return SimpleNamespace(status_code=200, url=url, text="note")

    parser.http_get = fake_get
    final, html = asyncio.run(parser._request_html(
        "https://www.xiaohongshu.com/explore/target", headers=parser.headers))
    assert final == "https://www.xiaohongshu.com/discovery/item/target"
    assert html == "note"
    assert all(not kwargs["allow_redirects"] for _, kwargs in calls)


@pytest.mark.parametrize("destination", [
    "https://evil.test/note", "https://www.xiaohongshu.com.evil.test/note",
    "https://sns-img.xiaohongshu.com/note", "https://xhscdn.com/note",
    "http://www.xiaohongshu.com/explore/target",
    "https://user@www.xiaohongshu.com/explore/target",
    "https://www.xiaohongshu.com:444/explore/target",
])
def test_untrusted_document_redirect_stops_before_second_request(parser, destination):
    parser.comment_cookie = "a1=fake"
    calls = []

    async def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=302, url=url, text="", headers={
            "Location": destination})

    parser.http_get = fake_get
    with pytest.raises(ParseException, match="不可信"):
        asyncio.run(parser._request_html(
            "https://www.xiaohongshu.com/explore/target", headers=parser.headers))
    assert len(calls) == 1
    with pytest.raises(ParseException, match="不可信"):
        asyncio.run(parser._request_html(destination, headers={"cookie": "secret"}))
    assert len(calls) == 1


def test_undefined_only_replaced_outside_json_strings(parser):
    original = {"title": "undefined", "desc": 'quote: "undefined" \\ undefined 中文',
                "url": "https://cdn.test/undefined.jpg", "undefined": "xundefinedy"}
    encoded = json.dumps(original, ensure_ascii=False)
    state = encoded[:-1] + ', "missing":undefined,"array":[undefined,null]}'
    result = parser._extract_initial_state_json(
        f"<script>window.__INITIAL_STATE__={state}</script>")
    assert result == {**original, "missing": None, "array": [None, None]}


@pytest.mark.parametrize("state", [
    '{"x":undefinedSuffix}', '{"x":myundefined}', '{"x":undefined$}',
    '{"x":function(){return undefined}}', '{"x":(()=>1)()}',
    '{"x":undefined};alert(1)', '[]', 'null',
])
def test_invalid_state_is_not_evaluated_or_permissively_decoded(parser, state):
    with pytest.raises(ParseException):
        parser._extract_initial_state_json(
            f"<script>window.__INITIAL_STATE__={state}</script>")


def test_matching_state_wins_over_other_notes_and_vue_wrappers(parser):
    target = {"noteId": "target", "type": "normal", "title": "correct"}
    state = {"value": {"note": {"noteDetailMap": {"value": {
        "other": {"note": {"type": "normal", "title": "wrong"}},
        "target": {"note": {"value": target}},
    }}}}}
    assert parser._extract_note_from_state(state, "target") == target


def test_document_redirect_loop_is_bounded(parser):
    calls = []

    async def fake_get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=302, url=url, text="", headers={
            "Location": "/explore/target"})

    parser.http_get = fake_get
    with pytest.raises(ParseException, match="次数过多"):
        asyncio.run(parser._request_html(
            "https://www.xiaohongshu.com/explore/target", headers=parser.headers))
    assert len(calls) == 6
