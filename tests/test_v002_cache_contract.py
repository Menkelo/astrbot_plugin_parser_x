"""New platform data must survive the existing strict disk snapshot codec."""
import pytest

from astrbot_plugin_parser_x.core.data import Author, ImageContent, ParseResult, Platform
from astrbot_plugin_parser_x.core.disk_result_cache import DiskResultCache


@pytest.mark.asyncio
@pytest.mark.parametrize('name,label', [
    ('tieba', '贴吧'), ('wechat_article', '微信公众号'),
    ('netease', '网易云音乐'), ('nga', 'NGA'),
    ('zhihu', '知乎'), ('coolapk', '酷安'),
])
async def test_new_platform_disk_roundtrip_preserves_body_flow_and_media(tmp_path, name, label):
    source = tmp_path / 'source.jpg'
    source.write_bytes(b'original-media-not-a-render')
    image = ImageContent(source)
    original = ParseResult(
        platform=Platform(name, label), author=Author('公开作者'),
        title='完整标题', text='第一段\n最后一段', url='https://example.test/work/123',
        contents=[image], extra={
            'text_card_flow': [{'type': 'text', 'text': '第一段'},
                               {'type': 'image', 'url': 'https://example.test/source.jpg'},
                               {'type': 'text', 'text': '最后一段'}],
            'text_card_detach_images': name == 'coolapk',
            'delivery_text_card_consume_non_video': name == 'wechat_article',
        },
    )
    assert await DiskResultCache(tmp_path).save(name, original)
    # New instance simulates a process restart, not a process-local hit.
    restored = DiskResultCache(tmp_path).load(name)
    assert restored is not None
    assert restored.platform == original.platform
    assert restored.author.name == '公开作者'
    assert restored.text == original.text
    assert restored.extra == original.extra
    assert await restored.contents[0].get_path() == source
    source.unlink()
    assert DiskResultCache(tmp_path).load(name) is None
