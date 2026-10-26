from astrbot_plugin_parser_x.main import ParserXPlugin


def test_batch_one_registers_only_verified_new_platforms():
    plugin = object.__new__(ParserXPlugin)
    for name in ('wechat_article', 'netease', 'nga', 'coolapk', 'bilibili'):
        assert plugin._platform_enabled(name)
    for name in ('tieba', 'zhihu'):
        assert not plugin._platform_enabled(name)
