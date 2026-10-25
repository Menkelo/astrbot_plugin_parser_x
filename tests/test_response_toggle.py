from types import SimpleNamespace

from core.config_policy import migrate_config
from astrbot_plugin_parser_x.main import ParserXPlugin


def test_response_enabled_defaults_true_and_preserves_explicit_value():
    config = {}
    assert migrate_config(config) is True
    assert config["behavior"]["response_enabled"] is True
    config = {"behavior": {"response_enabled": False}}
    migrate_config(config)
    assert config["behavior"]["response_enabled"] is False


def test_response_enabled_gate_defaults_on_and_can_disable():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {}}
    assert plugin._response_enabled() is True
    plugin.config["behavior"]["response_enabled"] = False
    assert plugin._response_enabled() is False


def test_response_enabled_is_checked_before_message_work():
    plugin = object.__new__(ParserXPlugin)
    plugin.config = {"behavior": {"response_enabled": False}}
    event = SimpleNamespace(unified_msg_origin="Bot:GroupMessage:123")
    assert plugin._should_respond(event) is False
