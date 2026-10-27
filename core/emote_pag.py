"""小红书官方 PAG 动画表情的首帧渲染（运行时、无本地图片素材）。

.. warning::
   当前 **未启用**（``PagFrameRenderer.PAG_RENDERING_ENABLED is False``）。
   实测结论：libpag 4.5.85 的 wasm 在容器内 Chromium（Vulkan/SwiftShader）下
   ``PAGFile`` 能正常解析（312x312、30fps、53 层、层类型齐全），但
   ``PAGPlayer.flush()`` 之后画布始终是全透明——WebGL 表面与 PAGSurface 快照
   两条路都拿不到像素。因此 PAG-only 表情（小红书部分 ``[xxR]``）目前按文本输出，
   不走本模块。保留实现是为了将来 libpag 修复后能直接打开开关。



官方 ``red_moji_pag`` 是 libpag（PAG）格式的动画表情包，静态图目录里没有这些
表情的图片。这里在运行时做两件事：

1. 下载官方 bundle，按 page 名取出该表情的 PAG 数据；
2. 用官方 libpag 的 WebAssembly 构建，在插件自己的 headless Chromium 里把首帧
   画到 canvas 上，取回 PNG。

素材与渲染器都不进仓库、不进安装包：libpag 的 JS/WASM 在首次需要时从官方 npm
构建拉取，缓存到插件数据目录；任何一步失败都只让表情回落到文本。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .emote_sources import (
    XIAOHONGSHU_PAG_BUNDLE_NAME,
    XIAOHONGSHU_PAG_MANIFEST,
    XIAOHONGSHU_PAG_PAGES,
    load_json,
)

#: 官方 libpag 的 WebAssembly 构建。npm 包里的 ``src/wasm/libpag.js`` 是未打包的
#: emscripten 胶水层，浏览器的 ``<script type="module">`` 加载不了 import 写法，
#: 因此改用 ``lib/libpag.esm.js`` + ``lib/libpag.wasm`` 这对官方发行版，且以
#: data URI 内联注入（file:// 会被 Chromium 拦下）。两者都只本地缓存、不进仓库。
LIBPAG_VERSION = "4.5.85"
LIBPAG_NPM = (
    "https://registry.npmmirror.com/libpag/-/libpag-{version}.tgz"
)
LIBPAG_CDNS = (
    "https://cdn.jsdelivr.net/npm/libpag@{version}/lib/{name}",
    "https://unpkg.com/libpag@{version}/lib/{name}",
)
LIBPAG_FILES = ("libpag.esm.js", "libpag.wasm")

#: 单个 PAG 文件上限。
MAX_PAG_BYTES = 4 * 1024 * 1024
#: bundle 上限（官方当前约 2.4MB）。
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_LIBPAG_BYTES = 8 * 1024 * 1024

_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><canvas id="c" width="96" height="96"></canvas><script type="module">
// libpag 发行版构建；路径由 Playwright 路由就地应答，见 PagFrameRenderer._make_handler。
import { PAGInit } from '%s';
window.__ready = PAGInit({ locateFile: () => '%s' }).then(
  (p) => { window.PAG = p; window.__ver = '%s'; },
  (e) => { window.__err = String(e); },
);
</script></body></html>"""

