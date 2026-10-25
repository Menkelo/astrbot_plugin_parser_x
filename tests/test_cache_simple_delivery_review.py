"""Independent delivery-level acceptance for disabled comment cards."""
import asyncio
import pytest
from astrbot.api.message_components import Image, Video, Nodes
from astrbot_plugin_parser_x.core.data import DeliveryPlan, DeliveryBatch, ImageContent, VideoContent, ParseResult, Platform
from test_unified_delivery import make_plugin, CaptureEvent, forwarded_parts, document

@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('video', [False, True])
def test_disabled_comments_preserve_original_media_without_card(tmp_path, native, video):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.config['comments'] = {'enabled': False}
        path = tmp_path / ('original.mp4' if video else 'original.jpg')
        path.write_bytes(b'original')
        content = VideoContent(path) if video else ImageContent(path)
        calls = []
        async def comments():
            calls.append(1)
            return document()
        result = ParseResult(Platform('douyin', '抖音'), text='original body', contents=[content],
            delivery=DeliveryPlan([DeliveryBatch([content])]) if native else None,
            extra={'render_text_card': True, 'native_delivery': native, 'text_card_comment_factory': comments})
        event = CaptureEvent()
        await plugin._send_parse_result(event, result)
        assert not calls
        assert not plugin.text_renderer.calls
        parts = [part for chain in event.sent for part in chain if not isinstance(part, Nodes)] + forwarded_parts(event)
        media = [part for part in parts if isinstance(part, (Video, Image))]
        assert len(media) == 1
        assert str(path) in media[0].file
        assert not list(tmp_path.glob('text_card_*.png'))
    asyncio.run(run())
