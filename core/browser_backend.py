"""本地 HTML→图片后端：插件管理的 headless Chromium（Playwright，需预装浏览器）。

这是插件里**唯一**接触浏览器的地方，浏览器按需启动。

字体：中文/西文统一用插件自带的思源黑体子集（``assets/fonts/*.woff2``）。做法照
``astrbot_plugin_quote_core``：把字体以 base64 内联进页面的 ``@font-face``，字族名用
``Noto Sans SC``（卡片 CSS 里本来就有这个名字）。这样字体跟着 HTML 走 —— 不用装系统
字体、不用 fontconfig、不用环境变量，换任何容器都开箱即用。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 仅用于让网页按桌面 Chrome 渲染，避免站点给出移动端降级样式。
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 容器内跑 Chromium 必需的开关：没有 user namespace 时 --no-sandbox 是硬性要求，
# /dev/shm 偏小的镜像要靠 --disable-dev-shm-usage 兜底，--disable-gpu 免掉驱动。
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--hide-scrollbars",
]

#: 插件自带的字体目录（``<插件根>/assets/fonts``）。
PLUGIN_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: 内联字体的字族名。刻意用 ``Noto Sans SC`` —— 卡片 CSS 的字体栈里已有此名，
#: 而它**不在**系统字体里（容器系统装的是 ``Noto Sans CJK SC``，两个不同名字），
#: 所以内嵌的这一份会且只会被我们的页面用到。
FONT_FAMILY = "Noto Sans SC"

#: 字重 → 文件名。卡片只用得到 400（正文）与 700（加粗），其余字重由浏览器合成。
_FONT_FILES = {400: "NotoSansSC-400.woff2", 700: "NotoSansSC-700.woff2"}

#: 内联样式块的 id，同时用作「是否已注入」的幂等判据。
FONT_STYLE_ID = "parser-x-font-faces"

_font_css_cache: str | None = None


def _font_face_css() -> str:
    """把插件自带字体编成 ``@font-face`` CSS（base64 内联），结果进程内缓存。

    找不到字体文件时返回空串，调用方继续走系统字体（不报错、不出图失败）。
    """
    global _font_css_cache
    if _font_css_cache is not None:
        return _font_css_cache

    rules: list[str] = []
    missing: list[str] = []
    for weight, name in _FONT_FILES.items():
        path = PLUGIN_FONT_DIR / name
        if not path.is_file():
            missing.append(name)
            continue
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{FONT_FAMILY}';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{payload}) format('woff2')}}"
        )

    if rules:
        size = sum((PLUGIN_FONT_DIR / n).stat().st_size for n in _FONT_FILES.values() if (PLUGIN_FONT_DIR / n).is_file())
        logger.info(
            f"Parser X 使用插件自带字体内联渲染：{FONT_FAMILY}"
            f"（{len(rules)} 个字重，{size / 1024 / 1024:.1f} MB）"
        )
    if missing:
        logger.warning(
            f"Parser X 插件自带字体缺失 {missing}（{PLUGIN_FONT_DIR}），回退容器系统字体"
        )

    _font_css_cache = "".join(rules)
    return _font_css_cache


def _inject_font_css(template: str) -> str:
    """把 ``@font-face`` 塞进页面头部。

    幂等判据用**样式块自己的 id**，不能用「页面里有没有出现字族名」——卡片 CSS 的
    字体栈里本来就写着 ``Noto Sans SC``，那样会让注入被永久跳过。
    """
    css = _font_face_css()
    if not css or FONT_STYLE_ID in template:
        return template
    block = f'<style id="{FONT_STYLE_ID}">{css}</style>'
    lowered = template.lower()
    for anchor in ("</head>", "<body"):
        idx = lowered.find(anchor)
        if idx != -1:
            return template[:idx] + block + template[idx:]
    return block + template


class LocalRenderBackend:
    """用插件自己管理的 headless Chromium 渲染 HTML。

    浏览器在第一次渲染时惰性启动，之后复用；``render`` 返回 PNG 字节而不是
    文件路径，这样调用方即使清理失败也不会把含有消息内容的截图留在磁盘上。
    """

    def __init__(self, *, viewport_width: int = 1000, max_pages: int = 4) -> None:
        self.viewport_width = max(320, int(viewport_width))
        self.max_pages = max(1, int(max_pages))
        self._playwright: Any = None
        self._browser: Any = None
        self._user_data_dir: str | None = None
        self._pages: asyncio.Semaphore | None = None
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def ensure_ready(self) -> Any:
        """返回可用的浏览器实例，首次调用时启动 Chromium。"""
        if self._browser is not None:
            return self._browser
        async with self._start_lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Parser X 本地渲染缺少 Playwright。请在 AstrBot 使用的 Python 环境中执行 "
                    "python -m pip install playwright，然后执行 "
                    "python -m playwright install chromium；详见 docs/INSTALL.md。"
                ) from exc

            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=list(_LAUNCH_ARGS),
                )
            except BaseException as exc:
                await playwright.stop()
                if isinstance(exc, Exception):
                    raise RuntimeError(
                        "Parser X 无法启动本地 Chromium。请使用 AstrBot 同一 Python 环境执行 "
                        "python -m playwright install chromium；Linux 缺系统库时由管理员执行 "
                        "python -m playwright install --with-deps chromium。"
                        "浏览器二进制并不随插件提供，详见 docs/INSTALL.md。"
                    ) from exc
                raise
            self._playwright = playwright
            self._browser = browser
            _font_face_css()  # 提前预热并记录一次日志
            logger.info("Parser X 本地渲染后端就绪：插件内 headless Chromium 已启动")
            return browser

    async def aclose(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # pragma: no cover - 关闭失败不影响插件退出
                logger.debug("Parser X 本地渲染浏览器关闭失败", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # pragma: no cover
                logger.debug("Parser X 本地渲染 Playwright 停止失败", exc_info=True)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    async def render(
        self,
        template: str,
        data: dict | None = None,
        *,
        options: dict | None = None,
    ) -> bytes:
        options = options or {}
        timeout_ms = int(options.get("timeout") or 45_000)
        viewport_height = max(200, int(options.get("viewport_height") or 900))
        # 版式可以按需要压缩视口宽度：手机版式用 480 宽排版，让 CSS 断点生效，
        # 再用 device_scale_factor 把物理像素补回来，成品图不至于模糊。
        viewport_width = max(320, int(options.get("viewport_width") or self.viewport_width))
        dsf = float(options.get("device_scale_factor") or 1)
        wait_until = str(options.get("wait_until") or "load")
        settle_ms = max(0, int(options.get("settle_ms") or 150))
        full_page = bool(options.get("full_page", True))

        browser = await self.ensure_ready()
        if self._pages is None:
            self._pages = asyncio.Semaphore(self.max_pages)
        async with self._pages:
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": viewport_height},
                device_scale_factor=dsf,
                user_agent=_CHROME_UA,
            )
            try:
                try:
                    await page.set_content(
                        _inject_font_css(template),
                        wait_until=wait_until,
                        timeout=timeout_ms,
                    )
                except Exception:
                    # 远程头像/表情图卡住时 networkidle 会超时；页面本身通常已经
                    # 渲染好，继续截图比直接失败更实用。
                    logger.debug(
                        "Parser X 本地渲染等待网络空闲超时，继续截图", exc_info=True
                    )
                if "__parserXGalleryReady" in template:
                    try:
                        await asyncio.wait_for(
                            page.evaluate("window.__parserXGalleryReady"), timeout=8
                        )
                    except asyncio.TimeoutError:
                        # Keep pending images in the DOM; a slow load is not a failure.
                        logger.debug("Parser X 评论拼图仍有图片加载中，保留原图布局")
                if settle_ms:
                    await asyncio.sleep(settle_ms / 1000)
                expected_images = int(options.get('require_flow_images') or 0)
                if expected_images:
                    if not full_page:
                        raise RuntimeError('微信公众号完整图文卡必须全页截图')
                    await page.wait_for_function(
                        """expected => {
                            const images = [...document.querySelectorAll('img.flow-image')];
                            return images.length === expected && images.every(img => {
                                const box = img.getBoundingClientRect();
                                const style = getComputedStyle(img);
                                return img.complete && img.naturalWidth > 0 && img.naturalHeight > 0
                                    && box.width > 0 && box.height > 0
                                    && style.visibility === 'visible' && style.display !== 'none';
                            });
                        }""", arg=expected_images, timeout=timeout_ms,
                    )
                return await page.screenshot(full_page=full_page, type="png")
            finally:
                try:
                    await page.close()
                except Exception:  # pragma: no cover
                    logger.debug("Parser X 本地渲染页面关闭失败", exc_info=True)


__all__ = ["LocalRenderBackend", "PLUGIN_FONT_DIR", "FONT_FAMILY"]
