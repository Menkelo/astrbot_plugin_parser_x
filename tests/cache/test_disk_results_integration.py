import asyncio
import re
from pathlib import Path
import pytest
from ..delivery.test_unified_delivery import make_plugin, CaptureEvent
from astrbot_plugin_parser_x.core.data import ParseResult, Platform, VideoContent


@pytest.mark.asyncio
async def test_disk_result_survives_fresh_plugin_without_parser_request(tmp_path):
    from astrbot_plugin_parser_x.main import ParserXPlugin
    from core.result_cache import ParseResultCache
    calls=[]
    video=tmp_path/'source.mp4';video.write_bytes(b'x'*200)
    class Parser:
        async def parse(self, keyword, searched):
            calls.append(keyword)
            return ParseResult(Platform('test','测试'),title='作品',url='https://example.com/work',contents=[VideoContent(video)])
    parser=Parser();match=re.search(r'https://.*','https://example.com/work?p=2')
    plugin=make_plugin(tmp_path)
    plugin.config={'comments':{'enabled':False},'cache_dir':str(tmp_path)}
    result=await plugin._parse_with_cache(parser,'test',match,match.group())
    await plugin._send_parse_result(CaptureEvent(),result)
    # A new plugin has neither the 300s in-memory result nor old parser objects.
    fresh=make_plugin(tmp_path);fresh.config=dict(plugin.config)
    result2=await fresh._parse_with_cache(Parser(),'test',match,match.group())
    assert len(calls)==1
    assert await result2.contents[0].get_path()==video


@pytest.mark.asyncio
async def test_card_and_media_restored_without_fetch_or_render(tmp_path):
    from ..delivery.test_unified_delivery import document
    calls=[]
    video=tmp_path/'video.mp4';video.write_bytes(b'v'*200)
    class Parser:
        async def parse(self, keyword, searched):
            calls.append('parse')
            async def comments():
                calls.append('comments')
                return document()
            return ParseResult(Platform('douyin','抖音'),title='作品',url=searched.group(),contents=[VideoContent(video)],extra={'render_text_card':True,'text_card_comment_factory':comments})
    match=re.search(r'https://.*','https://example.com/work')
    plugin=make_plugin(tmp_path);plugin.config={'comments':{'enabled':True}}
    parsed=await plugin._parse_with_cache(Parser(),'test',match,match.group())
    await plugin._send_parse_result(CaptureEvent(),parsed)
    assert calls==['parse','comments']
    fresh=make_plugin(tmp_path);fresh.config=dict(plugin.config)
    restored=await fresh._parse_with_cache(Parser(),'test',match,match.group())
    await fresh._send_parse_result(CaptureEvent(),restored)
    assert calls==['parse','comments']
    assert fresh.text_renderer.calls==[]
    assert len(fresh._comment_forward_segments(restored, object())) == 2
    assert '_disk_result_key' not in restored.extra


@pytest.mark.asyncio
async def test_disk_key_isolates_query_config_and_missing_files(tmp_path):
    calls=[]
    class Parser:
        async def parse(self, keyword, searched):
            calls.append(searched.group())
            video=tmp_path/('media'+str(len(calls))+'.mp4');video.write_bytes(b'x'*200)
            return ParseResult(Platform('test','测试'),title='作品',contents=[VideoContent(video)])
    async def request(url, config):
        p=make_plugin(tmp_path);p.config=config
        result=await p._parse_with_cache(Parser(),'test',re.search(r'https://.*',url),url)
        await p._send_parse_result(CaptureEvent(),result)
        return result
    config={'comments':{'enabled':False}}
    first=await request('https://example.com/work?p=1',config)
    await request('https://example.com/work?p=1',config)
    assert len(calls)==1
    await request('https://example.com/work?p=2',config)
    assert len(calls)==2
    await request('https://example.com/work?p=1',{**config,'performance':{'video_codec':'avc'}})
    assert len(calls)==3
    (await first.contents[0].get_path()).unlink()
    await request('https://example.com/work?p=1',config)
    assert len(calls)==4
