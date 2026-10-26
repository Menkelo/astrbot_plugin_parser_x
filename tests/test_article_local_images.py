import asyncio
from pathlib import Path
import pytest
from astrbot_plugin_parser_x.core.data import ImageContent, ParseResult, Platform
from astrbot_plugin_parser_x.core.card_service import CardServiceMixin

@pytest.mark.asyncio
async def test_coolapk_card_uses_downloaded_inline_images(tmp_path):
    from PIL import Image
    source=tmp_path/'source.png'; Image.new('RGB',(8,8),'red').save(source)
    class Downloader:
        def download_img(self,url,**kwargs):
            async def done(): return source
            return asyncio.create_task(done())
    class Renderer:
        async def render_card(self,**kwargs):
            self.kwargs=kwargs
            Path(kwargs['out_path']).write_bytes(b'card')
    host=CardServiceMixin();host.config={};host.cache_dir=tmp_path
    host.downloader=Downloader();host.text_renderer=Renderer()
    result=ParseResult(platform=Platform('coolapk','酷安'),text='A B',
        contents=[ImageContent(source)],extra={'text_card_flow':[{'type':'text','text':'A'},
        {'type':'image','url':'https://image.coolapk.com/a.png'},{'type':'text','text':'B'}]})
    await host._build_text_card_content(result)
    assert host.text_renderer.kwargs['content_blocks'][1]['url'].startswith('data:image/png;base64,')

@pytest.mark.asyncio
async def test_hero_removed_from_flow_does_not_shift_download_association(tmp_path):
    from PIL import Image
    from astrbot_plugin_parser_x.core.article_images import local_coolapk_images, _data_uri
    first=tmp_path/'first.png';second=tmp_path/'second.png'
    Image.new('RGB',(8,8),'red').save(first);Image.new('RGB',(8,8),'blue').save(second)
    a='https://image.coolapk.com/a.png';b='https://image.coolapk.com/b.png'
    result=ParseResult(platform=Platform('coolapk','酷安'),contents=[ImageContent(first),ImageContent(second)],
        extra={'text_card_source_images':[a,b]})
    blocks,_,_=await local_coolapk_images(result,None,[{'type':'image','url':b}],None,None)
    assert blocks[0]['url']==_data_uri(second)
    assert blocks[0]['url']!=_data_uri(first)
