from types import SimpleNamespace
import pytest
from core.comment_settings import CommentSettings
from core.parsers.acfun_comment import AcFunCommentFeed

@pytest.mark.asyncio
async def test_failed_catalog_keeps_text_and_retries_later():
    calls=[]
    async def post(url,**kwargs):
        calls.append(url)
        if len(calls)==1: raise TimeoutError('temporary')
        return SimpleNamespace(json=lambda:{'result':0,'emotionPackageList':[]})
    feed=AcFunCommentFeed(SimpleNamespace(comment_settings=CommentSettings.from_config({},'acfun'),headers={},client=SimpleNamespace(post=post)))
    e=feed._entry({'content':'正文[emot=acfun,999999/]'})
    await feed._resolve_emotes([e])
    assert ''.join(p.text for p in e.content)=='正文[emot=acfun,999999/]'
    await feed._resolve_emotes([e])
    assert len(calls)==2
    assert ''.join(p.text for p in e.content)=='正文[emot=acfun,999999/]'
