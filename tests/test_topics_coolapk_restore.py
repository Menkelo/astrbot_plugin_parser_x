import pytest

from astrbot_plugin_parser_x.core.topic_text import strip_topic_tags
from astrbot_plugin_parser_x.core.card_flow import build_card_extra
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer
from astrbot_plugin_parser_x.core.parsers.coolapk import _Document, _flow


def test_topics_are_preserved_and_highlighted():
    text = '正文 #数码# #手机 #小红书[话题]#'
    assert strip_topic_tags(text) == text
    assert '#数码#' in str(build_card_extra([('text', text)]))
    html = UnifiedCardRenderer._render_text_html(text)
    assert 'class="text-link"' in html
    assert '#数码#</span>' in html
    assert '#手机</span>' in html


def test_douyin_adjacent_topics_from_screenshot_are_all_highlighted():
    text = '#黄昏的教室 #致我们逝去的校园的回忆 #青春遗憾#随笔'
    html = UnifiedCardRenderer._render_text_html(text, platform_key='douyin')
    assert html == (
        '<span class="text-link">#黄昏的教室</span> '
        '<span class="text-link">#致我们逝去的校园的回忆</span> '
        '<span class="text-link">#青春遗憾</span>'
        '<span class="text-link">#随笔</span>'
    )


@pytest.mark.parametrize('platform', [None, 'weibo', 'coolapk', 'bilibili', 'xiaohongshu'])
def test_other_platforms_keep_closed_topic_and_rich_text_semantics(platform):
    text = '#话题#正文 #小红书# @同学 https://example.com/#片段 查看链接 <正文>'
    assert UnifiedCardRenderer._render_text_html(text, platform_key=platform) == (
        '<span class="text-link">#话题#</span>正文 '
        '<span class="text-link">#小红书#</span> '
        '<span class="text-link text-mention">@同学</span> '
        '<span class="text-link">https://example.com/#片段</span> '
        '<span class="text-link">查看链接</span> &lt;正文&gt;'
    )


def test_douyin_keeps_urls_mentions_and_escaping():
    assert UnifiedCardRenderer._render_text_html(
        '#青春#随笔 @同学 https://example.com/#片段 <正文>', platform_key='douyin'
    ) == (
        '<span class="text-link">#青春</span><span class="text-link">#随笔</span> '
        '<span class="text-link text-mention">@同学</span> '
        '<span class="text-link">https://example.com/#片段</span> &lt;正文&gt;'
    )


def test_view_link_label_highlighted():
    assert 'class="text-link">查看链接</span>' in UnifiedCardRenderer._render_text_html('查看链接')


def test_inline_emote_never_becomes_body_gallery_image():
    root = _Document('<p>正文<img alt="[滑稽]" src="https://static.coolapk.com/emoticons/huaji.png"></p>').root
    flow = _flow(root, {'image.coolapk.com'})
    assert '[滑稽]' in str(flow)
    assert all(k != 'image' for k, v in flow)


def test_real_share_page_emoji_alt_without_brackets():
    root = _Document('<p>A<img class="feed-emoji" alt="t耐克嘴" src="http://static.coolapk.com/emoticons/v9/coolapk_emotion_105_tnaikezui.png">B[牛牛点赞]</p>').root
    flow = _flow(root, {'image.coolapk.com'})
    assert flow == [('text', 'A[t耐克嘴]B[牛牛点赞]')]
    from pathlib import Path
    from astrbot_plugin_parser_x.core import emote_sources
    sources = emote_sources.load_coolapk_sources(Path(emote_sources.__file__).parent)
    assert sources.static['[t耐克嘴]'].startswith('https://static.coolapk.com/')
    assert '[牛牛点赞]' not in sources.static  # No verified official static source.
    html = UnifiedCardRenderer._render_text_html(flow[0][1], platform_key='coolapk', emotes=sources.static)
    assert html.count('class="inline-emote"') == 1
    assert '[牛牛点赞]' in html
