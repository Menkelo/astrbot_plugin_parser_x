"""Browser regression for complete body images; hero cropping is intentional."""
import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


def image_uri(width, height):
    output = BytesIO()
    Image.new("RGB", (width, height), "#628bc1").save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


@pytest.mark.parametrize("size", [(1236, 1413), (2400, 600), (1200, 3600), (320, 240)])
def test_body_image_scales_proportionally_without_clipping(size):
    async def run():
        source = image_uri(*size)
        html = UnifiedCardRenderer(None).build_html(
            platform_name="小黑盒", platform_key="xiaoheihe", author_name=None,
            text="", media_url=source,
            content_blocks=[{"type": "image", "url": source}],
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 900})
                await page.set_content(html, wait_until="load")
                await page.locator(".flow-image").evaluate("i => i.decode()")
                metrics = await page.locator(".flow-image").evaluate("""i => {
                    const r=i.getBoundingClientRect(), w=i.parentElement.getBoundingClientRect();
                    const hero=document.querySelector('.hero-image');
                    const h=hero.getBoundingClientRect();
                    return {width:r.width,height:r.height,container:w.width,
                        left:r.left-w.left,right:r.right-w.right,
                        heroWidth:h.width,heroHeight:h.height,heroFit:getComputedStyle(hero).objectFit};
                }""")
                assert metrics["width"] == pytest.approx(828, abs=0.1), metrics
                assert metrics["container"] == pytest.approx(828, abs=0.1), metrics
                assert metrics["left"] >= -0.1 and metrics["right"] <= 0.1, metrics
                assert metrics["height"] == pytest.approx(828 * size[1] / size[0], abs=0.1), metrics
                assert (metrics["heroWidth"], metrics["heroHeight"], metrics["heroFit"]) == (900, 480, "cover")
            finally:
                await browser.close()
    asyncio.run(run())
