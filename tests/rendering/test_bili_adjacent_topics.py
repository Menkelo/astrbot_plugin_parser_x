import pytest
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


@pytest.mark.parametrize('timestamps', [False, True])
def test_bili_adjacent_opening_hash_topics_are_all_highlighted(timestamps):
    text = '#海绵宝宝#章鱼哥#比奇堡'
    html = UnifiedCardRenderer._render_basic_text_html(text, platform_key='bilibili', bili_timestamps=timestamps)
    assert html == ''.join(f'<span class="text-link">{token}</span>' for token in ['#海绵宝宝', '#章鱼哥', '#比奇堡'])


def test_bili_topics_do_not_split_url_fragments():
    html = UnifiedCardRenderer._render_basic_text_html('https://example.com/a#片段 #海绵宝宝#章鱼哥', platform_key='bilibili')
    assert '<span class="text-link">https://example.com/a#片段</span>' in html
    assert '<span class="text-link">#章鱼哥</span>' in html


def test_other_platform_closed_topics_keep_their_original_rule():
    html = UnifiedCardRenderer._render_basic_text_html('#话题#正文', platform_key='weibo')
    assert html == '<span class="text-link">#话题#</span>正文'
