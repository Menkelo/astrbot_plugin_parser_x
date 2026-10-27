"""Mixed Weibo source galleries must never take the pure-video delivery path."""
import asyncio
from types import SimpleNamespace

import pytest
from astrbot.api.message_components import Image, Nodes, Video
from astrbot_plugin_parser_x.core.data import ImageContent, VideoContent, ParseResult, Platform
from astrbot_plugin_parser_x.core.delivery import DeliveryMixin


@pytest.mark.parametrize('cards_enabled', [True, False])
def test_mixed_weibo_keeps_six_images_two_videos_in_one_forward(tmp_path, cards_enabled):
    class Sender(DeliveryMixin):
        config = {'comments': {'enabled': cards_enabled}}
        def _show_download_fail_tip(self):
            return True
        async def _build_text_card_content(self, result):
            return ImageContent(tmp_path / 'card.png')

    class Event:
        message_obj = SimpleNamespace(message_id=1)
        def __init__(self):
            self.sent = []
        def get_sender_id(self):
            return '42'
        def get_sender_name(self):
            return 'Tester'
        def chain_result(self, chain):
            return chain
        async def send(self, chain):
            self.sent.append(chain)

    for name in ['card.png', *[f'{i}.jpg' for i in range(6)], 'a.mp4', 'b.mp4']:
        (tmp_path / name).write_bytes(b'media fixture')
    sources = [ImageContent(tmp_path / f'{i}.jpg') for i in range(6)]
    sources += [VideoContent(tmp_path / 'a.mp4'), VideoContent(tmp_path / 'b.mp4')]
    result = ParseResult(Platform('weibo', '微博'), text='source body', contents=sources,
                         extra={'mixed_post_body_card': True, 'render_text_card': True})
    event = Event()
    asyncio.run(Sender()._deliver_parse_result(event, result))
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Nodes)
    parts = [node.content[0] for node in event.sent[0][0].nodes]
    assert len(parts) == 9
    assert sum(isinstance(part, Image) for part in parts) == (7 if cards_enabled else 6)
    assert sum(isinstance(part, Video) for part in parts) == 2
    assert [type(part) for part in parts[1:]] == [Image] * 6 + [Video] * 2
