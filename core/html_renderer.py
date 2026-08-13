from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger

from .rendered_image import save_rendered_image

HtmlRenderCallable = Callable[..., Awaitable[str]]


class HtmlRenderService:
    """Shared gateway for AstrBot's official Canvas/Text2Image renderer."""

    def __init__(
        self,
        html_render: HtmlRenderCallable | None = None,
        *,
        timeout: float = 45,
        jpeg_quality: int = 84,
    ):
        self._html_render = html_render
        self._render_lock = asyncio.Lock()
        self.timeout = self._clamp_float(timeout, 45.0, 5.0, 180.0)
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
        html_render: HtmlRenderCallable | None = None,
    ) -> "HtmlRenderService":
        rendering = config.get("rendering", {}) if hasattr(config, "get") else {}
        if not isinstance(rendering, dict):
            rendering = {}
        return cls(
            html_render,
            timeout=rendering.get("timeout", 45),
            jpeg_quality=rendering.get("jpeg_quality", 84),
        )

    @property
    def available(self) -> bool:
        return self._html_render is not None

    def bind(self, html_render: HtmlRenderCallable | None) -> None:
        self._html_render = html_render

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
        if self._html_render is None:
            raise RuntimeError("AstrBot html_render 尚未注入")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        render_options = {
            "type": "png",
            "full_page": True,
            "animations": "disabled",
            "caret": "hide",
            "scale": "css",
            # AstrBot forwards this screenshot timeout in milliseconds; the
            # plugin configuration is intentionally user-facing seconds.
            "timeout": int(self.timeout * 1000),
        }
        if options:
            render_options.update(options)

        async with self._render_lock:
            try:
                rendered = await self._html_render(
                    template,
                    data or {},
                    return_url=False,
                    options=render_options,
                )
            except Exception as exc:
                raise RuntimeError(f"AstrBot html_render 渲染失败: {exc}") from exc

        rendered_path = Path(str(rendered))
        if not rendered_path.is_file() or rendered_path.stat().st_size <= 0:
            raise RuntimeError(f"AstrBot html_render 未返回有效图片: {rendered}")

        await asyncio.to_thread(
            save_rendered_image,
            rendered_path,
            out_path,
            target_width=target_width,
            fallback_width=fallback_width,
            bottom_padding=bottom_padding,
            jpeg_quality=self.jpeg_quality,
        )
        logger.debug(f"Parser X html_render 完成: {out_path.name}")
        return out_path


__all__ = ["HtmlRenderCallable", "HtmlRenderService"]
