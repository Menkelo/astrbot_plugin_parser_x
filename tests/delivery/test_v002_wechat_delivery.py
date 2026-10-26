"""Narrow WX complete-inline static-image consumption through real send entry."""
import asyncio
from types import SimpleNamespace
from pathlib import Path
import pytest
from PIL import Image as PILImage
from astrbot.api.message_components import Image, Nodes, Video, File
from astrbot_plugin_parser_x.core.data import ImageContent, AudioContent, VideoContent, ParseResult, Platform, DeliveryPlan, DeliveryBatch
from astrbot_plugin_parser_x.core.utils import generate_file_name
from astrbot_plugin_parser_x.main import ParserXPlugin

@pytest.mark.asyncio
async def test_real_chromium_wx_flow_contract(tmp_path):
    import base64
    from astrbot_plugin_parser_x.core.browser_backend import LocalRenderBackend
    source=tmp_path/'valid.png';PILImage.new('RGB',(20,20),'red').save(source)
    good='data:image/png;base64,'+base64.b64encode(source.read_bytes()).decode()
    backend=LocalRenderBackend()
    try:
        payload=await backend.render('<img class="flow-image" src="'+good+'">',options={'require_flow_images':1,'timeout':1000})
        assert payload.startswith(b'\x89PNG')
        with pytest.raises(Exception):
            await backend.render('<img class="flow-image" src="data:image/png;base64,broken">',options={'require_flow_images':1,'timeout':200})
        with pytest.raises(Exception):
            await backend.render('<div>missing expected image</div>',options={'require_flow_images':1,'timeout':200})
        with pytest.raises(Exception):
            await backend.render('<img class="flow-image" style="display:none" src="'+good+'">',options={'require_flow_images':1,'timeout':200})
        task=asyncio.create_task(backend.render('<div>pending expected image</div>',options={'require_flow_images':1,'timeout':10000}))
        await asyncio.sleep(.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert all(not context.pages for context in backend._browser.contexts)
    finally:await backend.aclose()


@pytest.mark.asyncio
async def test_wx_new_cache_reuses_validated_render_not_legacy(tmp_path):
    from astrbot_plugin_parser_x.core.data import ParseResult, Platform
    class Renderer(SuccessfulRenderer):
        calls=0
        async def render_card(self,*args,**kwargs):
            self.calls+=1
            await super().render_card(*args,**kwargs)
    plugin=object.__new__(ParserXPlugin);plugin.config={};plugin.cache_dir=tmp_path;plugin.text_renderer=Renderer()
    legacy=tmp_path/'legacy.png';PILImage.new('RGB',(900,500)).save(legacy)
    result=ParseResult(Platform('wechat_article','WX'),text='body',extra={'_disk_card_path':legacy})
    first=await plugin._build_text_card_content(result)
    first_path=await first.get_path()
    assert first_path!=legacy and plugin.text_renderer.calls==1
    del plugin.comment_render_cache
    second=await plugin._build_text_card_content(result)
    assert await second.get_path()==first_path and plugin.text_renderer.calls==1
    result.text='failed render'
    class Failed:
        async def render_card(self,out_path,**kwargs):
            out_path.write_bytes(b'partial')
            raise RuntimeError('image unavailable')
    plugin.text_renderer=Failed()
    with pytest.raises(RuntimeError):await plugin._build_text_card_content(result)
    assert list(tmp_path.glob('text_card_wechat_article_*.png'))==[first_path] or all('temporary' in p.name or p==first_path for p in tmp_path.glob('text_card_wechat_article_*.png'))
    assert '_disk_card_path' not in result.extra


class SuccessfulRenderer:
    async def render_card(self, out_path, **kwargs):
        PILImage.new('RGB',(900,500),'black').save(out_path)


class Event:
    message_obj=SimpleNamespace(message_id=123)
    def __init__(self):self.sent=[]
    def get_sender_id(self):return '42'
    def get_sender_name(self):return 'test'
    def chain_result(self,c):return c
    def plain_result(self,t):return []
    async def send(self,c):self.sent.append(c)

def leaves(event):
    return [p for c in event.sent for outer in c for p in ([x for n in outer.nodes for x in n.content] if isinstance(outer,Nodes) else [outer])]

@pytest.mark.parametrize('native',[False,True])
@pytest.mark.parametrize('case',['animated_gif','animated_png','animated_webp','unknown','unmapped','hero_only','detach','other_platform','flag_false','card_failure','cards_disabled','audio','video'])
def test_preserves_sources_unless_proven_static_inline(tmp_path,native,case):
    async def run():
        url='https://mmbiz.qpic.cn/a/picture'
        suffix={'animated_gif':'.gif','animated_png':'.png','animated_webp':'.webp'}.get(case,'.png')
        source=tmp_path/generate_file_name(url,suffix)
        first=PILImage.new('RGB',(20,20),'red')
        if case.startswith('animated_'):
            first.save(source,save_all=True,append_images=[PILImage.new('RGB',(20,20),'blue')],duration=100,loop=0)
        elif case=='unknown':source.write_bytes(b'unknown image bytes')
        else:first.save(source)
        if case=='unmapped':
            other=tmp_path/'custom.png';source.rename(other);source=other
        card=tmp_path/'card.png';PILImage.new('RGB',(900,500),'black').save(card)
        cls=AudioContent if case=='audio' else VideoContent if case=='video' else ImageContent
        content=cls(source)
        extra={'native_delivery':native,'render_text_card':True,'delivery_text_card_consume_non_video':case!='flag_false','text_card_flow':[{'type':'image','url':url}],'_disk_card_path':card}
        if case=='hero_only':extra.pop('text_card_flow');extra['text_card_media']=url
        if case=='detach':extra['text_card_detach_images']=True
        platform='coolapk' if case=='other_platform' else 'wechat_article'
        result=ParseResult(Platform(platform,'test'),text='body',contents=[content],delivery=DeliveryPlan([DeliveryBatch([content])]) if native else None,extra=extra)
        plugin=object.__new__(ParserXPlugin);plugin.text_renderer=SuccessfulRenderer();plugin.config={'comments':{'enabled':case!='cards_disabled'}};plugin.cache_dir=tmp_path
        if case=='card_failure':
            result.extra.pop('_disk_card_path')
            async def fail(*args,**kwargs):raise RuntimeError('render failed')
            plugin._build_text_card_content=fail
        event=Event();await plugin._send_parse_result(event,result)
        sent=[p for p in leaves(event) if isinstance(p,(Image,Video,File))]
        assert any(str(source) in p.file for p in sent), case
    asyncio.run(run())


@pytest.mark.parametrize('native',[False,True])
def test_unproven_render_success_preserves_inline_source(tmp_path,native):
    async def run():
        url='https://mmbiz.qpic.cn/a/picture'
        source=tmp_path/generate_file_name(url,'.png');PILImage.new('RGB',(20,20),'red').save(source)
        card=tmp_path/'card.png';PILImage.new('RGB',(900,500),'black').save(card)
        content=ImageContent(source)
        result=ParseResult(Platform('wechat_article','微信公众号'),text='body',contents=[content],delivery=DeliveryPlan([DeliveryBatch([content])]) if native else None,extra={'native_delivery':native,'render_text_card':True,'delivery_text_card_consume_non_video':True,'text_card_flow':[{'type':'image','url':url}],'_disk_card_path':card})
        plugin=object.__new__(ParserXPlugin);plugin.config={};plugin.cache_dir=tmp_path
        event=Event();await plugin._send_parse_result(event,result)
        assert len([p for p in leaves(event) if isinstance(p,Image)])==1
        assert str(source) in next(p.file for p in leaves(event) if isinstance(p,Image))
    asyncio.run(run())


@pytest.mark.parametrize('native',[False,True])
def test_wechat_complete_inline_card_consumes_static_only(tmp_path,native):
    async def run():
        url='https://mmbiz.qpic.cn/a/picture'
        source=tmp_path/generate_file_name(url,'.png');PILImage.new('RGB',(20,20),'red').save(source)
        card=tmp_path/'card.png';PILImage.new('RGB',(900,500),'black').save(card)
        content=ImageContent(source)
        result=ParseResult(Platform('wechat_article','微信公众号'),text='body',contents=[content],delivery=DeliveryPlan([DeliveryBatch([content])]) if native else None,extra={'native_delivery':native,'render_text_card':True,'delivery_text_card_consume_non_video':True,'text_card_flow':[{'type':'image','url':url},{'type':'text','text':'body'}],'_disk_card_path':card})
        plugin=object.__new__(ParserXPlugin);plugin.text_renderer=SuccessfulRenderer();plugin.config={};plugin.cache_dir=tmp_path
        event=Event();await plugin._send_parse_result(event,result)
        assert len([p for p in leaves(event) if isinstance(p,Image)])==1
    asyncio.run(run())
