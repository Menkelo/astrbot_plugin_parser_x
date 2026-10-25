"""非常规消息必须零请求、零回复地被忽略。"""

from __future__ import annotations

import pytest

from astrbot_plugin_parser_x.core.card_guard import (
    component_name,
    ignored_component_reason,
    is_card_segment,
)


class Plain:
    pass


class Image:
    pass


class At:
    pass


class Json:
    def __init__(self, data=None):
        self.data = data or {}


class Xml:
    def __init__(self, data=None):
        self.data = data or {}


class Forward:
    pass


class Poke:
    pass


class Dice:
    pass


class RPS:
    pass


class Shake:
    pass


class Music:
    pass


class Share:
    pass


class Location:
    pass


class Unknown:
    pass


@pytest.mark.parametrize(
    ("segment", "label"),
    [
        (Forward(), "合并转发"),
        (Poke(), "戳一戳"),
        (Dice(), "骰子"),
        (RPS(), "猜拳"),
        (Shake(), "窗口抖动"),
        (Music(), "音乐卡片"),
        (Share(), "链接卡片"),
        (Location(), "位置卡片"),
        (Unknown(), "未知组件"),
    ],
)
def test_known_non_standard_components_are_dropped(segment, label):
    assert ignored_component_reason([Plain(), segment]) == label


@pytest.mark.parametrize(
    "chain",
    [
        [],
        [Plain()],
        [Image()],
        [At()],
        [Plain(), Image()],
        [Json({"app": "com.tencent.miniapp_01"})],
        [Xml({"app": "com.tencent.miniapp_01"})],
    ],
)
def test_regular_messages_and_cards_are_not_dropped_here(chain):
    assert ignored_component_reason(chain) == ""


def test_component_name_falls_back_to_type_enum():
    class Stub:
        class type:  # noqa: N801 - mimics ComponentType enum
            name = "Poke"

    assert component_name(Stub()) == "Stub"
    assert component_name(type("Json", (), {})()) == "Json"


def test_is_card_segment_only_for_json_and_xml():
    assert is_card_segment(Json()) is True
    assert is_card_segment(Xml()) is True
    assert is_card_segment(Plain()) is False
    assert is_card_segment(Image()) is False


def _make_plugin(monkeypatch):
    from astrbot_plugin_parser_x import main as plugin_main

    class FakeEvent:
        def __init__(self, chain, text=""):
            self._chain = chain
            self.message_str = text
            self.unified_msg_origin = "Menkelo_Bot:GroupMessage:1"
            self.message_obj = type("Obj", (), {"message_id": 1})()

        def get_messages(self):
            return self._chain

        def get_self_id(self):
            return "1"

    monkeypatch.setattr(plugin_main, "AiocqhttpMessageEvent", FakeEvent, raising=False)
    plugin = object.__new__(plugin_main.ParserXPlugin)
    plugin.config = {}
    plugin._debug_mode_enabled = lambda: False
    plugin._disabled_sessions = lambda: set()
    plugin._live_card_reason = lambda chain: ""
    plugin.context = type(
        "Ctx", (), {"get_config": staticmethod(lambda: {"command_prefixes": ["/"]})}
    )()
    calls: list[str] = []

    def fake_collect(text):
        calls.append(text)
        return []

    plugin._collect_parser_matches = fake_collect
    return plugin, FakeEvent, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segment",
    [Forward(), Poke(), Dice(), RPS(), Shake(), Music(), Share(), Location(), Unknown()],
)
async def test_on_message_ignores_non_standard_components(monkeypatch, segment):
    plugin, FakeEvent, calls = _make_plugin(monkeypatch)
    await plugin.on_message(FakeEvent([segment], text="https://www.bilibili.com/video/BV1x"))
    assert calls == []


@pytest.mark.asyncio
async def test_on_message_ignores_card_without_usable_url(monkeypatch):
    plugin, FakeEvent, calls = _make_plugin(monkeypatch)
    await plugin.on_message(FakeEvent([Json({"app": "com.tencent.miniapp_01"})]))
    assert calls == []


@pytest.mark.asyncio
async def test_on_message_still_parses_supported_share_card(monkeypatch):
    plugin, FakeEvent, calls = _make_plugin(monkeypatch)
    card = Json(
        {
            "app": "com.tencent.miniapp_01",
            "meta": {
                "detail_1": {"qqdocurl": "https://www.bilibili.com/video/BV1QHnq6LEqv"}
            },
        }
    )
    await plugin.on_message(FakeEvent([card]))
    assert calls and "BV1QHnq6LEqv" in calls[0]


@pytest.mark.asyncio
async def test_on_message_parses_plain_text_as_before(monkeypatch):
    plugin, FakeEvent, calls = _make_plugin(monkeypatch)
    await plugin.on_message(FakeEvent([Plain()], text="https://www.bilibili.com/video/BV1x"))
    assert calls == ["https://www.bilibili.com/video/BV1x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["903365914", "1"])
async def test_on_message_collects_links_after_leading_mention(monkeypatch, target):
    from astrbot_plugin_parser_x import main as plugin_main

    plugin, FakeEvent, calls = _make_plugin(monkeypatch)
    url = "https://www.bilibili.com/video/BV1mfay6pErh"
    await plugin.on_message(FakeEvent([plugin_main.At(qq=target), Plain()], text=url))
    assert calls == [url]

