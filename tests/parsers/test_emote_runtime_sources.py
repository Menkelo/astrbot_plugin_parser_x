"""运行时表情解析的契约测试。

缓存与仓库里都不放表情图片：来源表只记官方地址，首次用到时下载到数据目录，
取不到就回落到文本。这里固定三件事：

1. 插件实例没有 ``http_get`` 时，借用已注册解析器的请求入口；
2. 官方静态图能下载时解析成内联图片；
3. 下载失败时不抛异常、不产出空图，token 直接留给文本。
"""

from __future__ import annotations

import asyncio

from core.emote_runtime import EmoteResolver
from core.emote_sources import EmoteSources

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Response:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status


class _Parser:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def http_get(self, url, **_kwargs):
        self.calls.append(url)
        if self.behaviour == "ok":
            return _Response(PNG)
        raise RuntimeError("network down")


class _Plugin:
    """没有 http_get 的插件实例，只带解析器注册表。"""

    def __init__(self, tmp_path, behaviour):
        self.cache_dir = str(tmp_path)
        self.data_dir = str(tmp_path)
        self.parser_map = {"demo": _Parser(behaviour)}


def _resolver(tmp_path, behaviour, static):
    plugin = _Plugin(tmp_path, behaviour)
    resolver = EmoteResolver(plugin)
    resolver._sources["xiaohongshu"] = EmoteSources(
        static=static, pag_pages={}, base_url=""
    )
    return resolver, plugin.parser_map["demo"]


def test_uses_parser_fetcher_when_plugin_has_no_http_get(tmp_path):
    resolver, parser = _resolver(
        tmp_path, "ok", {"[贴贴R]": "https://example.invalid/a.png"}
    )
    out = asyncio.run(resolver.resolve("xiaohongshu", {"[贴贴R]"}))
    assert parser.calls == ["https://example.invalid/a.png"]
    assert out["[贴贴R]"].startswith("data:image/png;base64,")


def test_download_failure_degrades_to_text_without_raising(tmp_path):
    resolver, _parser = _resolver(
        tmp_path, "fail", {"[贴贴R]": "https://example.invalid/a.png"}
    )
    out = asyncio.run(resolver.resolve("xiaohongshu", {"[贴贴R]"}))
    assert out == {}


def test_missing_fetcher_does_not_raise(tmp_path):
    plugin = _Plugin(tmp_path, "ok")
    plugin.parser_map = {}
    resolver = EmoteResolver(plugin)
    resolver._sources["xiaohongshu"] = EmoteSources(
        static={"[贴贴R]": "https://example.invalid/a.png"}
    )
    assert asyncio.run(resolver.resolve("xiaohongshu", {"[贴贴R]"})) == {}


def test_coolapk_sources_are_bundled_not_in_data_directory(tmp_path):
    resolver = EmoteResolver(_Plugin(tmp_path, 'ok'))
    assert len(resolver.sources('coolapk').static) >= 100


def test_directory_urls_use_disk_cache_and_no_account_headers(tmp_path):
    plugin = _Plugin(tmp_path, 'ok')
    calls = []
    async def fetch(url, **kwargs):
        calls.append(kwargs)
        return _Response(PNG)
    plugin.parser_map['demo'].http_get = fetch
    resolver = EmoteResolver(plugin)
    urls = {'[微笑R]': 'https://example.invalid/a.png'}
    first = asyncio.run(resolver.resolve_urls('xiaohongshu', urls))
    second = asyncio.run(EmoteResolver(plugin).resolve_urls('xiaohongshu', urls))
    assert first == second and first
    assert len(calls) == 1
    assert calls[0]['headers'] == {'Accept': 'image/png,image/jpeg,image/gif,image/webp'}


def test_emote_uri_rejects_non_raster_data():
    from core.platform_emotes import resolve_emote_url
    for uri in ('data:text/html,<script>x</script>', 'data:image/svg+xml;base64,AAAA', 'javascript:alert(1)'):
        assert resolve_emote_url('coolapk', '[test]', {'[test]': uri}) == ''
