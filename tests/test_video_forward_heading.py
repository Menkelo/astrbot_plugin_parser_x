from types import SimpleNamespace
import pytest
from astrbot_plugin_parser_x.main import ParserXPlugin
from astrbot.api.message_components import Plain

@pytest.mark.parametrize('platform', ['bilibili', 'douyin', 'xiaohongshu', 'weibo', 'acfun', 'kuaishou', 'xiaoheihe'])
@pytest.mark.parametrize('video', [False, True])
def test_forward_heading_only_for_video(platform, video):
    result = SimpleNamespace(has_video=video, platform=SimpleNamespace(display_name=platform), extra={'text_card_comment_factory': lambda: None})
    image = object()
    segments = ParserXPlugin._comment_forward_segments(result, image)
    if video:
        assert isinstance(segments[0], Plain)
        assert segments[0].text == f'{platform} · 热门评论'
        assert segments[1:] == [image]
    else:
        assert segments == [image]
