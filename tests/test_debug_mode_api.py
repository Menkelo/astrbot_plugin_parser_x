import asyncio
import json
from unittest.mock import patch

import pytest
from astrbot_plugin_parser_x.core import debug_api as module
from astrbot_plugin_parser_x.main import ParserXPlugin


@pytest.mark.parametrize('owner,value,code', [(None, True, 403), ('admin', 'yes', 400), ('admin', True, 200), ('admin', False, 200)])
def test_debug_mode_requires_owner_and_boolean_and_persists(owner, value, code):
    class Config(dict):
        saves = 0
        def save_config(self):
            self.saves += 1
    class Request:
        @staticmethod
        async def json(default=None):
            return {'enabled': value}
    plugin = object.__new__(ParserXPlugin)
    plugin.config = Config(debug={'enabled': False})
    plugin._debug_page_owner = lambda: owner
    with patch.object(module, 'request', Request()):
        response = asyncio.run(plugin.debug_page_mode())
    assert response.status_code == code
    assert plugin.config.saves == (1 if code == 200 else 0)
    if code == 200:
        assert plugin._debug_mode_enabled() is value
        assert json.loads(response.body)['debug_only'] is value


def test_debug_mode_failed_save_restores_previous_state():
    class Config(dict):
        def save_config(self):
            raise OSError('disk unavailable')
    class Request:
        @staticmethod
        async def json(default=None):
            return {'enabled': True}
    plugin = object.__new__(ParserXPlugin)
    plugin.config = Config(debug={'enabled': False})
    plugin._debug_page_owner = lambda: 'admin'
    with patch.object(module, 'request', Request()), pytest.raises(OSError):
        asyncio.run(plugin.debug_page_mode())
    assert plugin._debug_mode_enabled() is False
