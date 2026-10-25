"""微博 tv/fid 视频链路：接口被风控拦截时的浏览器兜底。

真实环境取证（2026-10-24）：
- `h5.video.weibo.com/api/component` 与 `weibo.com/tv/api/component` 在 curl_cffi 下
  分别返回 `{"code":"100001","msg":"miss param"}` 与 Sina 访客/安全检查页；
- 同一链接在插件自带的 Chromium 里访问 `https://weibo.com/tv/show/<fid>` 时，
  页面自身的 XHR 会返回 `code=100000` 的 `Component_Play_Playinfo`。
因此接口失败后必须能走浏览器兜底，且两者都失败时的报错要带上接口给的原因。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_parser_x.core.data import VideoContent
from astrbot_plugin_parser_x.core.exception import ParseException
from astrbot_plugin_parser_x.core.parsers import WeiboParser

FID = "1034:5356114485641249"
URL = f"https://video.weibo.com/show?fid={FID}"

BROWSER_INFO = {
    "title": "空警世界的微博视频",
    "text": "三上老师在台湾做啦啦队。",
    "nickname": "空警世界",
    "avatar": "https://tvax1.sinaimg.cn/crop.0.0.1.jpg",
    "real_date": 1792480668,
    "duration_time": 15.325,
    "urls": {
        "超清 2K60": "//f.video.weibocdn.com/o0/uD2yJvWOlx08BIzx.mp4?label=mp4_1440p60",
        "高清 1080P": "//f.video.weibocdn.com/o0/uD2yJvWOlx08BIzx.mp4?label=mp4_1080p",
    },
}


class Downloads:
    def __init__(self, root):
        self.root, self.calls, self.tasks = root, [], []

    def start(self, kind, url):
        path = self.root / f"{len(self.calls)}.{kind}"
        self.calls.append((kind, url))

        async def download():
            await asyncio.sleep(0)
            path.write_bytes(b"fixture-media")
            return path

        task = asyncio.create_task(download())
        self.tasks.append(task)
        return task

    def download_img(self, url, **kwargs):
        return self.start("jpg", url)

    def download_video(self, url, **kwargs):
        return self.start("mp4", url)

    async def finish(self):
        await asyncio.gather(*self.tasks, return_exceptions=True)


def config(root):
    return {"cache_dir": str(root), "comments": {"weibo": True}}


def _rejected_post():
    payload = {"code": "100001", "msg": "miss param", "data": {}}
    return SimpleNamespace(status_code=200, json=lambda: payload, text="{}")


def test_browser_fallback_recovers_video_when_api_is_blocked(tmp_path):
    async def run():
        downloads = Downloads(tmp_path)
        parser = WeiboParser(config(tmp_path), downloads)
        parser._session = SimpleNamespace(post=AsyncMock(return_value=_rejected_post()))
        parser._fetch_fid_info_via_browser = AsyncMock(return_value=BROWSER_INFO)

        key, match = parser.search_url(URL)
        result = await parser.parse(key, match)

        parser._fetch_fid_info_via_browser.assert_awaited_once()
        assert len(result.contents) == 1 and isinstance(result.contents[0], VideoContent)
        assert result.contents[0].duration == pytest.approx(15.325)
        assert "weibocdn.com" in downloads.calls[0][1]
        assert result.title == "空警世界的微博视频"
        assert "啦啦队" in (result.text or "")
        assert result.author.name == "空警世界"
        await downloads.finish()

    asyncio.run(run())


def test_api_error_reason_is_reported_when_browser_also_fails(tmp_path):
    async def run():
        parser = WeiboParser(config(tmp_path), Downloads(tmp_path))
        parser._session = SimpleNamespace(post=AsyncMock(return_value=_rejected_post()))
        parser._fetch_fid_info_via_browser = AsyncMock(return_value=None)

        key, match = parser.search_url(URL)
        with pytest.raises(ParseException) as excinfo:
            await parser.parse(key, match)

        message = str(excinfo.value)
        assert "100001" in message or "miss param" in message
        assert "浏览器" in message

    asyncio.run(run())


def test_browser_is_not_used_when_the_api_succeeds(tmp_path):
    async def run():
        downloads = Downloads(tmp_path)
        parser = WeiboParser(config(tmp_path), downloads)
        parser._session = SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    json=lambda: {
                        "data": {
                            "Component_Play_Playinfo": {
                                "title": "video",
                                "text": "body",
                                "reward": {"user": {"name": "author"}},
                                "urls": {"高清 1080P": "//example.com/clip.mp4"},
                                "duration_time": 4.9,
                            }
                        }
                    },
                )
            )
        )
        parser._fetch_fid_info_via_browser = AsyncMock(return_value=None)

        key, match = parser.search_url(URL)
        result = await parser.parse(key, match)

        parser._fetch_fid_info_via_browser.assert_not_awaited()
        assert isinstance(result.contents[0], VideoContent)
        await downloads.finish()

    asyncio.run(run())


def test_browser_fallback_survives_missing_optional_fields(tmp_path):
    """浏览器兜底拿到的最小信息（只有 title + 一条播放地址）也必须能出结果。"""

    async def run():
        downloads = Downloads(tmp_path)
        parser = WeiboParser(config(tmp_path), downloads)
        parser._session = SimpleNamespace(post=AsyncMock(return_value=_rejected_post()))
        parser._fetch_fid_info_via_browser = AsyncMock(
            return_value={
                "title": "只有标题",
                "urls": {"标清": "//f.video.weibocdn.com/o0/min.mp4"},
            }
        )

        key, match = parser.search_url(URL)
        result = await parser.parse(key, match)

        assert isinstance(result.contents[0], VideoContent)
        assert result.title == "只有标题"
        assert result.text is None
        await downloads.finish()

    asyncio.run(run())


class _FakeResponse:
    def __init__(self, url, payload):
        self.url, self._payload = url, payload

    async def json(self):
        return self._payload


class _FakePage:
    def __init__(self, payload):
        self._payload = payload
        self.handlers = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, **kwargs):
        assert "weibo.com/tv/show/" in url
        self.handlers["response"](
            _FakeResponse(
                "https://weibo.com/tv/api/component?page=%2Ftv%2Fshow%2F" + FID,
                {"code": "100000", "data": {"Component_Play_Playinfo": self._payload}},
            )
        )

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, payload):
        self._payload = payload
        self.page = None
        self.closed = False

    async def new_page(self):
        self.page = _FakePage(self._payload)
        return self.page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, payload):
        self._payload = payload
        self.context = None

    async def new_context(self, **kwargs):
        self.context = _FakeContext(self._payload)
        return self.context


class _FakeRenderService:
    def __init__(self, payload):
        self.browser = _FakeBrowser(payload)

    async def get_browser(self):
        return self.browser


def test_browser_fetch_captures_component_response_and_closes_context(tmp_path):
    """浏览器兜底必须真的把页面 XHR 的 JSON 截获下来，并关闭临时上下文。"""

    async def run():
        parser = WeiboParser(config(tmp_path), Downloads(tmp_path))
        service = _FakeRenderService(BROWSER_INFO)
        parser.render_service = service

        info = await parser._fetch_fid_info_via_browser(FID)

        assert info == BROWSER_INFO
        assert service.browser.context.closed is True
        assert service.browser.context.page.closed is True

    asyncio.run(run())


def test_browser_fetch_returns_none_when_page_never_answers(tmp_path):
    class SilentPage(_FakePage):
        async def goto(self, url, **kwargs):
            return None

    class SilentContext(_FakeContext):
        async def new_page(self):
            self.page = SilentPage({})
            return self.page

    class SilentBrowser(_FakeBrowser):
        async def new_context(self, **kwargs):
            self.context = SilentContext({})
            return self.context

    class SilentService(_FakeRenderService):
        def __init__(self):
            self.browser = SilentBrowser({})

    async def run():
        parser = WeiboParser(config(tmp_path), Downloads(tmp_path))
        parser.render_service = SilentService()
        info = await parser._fetch_fid_info_via_browser(FID)
        assert info is None

    asyncio.run(run())
