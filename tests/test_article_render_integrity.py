from pathlib import Path
import pytest
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer

@pytest.mark.parametrize('platform,name', [('coolapk', '酷安'), ('wechat_article', '微信公众号')])
def test_public_article_platform_survives_missing_identity(platform, name):
    html = UnifiedCardRenderer(None).build_html(platform_key=platform, platform_name=name,
        author_name=None, text='正文')
    assert f'<span class="profile-platform">{name}</span>' in html
    assert '<span class="author">' not in html

@pytest.mark.asyncio
async def test_coolapk_requires_all_inline_images(tmp_path):
    class Service:
        async def render(self, path, html, **kwargs):
            self.options = kwargs['options']
    service = Service()
    await UnifiedCardRenderer(service).render_card(tmp_path/'card.png', platform_key='coolapk',
        platform_name='酷安', author_name='作者', text='A B',
        content_blocks=[('text','A'),('image','https://image.coolapk.com/a.jpg'),('text','B')])
    assert service.options['require_flow_images'] == 1
