import asyncio
from pathlib import Path

import pytest
from astrbot.api.message_components import Image, Nodes
from astrbot_plugin_parser_x.core.data import DeliveryBatch, DeliveryPlan
from astrbot_plugin_parser_x.core.parsers import DouyinParser
from astrbot_plugin_parser_x.main import ParserXPlugin
from test_media_completion import Downloads, Event, _FakeCardRenderer, config


@pytest.mark.parametrize('native_plan', [False, True])
@pytest.mark.parametrize('image_count', [1, 19, 20, 21, 35])
def test_album_and_card_remain_one_forward(tmp_path, native_plan, image_count):
    async def run():
        downloads = Downloads(tmp_path)
        parser = DouyinParser(config(tmp_path), downloads)
        result = parser._build_result_from_aweme({
            'aweme_id': '123', 'desc': '王者女英雄建模图片', 'create_time': 1700000000,
            'author': {'nickname': '与世界失联'},
            'images': [{'url_list': [f'https://example.com/{i}.jpg']}
                       for i in range(image_count)],
        }, '123')
        if native_plan:
            result.delivery = DeliveryPlan([DeliveryBatch(list(result.contents), mode='forward')])
        plugin = object.__new__(ParserXPlugin)
        plugin.config = {}
        plugin.cache_dir = tmp_path
        plugin.text_renderer = _FakeCardRenderer()
        event = Event()
        await plugin._send_parse_result(event, result)
        await downloads.finish()
        assert len(event.sent) == 1, '一份图文不应按20节点拆成多条转发'
        assert isinstance(event.sent[0][0], Nodes)
        nodes = event.sent[0][0].nodes
        assert len(nodes) == image_count + 1
        assert all(isinstance(n.content[0], Image) for n in nodes)
        assert [Path(n.content[0].file).name for n in nodes[1:]] == [f'{i}.jpg' for i in range(image_count)]
    asyncio.run(run())
