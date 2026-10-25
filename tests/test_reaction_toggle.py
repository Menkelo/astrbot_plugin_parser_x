import asyncio
import pytest
from test_emoji_ack import _plugin

@pytest.mark.asyncio
async def test_disabled_reaction_keeps_parse_and_delivery(monkeypatch):
    plugin, Event, notified, sent = _plugin(monkeypatch, object())
    plugin.config = {'behavior': {'reaction_enabled': False, 'response_enabled': False}}
    await plugin.on_message(Event())
    await asyncio.sleep(0)
    assert len(sent) == 1
    assert notified == []

@pytest.mark.asyncio
async def test_default_reaction_still_enabled(monkeypatch):
    plugin, Event, notified, sent = _plugin(monkeypatch, object())
    await plugin.on_message(Event())
    await asyncio.sleep(0)
    assert len(sent) == 1
    assert notified == [1]

def test_migrate_discards_wrong_gate_and_preserves_reaction_preference():
    from core.config_policy import migrate_config
    config={'behavior': {'response_enabled': False, 'reaction_enabled': False}}
    migrate_config(config)
    assert 'response_enabled' not in config['behavior']
    assert config['behavior']['reaction_enabled'] is False
    config={}
    migrate_config(config)
    assert config['behavior']['reaction_enabled'] is True
