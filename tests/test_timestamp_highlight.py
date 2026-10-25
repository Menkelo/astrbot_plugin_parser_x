"""Bilibili video-position timestamps keep link styling without changing text."""
import pytest
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer
from astrbot_plugin_parser_x.core.parsers.bilibili.comment_canvas import BiliRichPart


@pytest.mark.parametrize('timestamp', ['00:00', '01:18', '06:22', '90:12', '1:02:03', '01:02:03'])
def test_bili_timestamp_has_link_highlight(timestamp):
    html = UnifiedCardRenderer(None)._render_bili_rich_text([BiliRichPart('text', text=f'{timestamp} 章节标题')])
    assert f'<span class="comment-highlight">{timestamp}</span>' in html
    assert ' 章节标题</span>' in html


@pytest.mark.parametrize('text', ['12345', '1:99', '123:45:67', '01:02:03:04', 'v1:23beta'])
def test_invalid_or_embedded_timestamps_are_not_highlighted(text):
    html = UnifiedCardRenderer(None)._render_bili_rich_text([BiliRichPart('text', text=text)])
    assert 'comment-highlight' not in html


def test_bili_timestamp_does_not_split_urls_or_escape_html_twice():
    html = UnifiedCardRenderer(None)._render_bili_rich_text([BiliRichPart('text', text='00:00 <开始> https://example.com/01:18?q=1&x=2')])
    assert html.count('comment-highlight') == 1
    assert 'https://example.com/01:18?q=1&amp;x=2</span>' in html
    assert '&lt;开始&gt;' in html
    assert '&amp;lt;' not in html


def test_regular_platform_text_does_not_acquire_timestamp_highlighting():
    html = UnifiedCardRenderer(None)._render_basic_text_html('00:00 章节标题')
    assert 'text-link' not in html
    assert 'comment-highlight' not in html
