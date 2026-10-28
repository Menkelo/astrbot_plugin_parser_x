"""平台表情的运行时解析：官方来源 + 本地缓存，仓库与安装包零图片字节。

设计要点：

* 仓库只带“表情名 -> 官方地址/文件名”的来源表（:mod:`core.emote_sources`）。
* 图片、PAG 动画帧一律在运行时从官方 CDN 下载，落到插件数据目录的
  ``emote_cache`` 下，命中缓存就不再联网。
* 下载来的字节会转成内联 data URI 交给卡片渲染器，这样渲染进程不需要联网，
  ``networkidle`` 也不会被外部图片连接拖住。
* 任何一步失败都只是让该表情回落到 ``[表情名]`` 文本，不影响解析和投递。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger

from .emote_sources import (
    EmoteSources,
    load_coolapk_sources,
    load_xiaohongshu_sources,
)

#: 单个表情文件上限，防止异常响应把缓存撑大。
MAX_EMOTE_BYTES = 512 * 1024
#: 已解析表情地址在进程内的复用时间。
MEMORY_TTL_SECONDS = 30 * 60
#: 渲染尺寸：与评论表情的显示高度匹配，再做 2x 以适配高 DPI。
EMOTE_RENDER_PX = 96
#: PAG 首帧渲染超时（秒）。
PAG_RENDER_TIMEOUT = 25

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _mime_for(raw: bytes, suffix: str) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return _MIME_BY_SUFFIX.get(suffix.lower(), "application/octet-stream")


def to_data_uri(raw: bytes, suffix: str = "") -> str:
    mime = _mime_for(raw, suffix)
    if not mime.startswith("image/"):
        raise ValueError("不是图片数据")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class EmoteResolver:
    """把 [表情名] 解析成内联图片地址，必要时按需下载官方素材。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        # 混入类（CardServiceMixin）在插件实例上调 resolver 时用的入口。
        self.instance = plugin
        cache_dir = getattr(plugin, "cache_dir", None)
        base = Path(cache_dir) if cache_dir else Path(".")
        self.root = base / "emote_cache"
        self._memory: dict[str, tuple[float, str]] = {}
        self._sources: dict[str, EmoteSources] = {}
        self._resolved: dict[str, dict[str, str]] = {}
        self._pag_frames: dict[str, dict[str, str]] = {}
        self._pag_renderer = None

    # -- 来源表 ---------------------------------------------------------
    def sources(self, platform_key: str) -> EmoteSources:
        cached = self._sources.get(platform_key)
        if cached is not None:
            return cached
        if platform_key == "coolapk":
            value = load_coolapk_sources(Path(__file__).parent)
        elif platform_key == "xiaohongshu":
            data_dir = Path(getattr(self.plugin, "data_dir", self.root))
            value = load_xiaohongshu_sources(data_dir)
            from .platform_emotes import fallback_emote_map

            # v0.0.1 的可信静态首帧是 PAG-only 表情的离线兜底。
            value = EmoteSources(
                static={**value.static, **fallback_emote_map(platform_key)},
                pag_pages=value.pag_pages,
            )
        elif platform_key == "nga":
            # NGA 的表情是随插件分发的官方静态表，直接按目录下载。
            from .platform_emotes import fallback_emote_map

            value = EmoteSources(static=fallback_emote_map(platform_key))
        else:
            value = EmoteSources(static={})
        self._sources[platform_key] = value
        return value

    def _fetcher(self):
        """找到可用的 GET 入口。

        插件实例本身没有 ``http_get``——统一请求入口挂在解析器基类
        （core/parsers/base.py 的 ``BaseParser.http_get``）上；表情素材不是某个
        平台的解析，所以借用任意一个已注册解析器：它自带 curl_cffi 指纹与
        aiohttp IPv4 兜底，下载 CDN 图片和模型走同一条链路。
        """
        direct = getattr(self.plugin, "http_get", None)
        if callable(direct):
            return direct
        parsers = getattr(self.plugin, "parser_map", None)
        if isinstance(parsers, dict):
            for parser in parsers.values():
                getter = getattr(parser, "http_get", None)
                if callable(getter):
                    return getter
        return None

    # -- 下载 -----------------------------------------------------------
    async def _download(self, url: str, key: str, suffix: str = "") -> bytes | None:
        path = self.root / f"{_digest(key)}{suffix or ''}"
        if path.is_file():
            raw = path.read_bytes()
            if 0 < len(raw) <= MAX_EMOTE_BYTES:
                return raw
        http_get = self._fetcher()
        if http_get is None:
            return None
        try:
            # Never inherit a borrowed parser's platform Cookie/Authorization.
            response = await http_get(
                url, headers={"Accept": "image/png,image/jpeg,image/gif,image/webp"},
                timeout=10, retries=1,
            )
        except Exception as exc:  # 网络失败只降级，不抛给主流程
            logger.debug(f"表情下载失败 {url}: {exc}")
            return None
        status = int(getattr(response, "status_code", 200) or 200)
        raw = bytes(getattr(response, "content", b"") or b"")
        if status >= 400 or not raw:
            logger.debug(f"表情下载失败 {url}: HTTP {status}")
            return None
        if len(raw) > MAX_EMOTE_BYTES:
            return None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError:
            logger.debug("表情缓存写入失败", exc_info=True)
            pass
        return raw

    async def _static_uri(self, platform_key: str, token: str, url: str) -> str:
        key = f"{platform_key}:{token}:{url}"
        now = time.monotonic()
        hit = self._memory.get(key)
        if hit and now < hit[0]:
            return hit[1]
        raw = await self._download(url, key)
        if raw is None:
            return ""
        try:
            uri = to_data_uri(raw)
        except ValueError:
            return ""
        self._memory[key] = (now + MEMORY_TTL_SECONDS, uri)
        return uri

    async def resolve_urls(self, platform_key: str, sources: Mapping[str, str]) -> dict[str, str]:
        """Inline directory-provided raster images without inheriting account headers."""
        from urllib.parse import urlsplit

        output: dict[str, str] = {}
        for token, url in sources.items():
            parsed = urlsplit(str(url))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            uri = await self._static_uri(platform_key, token, url)
            if uri:
                output[token] = uri
        return output

    # -- 小红书 PAG -----------------------------------------------------
    async def _pag_render(self) -> Any:
        """PAG 首帧渲染器。

        小红书官方 ``red_moji_pag`` 里的表情是 PAG 动画，静态图目录只有一部分。
        ``emote_pag`` 用 libpag 的 WebAssembly 在插件浏览器里取首帧；这条链路
        在 SwiftShader 环境下拿不到像素（见 emote_pag 模块说明），所以默认不启用，
        PAG-only 表情按文本输出，不会把空图渲进卡片。
        """
        if self._pag_renderer is None:
            from .emote_pag import PagFrameRenderer

            self._pag_renderer = PagFrameRenderer(self.plugin)
        return self._pag_renderer

    def _pag_frames_cached(self, platform_key: str) -> dict[str, str]:
        cached = self._pag_frames.get(platform_key)
        if cached is not None:
            return cached
        path = self.root / f"{platform_key}_pag_frames.json"
        frames: dict[str, str] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):
                frames = {str(k): str(v) for k, v in loaded.items() if isinstance(v, str)}
        self._pag_frames[platform_key] = frames
        return frames

    def _store_pag_frame(self, platform_key: str, token: str, uri: str) -> None:
        frames = self._pag_frames_cached(platform_key)
        frames[token] = uri
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / f"{platform_key}_pag_frames.json").write_text(
                json.dumps(frames, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.debug("PAG 帧缓存写入失败", exc_info=True)

    async def _render_pag_frames(self, platform_key: str, tokens: set[str]) -> dict[str, str]:
        """为缺失的 PAG 表情渲染首帧，返回 token -> data URI。"""
        frames = self._pag_frames_cached(platform_key)
        missing = [token for token in tokens if token not in frames]
        if not missing:
            return {token: frames[token] for token in tokens}
        try:
            renderer = await self._pag_render()
            rendered = await renderer.render(platform_key, missing)
        except Exception as exc:
            logger.debug(f"PAG 表情渲染失败，回落到文本: {exc}")
            return {token: frames[token] for token in tokens if token in frames}
        for token, uri in rendered.items():
            if uri:
                self._store_pag_frame(platform_key, token, uri)
        frames = self._pag_frames_cached(platform_key)
        return {token: frames[token] for token in tokens if token in frames}

    # -- 对外接口 -------------------------------------------------------
    def known_tokens(self, platform_key: str) -> set[str]:
        sources = self.sources(platform_key)
        tokens = set(sources.static) | set(sources.pag_pages)
        if platform_key == "nga":
            from .platform_emotes import fallback_emote_map

            tokens |= set(fallback_emote_map(platform_key))
        if platform_key == "xiaohongshu":
            from .platform_emotes import fallback_emote_map

            tokens |= {
                token
                for token in fallback_emote_map(platform_key)
                if token.endswith("R]") or token.endswith("H]")
            }
        return tokens

    async def _resolve_platform(self, platform_key: str, tokens: set[str]) -> dict[str, str]:
        sources = self.sources(platform_key)
        output: dict[str, str] = {}
        for token in tokens:
            url = str(sources.static.get(token) or "")
            if url.startswith("data:image/"):
                output[token] = url
                continue
            if url:
                uri = await self._static_uri(platform_key, token, url)
                if uri:
                    output[token] = uri
        pag_tokens = {t for t in tokens if t not in output and t in sources.pag_pages}
        if pag_tokens:
            output.update(await self._render_pag_frames(platform_key, pag_tokens))
        return output

    def sheets(self) -> dict[str, tuple[int, int]]:
        """返回 token -> (单格边长, 图层数)，供渲染器裁切雪碧图。"""
        renderer = self._pag_renderer
        return dict(getattr(renderer, "_sheets", {}) or {})

    async def resolve(
        self,
        platform_key: str,
        tokens: set[str],
        sources: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """返回 token -> 内联图片地址；无法获得的 token 不出现在结果里。

        ``sources`` 是调用方手上的平台目录（例如抖音的签名临时地址）。抖音只在
        目录里给出带时效签名的 URL，换一个调用点就失效，因此必须用当次目录里的
        地址下载，不能落进 ``EmoteResolver`` 的长期 token 缓存。
        """
        wanted = {str(token) for token in tokens if token}
        if not wanted:
            return {}
        live = {
            str(token): str(url)
            for token, url in (sources or {}).items()
            if str(token) in wanted and str(url).startswith(("http://", "https://"))
        }
        output: dict[str, str] = {}
        if live:
            from urllib.parse import urlsplit

            for token, url in live.items():
                parsed = urlsplit(url)
                if not parsed.hostname:
                    continue
                key = f"{platform_key}:{token}:{url}"
                uri = await self._static_uri(platform_key, token, url)
                if uri:
                    output[token] = uri
            wanted = {token for token in wanted if token not in output}
        if not wanted:
            return output
        known = self.known_tokens(platform_key)
        hit = {token: self._resolved[platform_key][token]
               for token in wanted
               if self._resolved.get(platform_key, {}).get(token)}
        todo = {token for token in wanted if token in known and token not in hit}
        if todo:
            resolved = await self._resolve_platform(platform_key, todo)
            self._resolved.setdefault(platform_key, {}).update(resolved)
            hit.update(resolved)
        output.update(hit)
        return output


def make_data_uri(raw: bytes) -> str:
    return to_data_uri(raw)


__all__ = ["EmoteResolver", "MAX_EMOTE_BYTES", "to_data_uri"]
