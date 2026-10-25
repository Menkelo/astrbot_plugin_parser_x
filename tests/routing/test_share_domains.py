import json
import pytest
from astrbot_plugin_parser_x.core.utils import extract_json_url
from astrbot_plugin_parser_x.core.parsers.ytdlp import AcFunParser

@pytest.mark.parametrize('as_string',[False,True])
def test_real_acfun_mobile_card_extracts_url(as_string):
    url='https://m.acfun.cn/v/?ac=48447328&sid=example'
    data={'app':'com.tencent.tuwen.lua','meta':{'news':{'jumpUrl':url,'tag':'AcFun'}}}
    assert extract_json_url(json.dumps(data) if as_string else data)==url

def test_acfun_mobile_query_routes():
    assert AcFunParser.search_url('https://m.acfun.cn/v/?ac=48447328&sid=example') is not None


def test_nested_acfun_url_without_legacy_fallback():
    from astrbot_plugin_parser_x.core.utils import pick_supported_share_url
    url='https://m.acfun.cn/v/?ac=48447328&sid=example'
    assert extract_json_url({'nested':{'url':url}})==url
    assert pick_supported_share_url([url])==url


def test_xiaoheihe_first_query_parameter():
    from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser
    _, match=XiaoHeiHeParser.search_url('https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=190978471')
    assert match.group(1)=='190978471'


@pytest.mark.asyncio
async def test_mobile_acfun_normalizes_before_parsing():
    parser=object.__new__(AcFunParser)
    from types import SimpleNamespace
    seen=[]
    async def parse(match):
        seen.append(match.group())
        raise RuntimeError('normalized-entry')
    parser._parse_ytdlp=parse
    _,match=AcFunParser.search_url('https://m.acfun.cn/v/?sid=abc&ac=48447328')
    with pytest.raises(RuntimeError,match='normalized-entry'):
        await parser.parse_acfun(match)
    assert seen==['https://www.acfun.cn/v/ac48447328']


@pytest.mark.parametrize('url',[
 'https://www.iesdouyin.com/share/video/7697103818928702153/',
 'https://m.weibo.cn/status/AbCd123',
 'https://mapp.api.weibo.cn/fx/token.html',
 'https://h5.video.weibo.com/show/1034:123456',
 'https://www.xiaohongshu.com/discovery/item/abc123?xsec_token=tok',
 'https://xhslink.cn/a/abc123',
 'https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=abcdef',
])
def test_existing_platform_domains_stay_extractable(url):
    assert extract_json_url({'payload':{'url':url}})==url


@pytest.mark.parametrize('url',['https://notb23.tv/abc','https://acfun.cn.evil.test/v/ac123','https://notxiaohongshu.com/explore/abc'])
def test_json_legacy_fallback_rejects_impostor_hosts(url):
    assert extract_json_url({'meta':{'news':{'jumpUrl':url}}}) is None
