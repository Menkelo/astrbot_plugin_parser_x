from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot_plugin_parser_x import main as main_module
from astrbot_plugin_parser_x.main import PLUGIN_NAME, ParserXPlugin

from core.data import ParseResult, Platform
from core.debug_page import (
    DebugCaptureEvent,
    DebugMediaRegistry,
    DebugMessageSerializer,
    DebugSessionManager,
)


def _response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_debug_switch_defaults_to_disabled_and_page_assets_exist():
    root = Path(__file__).parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    debug_html = (root / "pages" / "debug" / "index.html").read_text(
        encoding="utf-8"
    )
    debug_app = (root / "pages" / "debug" / "app.js").read_text(encoding="utf-8")
    debug_css = (root / "pages" / "debug" / "style.css").read_text(
        encoding="utf-8"
    )

    assert schema["debug"]["items"]["enabled"]["default"] is False
    assert (root / "pages" / "debug" / "index.html").is_file()
    assert (root / "pages" / "debug" / "app.js").is_file()
    assert (root / "pages" / "debug" / "style.css").is_file()
    assert 'class="qq-window"' in debug_html
    assert 'id="share-text"' in debug_html
    assert "composer-panel" not in debug_html
    assert "parse-details" not in debug_html
    assert 'entry.className = "message-entry is-self"' in debug_app
    assert "elements.clearButton.disabled = busy" in debug_app
    assert "[hidden] { display: none !important; }" in debug_css


def test_debug_mode_blocks_adapter_message_before_it_is_inspected():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"debug": {"enabled": True}}

    class FakeAiocqhttpEvent:
        @property
        def unified_msg_origin(self):
            raise AssertionError("adapter event must not be inspected in debug mode")

    async def run():
        with patch.object(main_module, "AiocqhttpMessageEvent", FakeAiocqhttpEvent):
            await plugin.on_message(FakeAiocqhttpEvent())

    asyncio.run(run())


def test_debug_page_status_is_available_but_start_is_rejected_when_disabled():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"debug": {"enabled": False}, "comments": {}}
    plugin.parser_map = {}
    plugin.debug_sessions = DebugSessionManager()

    class FakeRequest:
        username = "admin"
        plugin_name = PLUGIN_NAME

        @staticmethod
        async def json(default=None):
            return {"text": "https://b23.tv/example"}

    async def run():
        with patch.object(main_module, "request", FakeRequest()):
            status = await plugin.debug_page_status()
            start = await plugin.debug_page_start()
        await plugin.debug_sessions.close()
        return status, start

    status, start = asyncio.run(run())

    assert status.status_code == 200
    assert _response_json(status)["enabled"] is False
    assert start.status_code == 403
    assert "调试模式尚未开启" in _response_json(start)["message"]


def test_debug_page_rejects_a_request_without_dashboard_page_owner():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"debug": {"enabled": True}, "comments": {}}
    plugin.parser_map = {}
    plugin.debug_sessions = DebugSessionManager()

    request_without_owner = SimpleNamespace(
        username=None,
        plugin_name=PLUGIN_NAME,
    )

    async def run():
        with patch.object(main_module, "request", request_without_owner):
            response = await plugin.debug_page_status()
        await plugin.debug_sessions.close()
        return response

    response = asyncio.run(run())
    assert response.status_code == 403


def test_parser_match_collection_deduplicates_overlaps_and_repeated_urls():
    plugin = object.__new__(ParserXPlugin)
    parser = SimpleNamespace(
        platform=SimpleNamespace(name="kuaishou", display_name="快手")
    )
    pattern = re.compile(r"https://v\.kuaishou\.com/[A-Za-z0-9]+")
    plugin.parser_map = {
        "v.kuaishou": parser,
        "kuaishou": parser,
    }
    plugin.key_pattern_list = [
        ("v.kuaishou", pattern),
        ("kuaishou", pattern),
    ]

    matches = plugin._collect_parser_matches(
        "https://v.kuaishou.com/abc] https://v.kuaishou.com/abc "
        "https://v.kuaishou.com/xyz"
    )

    assert [match.group(0) for _, _, match in matches] == [
        "https://v.kuaishou.com/abc",
        "https://v.kuaishou.com/xyz",
    ]
    assert all(item[0] is parser for item in matches)