_RENDER_ONE_JS = r"""async (payload) => {
  if (!window.__ready) return {error: window.__err || 'libpag 未加载'};
  await window.__ready;
  const bin = atob(payload.b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  let file;
  try {
    // 官方包按“一个 .pag = 一个动画”组织，首帧就是该表情的静态样子。
    file = window.PAG.PAGFile.loadFromBuffer(bytes);
  } catch (err) {
    return {error: 'Load PAGFile fail: ' + String(err)};
  }
  if (!file) return {error: 'PAGFile null'};
  const canvas = document.getElementById('c');
  const w = Math.max(1, Math.round(Number(file.width()) || 1));
  const h = Math.max(1, Math.round(Number(file.height()) || 1));
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  ctx.clearRect(0, 0, w, h);
  const player = window.PAG.PAGPlayer.create();
  player.setSurface(window.PAG.PAGSurface.fromCanvas('c'));
  player.setComposition(file);
  player.setProgress(0);
  const ret = player.flush();
  if (ret && typeof ret.then === 'function') await ret;
  const px = ctx.getImageData(0, 0, w, h).data;
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (px[(y * w + x) * 4 + 3] > 8) {
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
  }
  if (x1 < 0) return {error: 'PAG 首帧为空'};
  const cw = x1 - x0 + 1, ch = y1 - y0 + 1;
  const size = 96;
  const out = document.createElement('canvas');
  out.width = size; out.height = size;
  const octx = out.getContext('2d');
  const scale = Math.min(size / cw, size / ch);
  const dw = Math.max(1, Math.round(cw * scale)), dh = Math.max(1, Math.round(ch * scale));
  octx.drawImage(
    canvas, x0, y0, cw, ch,
    Math.round((size - dw) / 2), Math.round((size - dh) / 2), dw, dh,
  );
  return {png: out.toDataURL('image/png'), w, h};
}"""

_PRELOAD_JS = r"""async (payload) => {
  if (!window.__ready) return {error: window.__err || 'libpag 未加载'};
  await window.__ready;
  for (const item of payload) {
    const bin = atob(item.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    try { window.PAG.PAGFile.loadFromBuffer(bytes); } catch (err) { /* 单个坏包不影响整体 */ }
  }
  return {ok: true};
}"""


