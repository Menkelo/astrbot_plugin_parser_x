"""Kuaishou-only comment tokens backed by its public first-party catalogue.

The bundled snapshot comes from visionBaseEmoticons.iconUrls, the same map
used by Kuaishou's desktop comment renderer. Unknown tokens remain literal;
URLs are never inferred from a name or borrowed from another platform.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..comment_canvas import CommentRichPart

_TOKEN = re.compile(r"\[[^\[\]\r\n]+\]")


@lru_cache(maxsize=1)
def load_kuaishou_emotes() -> dict[str, str]:
    try:
        data = json.loads(Path(__file__).with_name("kuaishou_emotes.json").read_text(encoding="utf-8"))
        emotes = data.get("emotes")
        if not isinstance(emotes, dict):
            return {}
        return {token: url for token, url in emotes.items()
                if isinstance(token, str) and isinstance(url, str)
                and url.startswith(("https://p66-plat.wskwai.com/kimg/bs2/emotion/",
                                    "https://p66.a.yximgs.com/bs2/emotion/"))}
    except (OSError, ValueError, AttributeError):
        return {}


def kuaishou_comment_parts(text: str) -> list[CommentRichPart]:
    catalog = load_kuaishou_emotes()
    parts: list[CommentRichPart] = []
    for index, line in enumerate(text.splitlines()):
        if index:
            parts.append(CommentRichPart("line-break"))
        cursor = 0
        for match in _TOKEN.finditer(line):
            token = match.group()
            if not (url := catalog.get(token)):
                continue
            if match.start() > cursor:
                parts.append(CommentRichPart("text", line[cursor:match.start()]))
            parts.append(CommentRichPart("emote", token, url))
            cursor = match.end()
        if cursor < len(line):
            parts.append(CommentRichPart("text", line[cursor:]))
    return parts
