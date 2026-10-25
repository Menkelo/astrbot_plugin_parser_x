import asyncio
from types import SimpleNamespace
from astrbot_plugin_parser_x.core.parsers.kuaishou import KuaiShouParser, Photo, CdnUrl

def test_video_cover_is_forwarded_to_unified_card(tmp_path):
    parser = KuaiShouParser({'cache_dir': str(tmp_path)}, None)
    parser.create_video_content = lambda *a, **k: SimpleNamespace()
    photo = Photo(caption='配文', timestamp=0, cover_urls=[CdnUrl(cdn='cdn',url='https://cdn.example/cover.jpg')], main_mv_urls=[CdnUrl(cdn='cdn',url='https://cdn.example/video.mp4')])
    result = parser._build_result_from_photo(photo,'https://www.kuaishou.com/short-video/3xphoto')
    assert result.extra.get('text_card_media') == 'https://cdn.example/cover.jpg'

def test_real_headurl_field_embedded_for_root_and_reply():
    from test_kuaishou_comment import make_feed, raw
    feed, _ = make_feed([
        {'result':1,'rootCommentsV2':[raw(headurl='http://cdn.example/root.jpg',hasSubComments=True)],'pcursorV2':'no_more'},
        {'result':1,'subCommentsV2':[raw(2,'真实回复',headurl='http://cdn.example/reply.jpg')],'pcursorV2':'no_more'},
    ])
    seen=[]
    async def embed(url):
        seen.append(url)
        return 'data:image/png;base64,' + ('root' if 'root' in url else 'reply')
    feed._avatar_to_data_uri=embed
    doc=asyncio.run(feed.fetch_document('photo'))
    assert doc.entries[0].author.avatar == 'data:image/png;base64,root'
    assert doc.entries[0].first_reply.author.avatar == 'data:image/png;base64,reply'
    assert len(seen)==2

def test_avatar_http_response_becomes_offline_image_data_uri():
    from test_kuaishou_comment import make_feed,raw
    import base64
    feed,_=make_feed([{'result':1,'rootCommentsV2':[raw(headurl='http://cdn.example/avatar.png')],'pcursorV2':'no_more'}])
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=')
    calls=[]
    async def get(url,**kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(status_code=200,content=png,headers={'Content-Type':'image/png'})
    feed.parser.headers={}
    feed.parser.http_get=get
    doc=asyncio.run(feed.fetch_document('photo'))
    assert doc.entries[0].author.avatar == 'data:image/png;base64,'+base64.b64encode(png).decode()
    assert calls[0][0]=='https://cdn.example/avatar.png'
    assert all(k.lower()!='cookie' for k in calls[0][1]['headers'])

def test_kuaishou_shortlink_resolved_feed_stops_before_page_fetch(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.base import SkipParseException
    import pytest
    parser=KuaiShouParser({'cache_dir':str(tmp_path)},None)
    calls=[]
    async def get(*args,**kwargs):
        calls.append(args[0])
        assert len(calls)==1, 'feed page was fetched after redirect'
        return SimpleNamespace(status_code=302,headers={'Location':'https://www.kuaishou.com/new-reco?source=PROFILE'})
    parser._session=SimpleNamespace(get=get)
    key,match=parser.search_url('https://v.kuaishou.com/token')
    with pytest.raises(SkipParseException):
        asyncio.run(parser.parse(key,match))
    assert len(calls)==1

def test_douyin_head_target_navigation_is_silent(tmp_path):
    from astrbot_plugin_parser_x.core.parsers.douyin import DouyinParser
    parser=DouyinParser({'cache_dir':str(tmp_path)},None)
    assert parser._is_short_link_target_unsupported('https://www.douyin.com/jingxuan')
    assert not parser._is_short_link_target_unsupported('https://www.douyin.com/jingxuan?modal_id=7420000000000000000')