def test_debug_media_tokens_are_scoped_to_dashboard_owner(tmp_path):
    media_path = tmp_path / "preview.png"
    media_path.write_bytes(b"not-a-real-png")
    registry = DebugMediaRegistry()

    alice = registry.describe(media_path, owner="alice")
    bob = registry.describe(media_path, owner="bob")

    assert alice["token"] != bob["token"]
    assert registry.get(alice["token"], owner="alice") is not None
    assert registry.get(alice["token"], owner="bob") is None
    assert registry.get(bob["token"], owner="alice") is None


def test_forward_preview_keeps_one_component_per_forward_node(tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    serializer = DebugMessageSerializer(DebugMediaRegistry(), owner="admin")
    forward = Nodes(
        [
            Node(uin="1", name="Parser X", content=[Plain("正文")]),
            Node(uin="1", name="Parser X", content=[Image(str(image_path))]),
        ]
    )

    serialized = asyncio.run(serializer.serialize_component(forward))

    assert serialized["type"] == "forward"
    assert len(serialized["nodes"]) == 2
    assert [len(node["content"]) for node in serialized["nodes"]] == [1, 1]
    assert serialized["nodes"][0]["content"][0]["type"] == "text"
    assert serialized["nodes"][1]["content"][0]["type"] == "image"


def test_capture_event_uses_a_session_wide_message_index():
    emitted = []

    async def run():
        async def emit(payload):
            emitted.append(payload)

        capture = DebugCaptureEvent(
            emit=emit,
            serializer=DebugMessageSerializer(DebugMediaRegistry(), owner="admin"),
            started_at=asyncio.get_running_loop().time(),
            platform="B站",
            initial_message_index=3,
        )
        await capture.send([Plain("first")])
        await capture.send([Plain("second")])
        return capture.message_count

    message_count = asyncio.run(run())

    assert [item["message"]["index"] for item in emitted] == [4, 5]
    assert message_count == 5


def test_debug_runner_uses_real_parser_and_delivery_pipeline():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"show_download_fail_tip": True}}
    plugin.debug_media = DebugMediaRegistry()

    class FakeParser:
        platform = Platform(name="demo", display_name="演示平台")
        source_text = None

        async def parse(self, keyword, searched):
            assert keyword == "example.com"
            assert searched.group(0) == "https://example.com/post/1"
            return ParseResult(
                platform=self.platform,
                text="真实投递正文",
                url=searched.group(0),
            )

    parser = FakeParser()
    plugin.parser_map = {"example.com": parser}
    plugin.key_pattern_list = [
        ("example.com", re.compile(r"https://example\.com/post/\d+"))
    ]
    emitted = []

    async def run():
        async def emit(payload):
            emitted.append(payload)

        await plugin._run_debug_page_parse(
            "请解析 https://example.com/post/1",
            owner="admin",
            emit=emit,
        )

    asyncio.run(run())

    assert parser.source_text == "请解析 https://example.com/post/1"
    assert [item["event"] for item in emitted] == [
        "started",
        "match",
        "parsed",
        "message",
        "delivered",
        "done",
    ]
    assert emitted[3]["message"]["components"] == [
        {"type": "text", "text": "真实投递正文"}
    ]


def test_debug_session_stream_completes_and_removes_session():
    async def run():
        manager = DebugSessionManager()

        async def runner(emit):
            await emit({"event": "working", "value": 1})

        session = await manager.create(owner="admin", runner=runner)
        chunks = [chunk async for chunk in manager.stream(session)]
        remaining = manager.get(session.session_id, owner="admin")
        await manager.close()
        return chunks, remaining

    chunks, remaining = asyncio.run(run())
    payloads = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks]

    assert [payload["event"] for payload in payloads] == ["working", "session_end"]
    assert remaining is None


def test_debug_session_can_be_cancelled_before_runner_starts():
    async def run():
        manager = DebugSessionManager()
        gate = asyncio.Event()

        async def runner(_emit):
            await gate.wait()

        session = await manager.create(owner="admin", runner=runner)
        wrong_owner = await manager.cancel(session.session_id, owner="other")
        cancelled = await manager.cancel(session.session_id, owner="admin")
        chunks = [chunk async for chunk in manager.stream(session)]
        await manager.close()
        return wrong_owner, cancelled, chunks

    wrong_owner, cancelled, chunks = asyncio.run(run())
    payloads = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks]

    assert wrong_owner is False
    assert cancelled is True
    assert [payload["event"] for payload in payloads] == [
        "cancelled",
        "session_end",
    ]
