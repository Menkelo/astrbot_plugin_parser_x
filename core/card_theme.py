"""占位头像用的平台字形。

正文卡与评论图的配色都已统一写在各自的样式表里（``text_renderer`` 的
统一深色版式、``comment_render_base._CSS`` 的黑底），不再有分平台色板。
这里只保留「平台名 -> 首字母占位」所需的字形，避免头像为空时留白。
"""

from __future__ import annotations

# 平台名/别名 -> 占位头像里显示的单个字符
_GLYPHS: dict[str, str] = {
    "bilibili": "B",
    "bili": "B",
    "b站": "B",
    "b站动态": "B",
    "哔哩哔哩": "B",
    "douyin": "抖",
    "抖音": "抖",
    "kuaishou": "快",
    "快手": "快",
    "xiaohongshu": "薯",
    "xhs": "薯",
    "小红书": "薯",
    "weibo": "微",
    "微博": "微",
    "acfun": "A",
    "a站": "A",
    "xiaoheihe": "盒",
    "小黑盒": "盒",
    "heybox": "盒",
}

_DEFAULT_GLYPH = "P"


def resolve_platform_glyph(
    platform_key: str | None = None,
    platform_name: str | None = None,
) -> str:
    """按平台标识取占位头像用的字形，取不到就回到 ``P``。"""

    for candidate in (platform_key, platform_name):
        normalized = str(candidate or "").strip().lower().replace(" ", "")
        if not normalized:
            continue
        if glyph := _GLYPHS.get(normalized):
            return glyph
        for alias, glyph in _GLYPHS.items():
            if alias in normalized:
                return glyph
    return _DEFAULT_GLYPH


__all__ = ["resolve_platform_glyph"]
