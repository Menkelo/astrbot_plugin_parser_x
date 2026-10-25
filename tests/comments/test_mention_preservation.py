import asyncio
import pytest
from .test_comment_filter import _entry, _filter, _text
from core.comment_canvas import CommentRichPart

@pytest.mark.parametrize('text',['@用户 这段说得很有道理','@甲 @乙 这两个步骤必须一起执行','这是给 @用户 的具体建议'])
def test_normal_mentions_preserved(text):
    result=asyncio.run(_filter().apply([_entry(text)],limit=10))
    assert len(result)==1
    assert _text(result[0])==text


def test_reply_prefix_removed_without_touching_body_mentions():
    entry=_entry('')
    entry.content=[CommentRichPart('text','回复 '),CommentRichPart('highlight','@原用户'),CommentRichPart('text','：@另一个用户 这段分析是对的')]
    result=asyncio.run(_filter().apply([entry],limit=10))
    assert _text(result[0])=='@另一个用户 这段分析是对的'

@pytest.mark.parametrize('text',['@甲','@甲 @乙','@甲 快来看','@甲 @乙 快看！'])
def test_pure_summons_still_filtered(text):
    assert asyncio.run(_filter().apply([_entry(text)],limit=10))==[]
