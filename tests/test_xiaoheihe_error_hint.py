"""用户可读的小黑盒风控提示；无需真实 Cookie 或网络请求。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_parser_x.core.exception import ParseException
from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser


@pytest.mark.parametrize("device_id,msg", [(None, ""), (None, "请完成验证"), ("fake-device", "")])
def test_captcha_failure_explains_link_and_cookie_checks(device_id, msg):
    parser = object.__new__(XiaoHeiHeParser)
    parser.headers = {"Cookie": "fake-cookie"}
    response = SimpleNamespace(json=lambda: {"status": "show_captcha", "msg": msg})
    parser.http_get = AsyncMock(return_value=response)
    parser._fetch_device_id = AsyncMock(return_value=device_id)

    with pytest.raises(ParseException) as error:
        asyncio.run(parser._fetch_post("b64472a58047"))

    assert str(error.value) == "请确认链接仍可访问、Cookie 配置有效，然后重试。"
    assert parser.http_get.await_count == (2 if device_id else 1)
    assert parser.headers == {"Cookie": "fake-cookie"}


@pytest.mark.parametrize("final_status", ["ok", "failed"])
def test_device_retry_preserves_success_and_other_errors(final_status):
    parser = object.__new__(XiaoHeiHeParser)
    parser.headers = {}
    parser.http_get = AsyncMock(side_effect=[
        SimpleNamespace(json=lambda: {"status": "show_captcha"}),
        SimpleNamespace(json=lambda: {"status": final_status, "msg": "帖子不存在", "result": {"link": {"title": "保留结果"}}}),
    ])
    parser._fetch_device_id = AsyncMock(return_value="fake-device")

    if final_status == "ok":
        assert asyncio.run(parser._fetch_post("123")) == {"title": "保留结果"}
    else:
        with pytest.raises(ParseException, match="小黑盒解析失败: 帖子不存在"):
            asyncio.run(parser._fetch_post("123"))
