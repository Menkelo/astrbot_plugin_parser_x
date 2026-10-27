import asyncio
from types import SimpleNamespace

from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser


def test_build_result_captures_only_body_emotes():
    async def run():
        async def catalog():
            return {'[cube_鳄鱼]': 'https://example.com/a.png', '[cube_笑]': 'https://example.com/b.png'}

        async def videos(_link):
            return []

        parser = SimpleNamespace(
            platform=XiaoHeiHeParser.platform,
            _fetch_emote_catalog=catalog,
            _parse_body=lambda _link: ('正文[cube_鳄鱼]', [], [('text', '正文[cube_鳄鱼]')], ''),
            _video_contents=videos,
            _format_date=XiaoHeiHeParser._format_date,
            comment_settings=SimpleNamespace(enabled=False),
        )
        result = await XiaoHeiHeParser._build_result(parser, '123', {'user': {'username': '作者'}})
        assert result.extra['card_emote_captures'] == ['[cube_鳄鱼]']
        assert len(result.extra['card_emotes']) == 2

    asyncio.run(run())
