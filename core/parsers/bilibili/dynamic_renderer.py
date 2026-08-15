from pathlib import Path

from ...html_renderer import HtmlRenderService
from ...text_renderer import TextCardRenderer


class BiliDynamicRenderer(TextCardRenderer):
    def __init__(self, render_service: HtmlRenderService):
        super().__init__(render_service)

    async def render_dynamic_card(
        self,
        out_path: Path,
        *,
        author_name: str,
        author_avatar: str | None,
        text: str | None,
        title: str | None = None,
        timestamp_text: str | None = None,
    ):
        await self.render_text_card(
            out_path=out_path,
            platform_name="B站动态",
            platform_key="bilibili",
            author_name=author_name,
            author_avatar=author_avatar,
            title=title,
            text=text or "（无正文）",
            timestamp_text=timestamp_text,
        )
