from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformCardTheme:
    key: str
    display_name: str
    glyph: str
    accent: str
    accent_soft: str
    background: str = "#f5f7fa"
    surface: str = "#ffffff"
    subtle: str = "#f5f7fa"
    text: str = "#17202b"
    muted: str = "#6f7a88"
    border: str = "#e2e7ed"


DEFAULT_CARD_THEME = PlatformCardTheme(
    key="default",
    display_name="Parser X",
    glyph="P",
    accent="#536579",
    accent_soft="#edf1f5",
)

BILIBILI_CARD_THEME = PlatformCardTheme(
    key="bilibili",
    display_name="B站动态",
    glyph="B",
    accent="#fb7299",
    accent_soft="#fff0f5",
)

DOUYIN_CARD_THEME = PlatformCardTheme(
    key="douyin",
    display_name="抖音",
    glyph="抖",
    accent="#fe2c55",
    accent_soft="#fff0f3",
)

KUAISHOU_CARD_THEME = PlatformCardTheme(
    key="kuaishou",
    display_name="快手",
    glyph="快",
    accent="#ff4906",
    accent_soft="#fff2ec",
)

MIYOUSHE_CARD_THEME = PlatformCardTheme(
    key="miyoushe",
    display_name="米游社",
    glyph="米",
    accent="#00b8e6",
    accent_soft="#e9faff",
)

XIAOHEIHE_CARD_THEME = PlatformCardTheme(
    key="xiaoheihe",
    display_name="小黑盒",
    glyph="盒",
    accent="#ff6a00",
    accent_soft="#fff3ec",
)

XIAOHONGSHU_CARD_THEME = PlatformCardTheme(
    key="xiaohongshu",
    display_name="小红书",
    glyph="薯",
    accent="#ff2442",
    accent_soft="#fff0f1",
)

WEIBO_CARD_THEME = PlatformCardTheme(
    key="weibo",
    display_name="微博",
    glyph="微",
    accent="#ff8200",
    accent_soft="#fff7e7",
)

ACFUN_CARD_THEME = PlatformCardTheme(
    key="acfun",
    display_name="AcFun",
    glyph="A",
    accent="#fd4c5b",
    accent_soft="#fff0f2",
)

XIGUA_CARD_THEME = PlatformCardTheme(
    key="xigua",
    display_name="西瓜视频",
    glyph="西",
    accent="#ff4a45",
    accent_soft="#fff0ef",
)

PIPIXIA_CARD_THEME = PlatformCardTheme(
    key="pipixia",
    display_name="皮皮虾",
    glyph="皮",
    accent="#ff5a36",
    accent_soft="#fff2ed",
)

WEISHI_CARD_THEME = PlatformCardTheme(
    key="weishi",
    display_name="微视",
    glyph="视",
    accent="#ff5a65",
    accent_soft="#fff0f2",
)

NETEASE_MUSIC_CARD_THEME = PlatformCardTheme(
    key="netease_music",
    display_name="网易云音乐",
    glyph="云",
    accent="#e60026",
    accent_soft="#fff0f2",
)


_THEME_ALIASES = {
    "bilibili": BILIBILI_CARD_THEME,
    "bili": BILIBILI_CARD_THEME,
    "b站": BILIBILI_CARD_THEME,
    "b站动态": BILIBILI_CARD_THEME,
    "哔哩哔哩": BILIBILI_CARD_THEME,
    "douyin": DOUYIN_CARD_THEME,
    "抖音": DOUYIN_CARD_THEME,
    "kuaishou": KUAISHOU_CARD_THEME,
    "快手": KUAISHOU_CARD_THEME,
    "miyoushe": MIYOUSHE_CARD_THEME,
    "mihoyo": MIYOUSHE_CARD_THEME,
    "米游社": MIYOUSHE_CARD_THEME,
    "米哈游": MIYOUSHE_CARD_THEME,
    "xiaoheihe": XIAOHEIHE_CARD_THEME,
    "heybox": XIAOHEIHE_CARD_THEME,
    "小黑盒": XIAOHEIHE_CARD_THEME,
    "xiaohongshu": XIAOHONGSHU_CARD_THEME,
    "xhs": XIAOHONGSHU_CARD_THEME,
    "小红书": XIAOHONGSHU_CARD_THEME,
    "weibo": WEIBO_CARD_THEME,
    "微博": WEIBO_CARD_THEME,
    "acfun": ACFUN_CARD_THEME,
    "a站": ACFUN_CARD_THEME,
    "xigua": XIGUA_CARD_THEME,
    "西瓜视频": XIGUA_CARD_THEME,
    "pipixia": PIPIXIA_CARD_THEME,
    "皮皮虾": PIPIXIA_CARD_THEME,
    "weishi": WEISHI_CARD_THEME,
    "微视": WEISHI_CARD_THEME,
    "netease_music": NETEASE_MUSIC_CARD_THEME,
    "neteasemusic": NETEASE_MUSIC_CARD_THEME,
    "网易云音乐": NETEASE_MUSIC_CARD_THEME,
}


def resolve_card_theme(
    platform_key: str | None = None,
    platform_name: str | None = None,
) -> PlatformCardTheme:
    for candidate in (platform_key, platform_name):
        normalized = str(candidate or "").strip().lower().replace(" ", "")
        if not normalized:
            continue
        if theme := _THEME_ALIASES.get(normalized):
            return theme
        for alias, theme in _THEME_ALIASES.items():
            if alias in normalized:
                return theme
    return DEFAULT_CARD_THEME


__all__ = [
    "ACFUN_CARD_THEME",
    "BILIBILI_CARD_THEME",
    "DEFAULT_CARD_THEME",
    "DOUYIN_CARD_THEME",
    "KUAISHOU_CARD_THEME",
    "MIYOUSHE_CARD_THEME",
    "NETEASE_MUSIC_CARD_THEME",
    "PIPIXIA_CARD_THEME",
    "PlatformCardTheme",
    "WEIBO_CARD_THEME",
    "WEISHI_CARD_THEME",
    "XIGUA_CARD_THEME",
    "XIAOHEIHE_CARD_THEME",
    "XIAOHONGSHU_CARD_THEME",
    "resolve_card_theme",
]
