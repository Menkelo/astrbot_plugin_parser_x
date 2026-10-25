from types import SimpleNamespace
from core.config_policy import migrate_config
from astrbot_plugin_parser_x.main import ParserXPlugin


def test_old_group_records_become_plain_ids():
    config={'behavior':{'disabled_sessions':['Bot:GroupMessage:123',123,' 456 ','Bot:FriendMessage:123']}}
    migrate_config(config)
    assert config['behavior']['disabled_sessions']==['123','456','Bot:FriendMessage:123']
    assert not migrate_config(config)


def test_group_key_not_private_id():
    plugin=object.__new__(ParserXPlugin)
    group=SimpleNamespace(get_group_id=lambda:'123',unified_msg_origin='Bot:GroupMessage:123')
    private=SimpleNamespace(get_group_id=lambda:'',unified_msg_origin='Bot:FriendMessage:123')
    assert plugin._session_key(group)=='123'
    assert plugin._session_key(private)=='Bot:FriendMessage:123'


async def test_commands_store_and_remove_plain_group_id():
    plugin=object.__new__(ParserXPlugin)
    plugin.config={'behavior':{'disabled_sessions':[]}}
    saved=[]
    plugin._save_disabled_sessions=lambda values: (saved.append(list(values)),plugin.config['behavior'].update(disabled_sessions=list(values)))
    event=SimpleNamespace(get_group_id=lambda:'123',unified_msg_origin='Bot:GroupMessage:123',plain_result=lambda text:text)
    assert [x async for x in plugin.close_parser(event)]==['解析已关闭']
    assert saved[-1]==['123']
    assert [x async for x in plugin.open_parser(event)]==['解析已开启']
    assert saved[-1]==[]
