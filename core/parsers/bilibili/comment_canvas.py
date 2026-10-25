from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ...comment_render_base import CommentCanvasBase


@dataclass(slots=True)
class BiliRichPart:
    kind: Literal["text", "line-break", "highlight", "emote"]
    text: str = ""
    url: str = ""


@dataclass(slots=True)
class BiliFanMedal:
    name: str
    level: int | None = None
    background: str = ""
    foreground: str = ""
    level_background: str = ""
    level_foreground: str = ""
    border: str = ""


@dataclass(slots=True)
class BiliAuthorBadge:
    nickname: str
    avatar: str = ""
    nickname_color: str = ""
    level: int | None = None
    senior: bool = False
    is_up: bool = False
    fan_medal: BiliFanMedal | None = None


@dataclass(slots=True)
class BiliCommentDecor:
    image: str = ""
    prefix: str = ""
    number: str = ""
    text: str = ""
    color: str = ""


@dataclass(slots=True)
class BiliCommentEntry:
    author: BiliAuthorBadge
    content: list[BiliRichPart]
    images: list[str] = field(default_factory=list)
    time_text: str = ""
    location: str = ""
    like_text: str = "0"
    reply_text: str = "回复"
    pinned: bool = False
    up_liked: bool = False
    meta_items: list[str] = field(default_factory=list)
    decor: BiliCommentDecor | None = None
    first_reply: BiliCommentEntry | None = None


@dataclass(slots=True)
class BiliCommentDocument:
    work_title: str
    cover: str
    total_text: str
    entries: list[BiliCommentEntry]



class BiliCommentCanvas(CommentCanvasBase):
    """Bilibili comment canvas; uses the unified minimal template."""


__all__ = [
    "BiliAuthorBadge",
    "BiliCommentCanvas",
    "BiliCommentDecor",
    "BiliCommentDocument",
    "BiliCommentEntry",
    "BiliFanMedal",
    "BiliRichPart",
]
