from __future__ import annotations

from html import unescape
from html.parser import HTMLParser

from ..utils import normalize_image_url


class _OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_dict = {key.lower(): value for key, value in attrs if value}
        if tag.lower() == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = unescape(content).strip()
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str):
        if self._in_title and data.strip():
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str | None:
        value = self.meta.get("og:title") or " ".join(self.title_parts).strip()
        return value or None

    @property
    def description(self) -> str | None:
        value = self.meta.get("og:description") or self.meta.get("description")
        return value or None

    @property
    def image(self) -> str | None:
        return normalize_image_url(
            self.meta.get("og:image") or self.meta.get("twitter:image")
        )


def parse_open_graph(html: str) -> dict[str, str | None]:
    parser = _OpenGraphParser()
    parser.feed(html or "")
    return {
        "title": parser.title,
        "description": parser.description,
        "image": parser.image,
    }


__all__ = ["parse_open_graph"]
