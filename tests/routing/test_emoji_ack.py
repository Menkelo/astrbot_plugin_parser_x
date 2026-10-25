"""表情回应只能出现在"解析真的出结果"之后。"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_parser_x.core.exception import SkipParseException


def _plugin(monkeypatch, parse_behaviour):
    from astrbot_plugin_parser_x import main as plugin_main

    class FakeEvent:
        def __init__(self):
            self._chain = []
            self.message_str = "https://live.bilibili.com/123"
            self.unified_msg_origin = "Menkelo_Bot:GroupMessage:1"
            self.message_obj = type("Obj", (), {"message_id": 1})()
            self.bot = object()

        def get_messages(self):
            return self._chain

        def get_self_id(self):
            return "1"

        def plain_result(self, text):
            return text

        async def send(self, _result):
            return None

    monkeypatch.setattr(plugin_main, "AiocqhttpMessageEvent", FakeEvent, raising=False)
    plugin = object.__new__(plugin_main.ParserXPlugin)
    plugin.config = {}
    plugin._debug_mode_enabled = lambda: False
    plugin._disabled_sessions = lambda: set()
    plugin._live_card_reason = lambda chain: ""
    plugin.context = type(
        "Ctx", (), {"get_config": staticmethod(lambda: {"command_prefixes": ["/"]})}
    )()

    notified: list[int] = []

    class Arbiter:
        async def notify(self, _bot, message_id):
            notified.append(message_id)

    plugin.arbiter = Arbiter()
    plugin._collect_parser_matches = lambda text: [("p", "k", None)]

    async def fake_parse(parser, keyword, searched, text):
        if isinstance(parse_behaviour, Exception):
            raise parse_behaviour
        return parse_behaviour

    plugin._parse_with_cache = fake_parse
    sent: list[object] = []

    async def fake_send(_event, result):
        sent.append(result)

    plugin._send_parse_result = fake_send
    return plugin, FakeEvent, notified, sent


@pytest.mark.asyncio
async def test_live_link_never_gets_reaction(monkeypatch):
    plugin, FakeEvent, notified, sent = _plugin(
        monkeypatch, SkipParseException("直播链接")
    )
    await plugin.on_message(FakeEvent())
    assert notified == []
    assert sent == []


@pytest.mark.asyncio
async def test_parse_error_never_gets_reaction(monkeypatch):
    from astrbot_plugin_parser_x.core.exception import ParseException

    plugin, FakeEvent, notified, _sent = _plugin(monkeypatch, ParseException("boom"))
    await plugin.on_message(FakeEvent())
    assert notified == []


@pytest.mark.asyncio
async def test_successful_parse_gets_one_reaction(monkeypatch):
    plugin, FakeEvent, notified, sent = _plugin(monkeypatch, object())
    await plugin.on_message(FakeEvent())
    await asyncio.sleep(0)  # 表情是 create_task 异步贴的
    assert notified == [1]
    assert len(sent) == 1
