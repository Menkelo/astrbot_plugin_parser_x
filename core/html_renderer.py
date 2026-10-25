from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger

from .config_policy import RENDER_TIMEOUT_SECONDS
from .rendered_image import save_rendered_image

# 仅为兼容历史调用点保留的类型别名；本插件不再使用该可调用对象。
HtmlRenderCallable = Callable[..., Awaitable[str]]

#: 卡片样式自带固定宽度（评论卡 900px、正文卡 760px），随后由 `target_width`
#: 把截图裁到这个宽度。本地 viewport 因此刻意比任何卡片都宽，让裁剪环节继续
#: 决定最终宽度。
VIEWPORT_WIDTH = 1000
_VIEWPORT_HEIGHT = 900


class HtmlRenderService:
    """卡片渲染网关：一律由插件自带的 headless Chromium 出图。

    HTML/CSS 由插件自己拼装、自己截图，不调用 AstrBot 的 Canvas/T2I 渲染服务，
    独立评论区图片与正文卡的内容（含昵称）也不会发往任何第三方服务器。
    """

    def __init__(
        self,
        _html_render: HtmlRenderCallable | None = None,
        *,
        timeout: float = 45,
        jpeg_quality: int = 84,
        local_backend: object | None = None,
    ):
        # `_html_render` 只是历史调用点的占位参数，渲染已固定走本地 Chromium。
        self._local_backend = local_backend
        self._render_lock = asyncio.Lock()
        self.timeout = float(RENDER_TIMEOUT_SECONDS)
        self.jpeg_quality = int(self._clamp_float(jpeg_quality, 84.0, 40.0, 100.0))

    @staticmethod
    def _clamp_float(value, default: float, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    @classmethod
    def from_config(
        cls,
        config,
        _html_render: HtmlRenderCallable | None = None,
    ) -> "HtmlRenderService":
        # Rendering settings are no longer user-configurable. Ignore stale raw
        # values even when the caller has not yet run configuration migration.
        return cls()

    @property
    def available(self) -> bool:
        # 浏览器是按需启动的，这里不做耗时的探活。
        return True

    def bind(self, _html_render: HtmlRenderCallable | None) -> None:
        """保留的历史接口：渲染固定为本地 Chromium，注入回调不再起作用。"""

    async def aclose(self) -> None:
        """释放插件自己启动的浏览器。"""
        backend = self._local_backend
        if backend is None or not hasattr(backend, "aclose"):
            return
        try:
            await backend.aclose()
        except Exception:  # pragma: no cover - 退出路径不做兜底失败
            logger.debug("Parser X 本地渲染后端关闭失败", exc_info=True)

    async def get_browser(self):
        """返回插件共享的 Chromium 实例。

        个别站点（如微博 tv 视频接口）只在真实浏览器上下文里才放行，解析器可以
        复用这里的浏览器实例自己开页面取数，不必再启动第二份 Chromium。
        """
        backend = await self._ensure_local_backend()
        return await backend.ensure_ready()

    async def _ensure_local_backend(self):
        backend = self._local_backend
        if backend is None:
            from .browser_backend import LocalRenderBackend

            backend = LocalRenderBackend(viewport_width=VIEWPORT_WIDTH)
            self._local_backend = backend
        if hasattr(backend, "ensure_ready"):
            await backend.ensure_ready()
        return backend

    async def render(
        self,
        out_path: Path,
        template: str,
        data: dict | None = None,
        *,
        options: dict | None = None,
        target_width: int | None = None,
        fallback_width: int | None = None,
        bottom_padding: int | None = None,
    ) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        render_options = {
            "type": "png",
            "full_page": True,
            "animations": "disabled",
            "caret": "hide",
            "scale": "css",
            # 截图超时是毫秒；插件配置刻意保持面向用户的秒。
            "timeout": int(self.timeout * 1000),
        }
        if options:
            render_options.update(options)

        async with self._render_lock:
            backend = await self._ensure_local_backend()
            payload = await backend.render(
                template,
                data or {},
                options={
                    **render_options,
                    "viewport_height": _VIEWPORT_HEIGHT,
                    "wait_until": "networkidle",
                },
            )
            rendered_path = self._write_temp_screenshot(payload)

        if not rendered_path.is_file() or rendered_path.stat().st_size <= 0:
            raise RuntimeError(f"卡片渲染未返回有效图片: {rendered_path}")

        try:
            await asyncio.to_thread(
                save_rendered_image,
                rendered_path,
                out_path,
                target_width=target_width,
                fallback_width=fallback_width,
                bottom_padding=bottom_padding,
                jpeg_quality=self.jpeg_quality,
            )
        finally:
            # 本地截图含消息内容，裁切完立刻删掉，不留在磁盘上。
            self._discard_screenshot(rendered_path)

        logger.debug(f"Parser X 卡片渲染完成: {out_path.name}")
        return out_path

    @staticmethod
    def _write_temp_screenshot(payload: bytes) -> Path:
        handle, raw_path = tempfile.mkstemp(prefix="parser-x-render-", suffix=".png")
        os.close(handle)
        path = Path(raw_path)
        try:
            path.write_bytes(payload)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    @staticmethod
    def _discard_screenshot(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败只记日志
            logger.debug("Parser X 本地渲染截图清理失败", exc_info=True)


__all__ = ["HtmlRenderCallable", "HtmlRenderService", "VIEWPORT_WIDTH"]
