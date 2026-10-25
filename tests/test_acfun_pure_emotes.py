from types import SimpleNamespace
import pytest
from core.comment_settings import CommentSettings
from core.parsers.acfun_comment import AcFunCommentFeed

@pytest.mark.asyncio
async def test_pure_acfun_emotes_are_removed_but_mixed_text_and_images_remain():
    feed=AcFunCommentFeed(SimpleNamespace(comment_settings=CommentSettings.from_config({},'acfun'),headers={}))
    entries=[feed._entry({'content':text}) for text in ['[emot=acfun,2763/]',' [emot=acfun,1689/][emot=acfun,1803/] ','[emot=acfun,1689/]人类是害虫','来了来了','[img=图片]https://example.com/photo.jpg[/img]']]
    feed.comment_filter._has_qrcode=lambda url: no_qr()
    kept=await feed.comment_filter.apply(entries,limit=10)
    assert entries[0] not in kept
    assert entries[1] not in kept
    assert entries[2] in kept
    assert entries[3] in kept
    assert entries[4] in kept

async def no_qr():
    return False
