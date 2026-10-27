import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from astrbot_plugin_parser_x.core.parsers.zhihu import ZhihuParser


def test_video_anchor_url_removed_from_both_text_and_card_flow():
    p = ZhihuParser({}, SimpleNamespace(download_img=lambda *a, **kw: None))
    p._video_content = AsyncMock(return_value=object())
    p.result = lambda **kw: SimpleNamespace(**kw)
    html = '<p>正常简介</p><a class="video-box" data-lens-id="123" data-poster="https://pic1.zhimg.com/cover.jpg">https://www.zhihu.com/video/123</a>'
    r = asyncio.run(p._assemble(html, '标题', {'name': '作者'}, 'https://www.zhihu.com/question/1/answer/2'))
    assert 'zhihu.com/video' not in (r.text or '')
    assert 'zhihu.com/video' not in str(r.extra.get('text_card_flow'))
    assert '正常简介' in r.text
