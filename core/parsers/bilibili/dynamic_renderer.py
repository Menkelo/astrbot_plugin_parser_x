from pathlib import Path

from ...text_renderer import TextCardRenderer


class BiliDynamicRenderer(TextCardRenderer):
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
            platform_name="Bilibili",
            author_name=author_name,
            author_avatar=author_avatar,
            title=title,
            text=text or "（无正文）",
            timestamp_text=timestamp_text,
        )
