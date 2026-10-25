from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .comment_render_base import CommentCanvasBase


@dataclass(slots=True)
class CommentTheme:
    """评论图的平台标识。

    评论图的配色已统一写在 ``comment_render_base._CSS`` 里（黑底），
    各平台不再有自己的色板；这里只保留平台名，供 ``_LAYOUT_CSS``
    查找字号/间距覆盖。
    """

    platform_name: str


DOUYIN_THEME = CommentTheme(platform_name="抖音")
WEIBO_THEME = CommentTheme(platform_name="微博")
XIAOHONGSHU_THEME = CommentTheme(platform_name="小红书")
XIAOHEIHE_THEME = CommentTheme(platform_name="小黑盒")


@dataclass(slots=True)
class CommentRichPart:
    kind: Literal["text", "line-break", "highlight", "emote", "emoji-text"]
    text: str = ""
    url: str = ""


@dataclass(slots=True)
class CommentBadge:
    text: str
    foreground: str = ""
    background: str = ""
    border: str = ""


@dataclass(slots=True)
class CommentAuthor:
    nickname: str
    avatar: str = ""
    nickname_color: str = ""
    # 是否为作品作者本人（评论区「作者」标记）；weibo/bili 的无画布路径会赋值。
    is_up: bool = False
    badges: list[CommentBadge] = field(default_factory=list)


@dataclass(slots=True)
class CommentEntry:
    author: CommentAuthor
    content: list[CommentRichPart]
    images: list[str] = field(default_factory=list)
    sticker_image: str = ""
    time_text: str = ""
    location: str = ""
    like_text: str = "0"
    reply_text: str = "回复"
    pinned: bool = False
    creator_liked: bool = False
    meta_items: list[str] = field(default_factory=list)
    first_reply: CommentEntry | None = None


@dataclass(slots=True)
class CommentDocument:
    theme: CommentTheme
    work_title: str
    cover: str
    total_text: str
    entries: list[CommentEntry]


class SocialCommentCanvas(CommentCanvasBase):
    """Douyin/Weibo comment canvas; uses the unified minimal template."""



__all__ = [
    "CommentAuthor",
    "CommentBadge",
    "CommentDocument",
    "CommentEntry",
    "CommentRichPart",
    "CommentTheme",
    "DOUYIN_THEME",
    "SocialCommentCanvas",
    "WEIBO_THEME",
    "XIAOHONGSHU_THEME",
    "XIAOHEIHE_THEME",
]
