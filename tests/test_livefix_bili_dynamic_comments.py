"""Dynamic comments must use the outer item's reply metadata, never opus IDs."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_parser_x.core.parsers.bilibili.dynamic_service import BiliDynamicService


@pytest.mark.parametrize('kind,oid,type_', [
    ('DYNAMIC_TYPE_DRAW', 413435505, 11),
    ('DYNAMIC_TYPE_WORD', 1263699639752720392, 17),
    ('DYNAMIC_TYPE_FORWARD', 1263699639752720392, 17),
    ('DYNAMIC_TYPE_AV', 117432291236629, 1),
    ('DYNAMIC_TYPE_ARTICLE', 12345, 12),
])
def test_metadata_is_authoritative(kind, oid, type_):
    item = {'type': kind, 'basic': {'comment_id_str': str(oid), 'comment_type': type_},
            'orig': {'basic': {'comment_id_str': '1', 'comment_type': 1}}}
    assert BiliDynamicService._comment_target(item, 999) == (oid, type_)


@pytest.mark.parametrize('item,expected', [
    ({'type': 'DYNAMIC_TYPE_WORD'}, (999, 17)),
    ({'type': 'DYNAMIC_TYPE_FORWARD'}, (999, 17)),
    ({'type': 'DYNAMIC_TYPE_DRAW', 'basic': {'rid_str': '413435505'}}, (413435505, 11)),
    ({'type': 'DYNAMIC_TYPE_DRAW'}, None),
    ({'type': 'DYNAMIC_TYPE_AV', 'modules': {'module_dynamic': {'major': {'archive': {'aid': '123'}}}}}, (123, 1)),
    ({'type': 'unknown'}, None),
    ({'basic': {'comment_id_str': 'bad', 'comment_type': 11}}, None),
])
def test_legacy_fallback_does_not_guess_draw_oid(item, expected):
    assert BiliDynamicService._comment_target(item, 999) == expected


def test_factory_calls_real_build_document_signature_and_outer_owner():
    build = AsyncMock(return_value=object())
    parser = SimpleNamespace(enable_comment_card=True, comment_timeout=90,
                             comment_feed=SimpleNamespace(build_document=build))
    service = BiliDynamicService(parser)
    extra = {'text_card_comment_factory': lambda: 'original video'}
    item = {'type': 'DYNAMIC_TYPE_FORWARD', 'modules': {'module_author': {'mid': 42}}}
    service._attach_comments(extra, item, 999)
    asyncio.run(extra['text_card_comment_factory']())
    build.assert_awaited_once_with(999, 17, video_title='B站动态', video_cover=None, owner_mid=42)
    assert extra['comment_timeout'] == 90


def test_disabled_repost_drops_inherited_video_thread():
    service = BiliDynamicService(SimpleNamespace(enable_comment_card=False))
    extra = {'text_card_comment_factory': lambda: None, 'comment_timeout': 90}
    service._attach_comments(extra, {'type': 'DYNAMIC_TYPE_FORWARD'}, 999)
    assert extra == {}