class PagFrameRenderer:
    """用插件共享浏览器渲染 PAG 首帧。素材按需下载并缓存到数据目录。"""

    #: 见模块说明：SwiftShader 环境下取不到像素，默认关闭，避免空白表情进入卡片。
    PAG_RENDERING_ENABLED = False

    def __init__(self, plugin: Any):
        self.plugin = plugin
        cache_dir = getattr(plugin, "cache_dir", None)
        self.root = (Path(cache_dir) if cache_dir else Path(".")) / "emote_cache" / "pag"
        self._lock = asyncio.Lock()
        self._pages: dict[str, bytes] = {}
        self._libpag: tuple[bytes, bytes, str] | None = None


    # -- 素材 -----------------------------------------------------------
    async def _fetch(self, url: str, limit: int) -> bytes | None:
        http_get = getattr(self.plugin, "http_get", None)
        if not callable(http_get):
            return None
        try:
            response = await http_get(url, timeout=30, retries=1)
        except Exception as exc:
            logger.debug(f"素材下载失败 {url}: {exc}")
            return None
        status = int(getattr(response, "status_code", 200) or 200)
        raw = bytes(getattr(response, "content", b"") or b"")
        if status >= 400 or not raw or len(raw) > limit:
            logger.debug(f"素材不可用 {url}: HTTP {status} {len(raw)}B")
            return None
        return raw

    async def _cached_fetch(self, name: str, urls: list[str], limit: int) -> bytes | None:
        path = self.root / name
        if path.is_file():
            raw = path.read_bytes()
            if 0 < len(raw) <= limit:
                return raw
        for url in urls:
            raw = await self._fetch(url, limit)
            if raw is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            except OSError:
                logger.debug("素材缓存写入失败", exc_info=True)
            return raw
        return None

    async def _libpag_files(self) -> tuple[bytes, bytes, str] | None:
        """返回 (esm 源码, wasm 字节, 版本号)；失败返回 None。"""
        if self._libpag is not None:
            return self._libpag
        outputs: dict[str, bytes] = {}
        for name in LIBPAG_FILES:
            urls = [tpl.format(version=LIBPAG_VERSION, name=name) for tpl in LIBPAG_CDNS]
            raw = await self._cached_fetch(name, urls, MAX_LIBPAG_BYTES)
            if raw is None:
                raw = await self._from_tarball(name)
            if raw is None:
                return None
            outputs[name] = raw
        self._libpag = (
            outputs["libpag.esm.js"],
            outputs["libpag.wasm"],
            LIBPAG_VERSION,
        )
        return self._libpag

    @staticmethod
    def _prefix(assets: dict[str, bytes]) -> str:
        token = hashlib.sha256(
            b"".join(assets[name] for name in sorted(assets))
        ).hexdigest()[:16]
        return f"/emote-libpag/{token}"

    @staticmethod
    def _make_handler(assets: dict[str, bytes]):
        """在浏览器进程内就地应答 libpag 资源，不依赖任何本地监听端口。

        注意 ``Browser.route`` 不存在（Playwright 只在 BrowserContext / Page 上
        提供 route），所以这里由调用方挂在 ``context.route`` 上。
        """

        async def handler(route, request):  # noqa: ANN001
            name = request.url.split("?")[0].rsplit("/", 1)[-1]
            body = assets.get(name)
            if body is None:
                await route.fulfill(status=404, body=b"")
                return
            suffix = ".js" if name.endswith(".js") else ".wasm"
            await route.fulfill(
                status=200,
                body=body,
                headers={
                    "content-type": (
                        "text/javascript; charset=utf-8"
                        if suffix == ".js"
                        else "application/wasm"
                    ),
                    "cache-control": "no-store",
                },
            )

        return handler

    async def _from_tarball(self, name: str) -> bytes | None:
        """CDN 都不可用时，从官方 npm 包（tgz）里取出 lib/ 下的发行版文件。"""
        tgz = await self._cached_fetch(
            "libpag.tgz",
            [LIBPAG_NPM.format(version=LIBPAG_VERSION)],
            MAX_LIBPAG_BYTES * 2,
        )
        if tgz is None:
            return None
        try:
            with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as archive:
                member = archive.getmember(f"package/lib/{name}")
                handle = archive.extractfile(member)
                if handle is None:
                    return None
                raw = handle.read(MAX_LIBPAG_BYTES)
        except (KeyError, OSError, tarfile.TarError):
            logger.debug(f"libpag 包内缺少 {name}", exc_info=True)
            return None
        try:
            (self.root / name).write_bytes(raw)
        except OSError:
            logger.debug("libpag 缓存写入失败", exc_info=True)
        return raw

    async def _manifest(self) -> dict:
        data_dir = Path(getattr(self.plugin, "data_dir", self.root))
        return load_json(data_dir / XIAOHONGSHU_PAG_MANIFEST)

    async def _load_pages(self) -> dict[str, bytes]:
        if self._pages:
            return self._pages
        manifest = await self._manifest()
        pages_spec = manifest.get("pages")
        if not pages_spec:
            return {}
        url = str(manifest.get("bundle_url") or "")
        if not url.startswith("https://"):
            return {}
        archive = await self._cached_fetch(
            XIAOHONGSHU_PAG_BUNDLE_NAME + ".zip",
            [url],
            MAX_BUNDLE_BYTES,
        )
        if archive is None:
            return {}
        pages = self.split_zip(archive, pages_spec)
        if pages:
            self._pages = pages
        return pages

    @staticmethod
    def split_zip(archive: bytes, sizes: Any) -> dict[str, bytes]:
        """从官方 zip 包里按 (page 名, 长度) 取出每段 PAG。

        官方 bundle 是 zip（文件名可能被平台侧转码成非 UTF-8），所以不按文件名
        逐个 getinfo，而是按 manifest 里的顺序在 zip 条目里找名字以 ``.pag`` 结尾、
        且同名的成员；名字对不上时按“长度 + 出现顺序”兜底，长度再用 manifest
        校验，避免错位。
        """
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                members = [
                    name
                    for name in bundle.namelist()
                    if name.endswith(".pag") and not name.startswith("__MACOSX/")
                ]
                wanted = [item for item in (sizes or []) if isinstance(item, (list, tuple)) and len(item) == 2]
                if len(members) < len(wanted):
                    return {}
                blobs: dict[str, bytes] = {}
                for index, (page_name, length) in enumerate(wanted):
                    if not isinstance(page_name, str) or not isinstance(length, int) or length <= 0:
                        continue
                    base = page_name.rsplit("/", 1)[-1]
                    match = next((m for m in members if m.rsplit("/", 1)[-1] == base), None)
                    if match is None:
                        match = members[index] if index < len(members) else None
                    if match is None:
                        continue
                    data = bundle.read(match)
                    if len(data) != length:
                        continue
                    blobs[page_name] = data
                return blobs
        except (OSError, zipfile.BadZipFile):
            logger.debug("表情 bundle 解包失败", exc_info=True)
            return {}

    @staticmethod
    def split_bundle(bundle: bytes, sizes: Any) -> dict[str, bytes]:
        """按官方 manifest 的 (page 名, 长度) 顺序切出每段 PAG。"""
        pages: dict[str, bytes] = {}
        offset = 0
        for item in sizes or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, length = item
            if not isinstance(name, str) or not isinstance(length, int) or length <= 0:
                continue
            chunk = bundle[offset:offset + length]
            offset += length
            if len(chunk) != length:
                break
            pages[name] = chunk
        return pages

    # -- 渲染 -----------------------------------------------------------
    async def _open_page(self):
        render_service = getattr(self.plugin, "render_service", None)
        getter = getattr(render_service, "get_browser", None)
        if not callable(getter):
            return None
        libpag = await self._libpag_files()
        if libpag is None:
            return None
        esm, wasm, version = libpag
        browser = await getter()
        assets = {"libpag.esm.js": esm, "libpag.wasm": wasm}
        context = await browser.new_context(viewport={"width": 200, "height": 200})
        await context.route("**/emote-libpag/**", self._make_handler(assets))
        prefix = self._prefix(assets)
        base = f"http://libpag.local{prefix}"
        html = _HTML % (f"{base}/libpag.esm.js", f"{base}/libpag.wasm", version)
        page = await context.new_page()
        await page.set_content(html, wait_until="load")
        await page.wait_for_function("window.PAG || window.__err", timeout=60000)
        return page

    async def render(self, platform_key: str, tokens: list[str]) -> dict[str, str]:
        if not self.PAG_RENDERING_ENABLED:
            return {}
        if platform_key != "xiaohongshu":
            return {}
        pages = await self._load_pages()
        if not pages:
            return {}
        page_map = dict(XIAOHONGSHU_PAG_PAGES)
        output: dict[str, str] = {}
        async with self._lock:
            try:
                page = await self._open_page()
            except Exception as exc:
                logger.debug(f"PAG 渲染页面准备失败: {exc}")
                return {}
            if page is None:
                return {}
            try:
                # 预热：先把本批要渲染的 PAG 全部解码一遍。libpag 首次解码时会从
                # 网络补拉字体等子资源，逐个渲染会遇到偶发 "Load PAGFile fail"，
                # 集中预热后逐帧渲染就稳定了。
                pending = [
                    (token, pages[page_map[token]])
                    for token in tokens
                    if page_map.get(token) and page_map[token] in pages
                ]
                if pending:
                    try:
                        await asyncio.wait_for(
                            page.evaluate(
                                _PRELOAD_JS,
                                [
                                    {"b64": base64.b64encode(raw).decode("ascii")}
                                    for _, raw in pending
                                ],
                            ),
                            timeout=60,
                        )
                    except Exception as exc:
                        logger.debug(f"PAG 预热失败，继续逐帧渲染: {exc}")
                for token, raw in pending:
                    try:
                        result = await asyncio.wait_for(
                            page.evaluate(
                                _RENDER_ONE_JS,
                                {"b64": base64.b64encode(raw).decode("ascii")},
                            ),
                            timeout=30,
                        )
                    except Exception as exc:
                        logger.debug(f"PAG 渲染失败 {token}: {exc}")
                        continue
                    png = str((result or {}).get("png") or "")
                    if not png.startswith("data:image/png;base64,"):
                        logger.debug(f"PAG 渲染无结果 {token}: {result}")
                        continue
                    output[token] = png
            finally:
                try:
                    await page.context.close()
                except Exception:
                    logger.debug("PAG 渲染页面关闭失败", exc_info=True)
        return output


__all__ = ["PagFrameRenderer", "LIBPAG_CDNS", "LIBPAG_VERSION"]
