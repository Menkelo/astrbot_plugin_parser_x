from types import SimpleNamespace
import pytest
from core.comment_settings import CommentSettings
from core.parsers.acfun_comment import AcFunCommentFeed

@pytest.mark.asyncio
async def test_mixed_emote_uses_official_catalog_and_caches_it():
    calls=[]
    async def post(url,**kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(json=lambda:{'result':0,'emotionPackageList':[{'emotions':[{'id':1689,'bigImageInfo':{'thumbnailImage':{'cdnUrls':[{'url':'https://imgs.acfun.cn/official.png'}]}}}]}]})
    feed=AcFunCommentFeed(SimpleNamespace(comment_settings=CommentSettings.from_config({},'acfun'),headers={},client=SimpleNamespace(post=post)))
    entry=feed._entry({'content':'[emot=acfun,1689/]人类是害虫'})
    await feed._resolve_emotes([entry])
    assert entry.content[0].kind=='emote'
    assert entry.content[0].url=='https://imgs.acfun.cn/official.png'
    assert entry.content[1].text=='人类是害虫'
    await feed._resolve_emotes([feed._entry({'content':'a[emot=acfun,1689/]'})])
    assert len(calls)==1
