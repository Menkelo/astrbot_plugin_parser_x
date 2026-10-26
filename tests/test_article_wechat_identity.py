"""Synthetic identity contracts, NOT captured/current WeChat page fixtures.

Live sample retrieval failed at DNS; cgiDataNew identity fields remain unverified.
"""
from pathlib import Path

import pytest

from astrbot_plugin_parser_x.core.parsers.wechat_article import WechatArticleParser

URL = 'https://mp.weixin.qq.com/s/offline'
AVATAR = 'https://mmbiz.qlogo.cn/mmbiz/profile/0?x=1&y=2'


class Downloads:
    def __init__(self):
        self.urls = []

    def download_img(self, url, **kwargs):
        self.urls.append(url)
        return Path('/tmp/offline-wechat.jpg')


@pytest.fixture
def parser(tmp_path):
    return WechatArticleParser({'cache_dir': str(tmp_path)}, Downloads())


def test_dom_account_identity_is_not_byline_or_body_media(parser):
    result = parser._parse_html('''
        <span id="js_author_name">撰稿人</span>
        <a id="js_name">公众号 &amp; 名称</a>
        <img class="profile_avatar" src="//mmbiz.qlogo.cn/mmbiz/profile/0?x=1&amp;y=2">
        <div id="js_content"><p>正文</p><img src="https://mmbiz.qpic.cn/body/0"></div>
    ''', URL)
    assert result.author is not None
    assert result.author.name == '公众号 & 名称'
    assert result.extra['text_card_avatar'] == AVATAR
    assert parser.downloader.urls == ['https://mmbiz.qpic.cn/body/0']
    assert result.text == '正文'


@pytest.mark.parametrize('body', [
    '<div id="js_content">正文</div>',
    '<script>window.cgiDataNew={item_show_type:0,content_noencode:"正文"};</script>',
    '<script>window.cgiDataNew={item_show_type:8,content_noencode:"正文"};</script>',
])
def test_legacy_string_literals_reach_every_article_branch(parser, body):
    result = parser._parse_html('''<script>
        var nickname = "老模板\\u516c众\\x26号";
        var round_head_img = "https:\\/\\/mmbiz.qlogo.cn/mmbiz/profile/0?x=1&amp;y=2";
        </script>''' + body, URL)
    assert result.author is not None
    assert result.author.name == '老模板公众&号'
    assert result.extra['text_card_avatar'] == AVATAR
    assert result.text == '正文'


@pytest.mark.parametrize('script', [
    '// var nickname="假帐号";\n// var round_head_img="' + AVATAR + '";',
    '/* var nickname="假帐号"; var round_head_img="' + AVATAR + '"; */',
    '''const decoy = 'var nickname="假帐号";';''',
    '''const decoy = `var nickname="假帐号";`;''',
    '''const decoy = /var nickname="假帐号";/;''',
    'var nickname="假帐号" + run(); var round_head_img="' + AVATAR + '" + run();',
    'var nickname=run(); var round_head_img=run();',
])
def test_script_decoys_and_expressions_are_not_identity(parser, script):
    result = parser._parse_html('<script>' + script + '</script><div id="js_content">正文</div>', URL)
    assert result.author is None
    assert 'text_card_avatar' not in result.extra


@pytest.mark.parametrize('avatar', [
    'https://mmbiz.qlogo.cn.evil/profile', 'http://127.0.0.1/a',
    'https://evil@mmbiz.qlogo.cn/a', 'https://mmbiz.qlogo.cn:999/a',
    'data:image/png;base64,x', 'javascript:alert(1)',
])
def test_untrusted_profile_avatar_is_not_emitted(parser, avatar):
    result = parser._parse_html('<a id="js_name">帐号</a><img class="profile_avatar" src="'
        + avatar + '"><div id="js_content">正文</div>', URL)
    assert result.author.name == '帐号'
    assert 'text_card_avatar' not in result.extra


def test_body_identity_decoys_and_byline_are_not_account_identity(parser):
    result = parser._parse_html('''<span id="js_author_name">作者署名</span>
        <div id="js_content"><a id="js_name">正文伪装</a>
        <img class="profile_avatar" src="https://mmbiz.qpic.cn/body/0">
        <script>var nickname="正文脚本";</script></div>''', URL)
    assert result.author is None
    assert 'text_card_avatar' not in result.extra


def test_dom_identity_takes_precedence_over_legacy(parser):
    result = parser._parse_html('''<a id="js_name">DOM帐号</a>
        <img class="profile_avatar" data-src="https://mmbiz.qlogo.cn/dom/0">
        <script>var nickname="旧帐号"; var round_head_img="https://mmbiz.qlogo.cn/old/0";</script>
        <div id="js_content">正文</div>''', URL)
    assert result.author.name == 'DOM帐号'
    assert result.extra['text_card_avatar'] == 'https://mmbiz.qlogo.cn/dom/0'


def test_unverified_payload_identity_fields_are_not_guessed(parser):
    result = parser._parse_html('''<script>window.cgiDataNew={item_show_type:0,
        nickname:"未经实证",round_head_img:"https://mmbiz.qlogo.cn/guess/0",
        author:"文章署名",content_noencode:"正文"};</script>''', URL)
    assert result.author is None
    assert 'text_card_avatar' not in result.extra
