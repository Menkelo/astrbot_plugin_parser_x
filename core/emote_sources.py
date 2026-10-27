"""小红书、酷安表情的官方来源表（不含任何图片字节）。

本模块只保存两类信息：

* 小红书：官方 ``red_moji_pag`` 动画表情包里每个表情对应的 page 地址，以及官方
  直接下发的静态图地址。
* 酷安：官方 CDN ``https://static.coolapk.com/emoticons/v9/`` 下的文件名。

图片本身一律在运行时按需下载并缓存到插件数据目录（见
:mod:`core.emote_runtime`），不进入仓库、不进入安装包。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# ---------------------------------------------------------------------------
# 小红书
# ---------------------------------------------------------------------------
#: ``red_moji_pag`` 是小红书的官方动画表情包（PAG / libpag 格式）。插件运行时
#: 下载同一份官方 bundle，按 page 名取出对应表情，再用官方 libpag 渲染首帧。
XIAOHONGSHU_PAG_BUNDLE_NAME = "red_moji_pag"
XIAOHONGSHU_PAG_MANIFEST = "xiaohongshu_emote_pag.json"

#: 评论正文里出现、但官方静态目录不提供图片的表情 -> bundle 内 page 路径。
_XIAOHONGSHU_PAG_PAGE: dict[str, str] = {
    "[亲一个R]": "red_moji_pag/[亲一个R].pag",
    "[冰淇淋R]": "red_moji_pag/[冰淇淋R].pag",
    "[变猪猪R]": "red_moji_pag/[变猪猪R].pag",
    "[吃粽子R]": "red_moji_pag/[吃粽子R].pag",
    "[呃R]": "red_moji_pag/[呃R].pag",
    "[嘻嘻R]": "red_moji_pag/[嘻嘻R].pag",
    "[坏笑R]": "red_moji_pag/[坏笑R].pag",
    "[安排R]": "red_moji_pag/[安排R].pag",
    "[完啦R]": "red_moji_pag/[完啦R].pag",
    "[尬住R]": "red_moji_pag/[尬住R].pag",
    "[心心眼R]": "red_moji_pag/[心心眼R].pag",
    "[扶额R]": "red_moji_pag/[扶额R].pag",
    "[抽泣R]": "red_moji_pag/[抽泣R].pag",
    "[捂嘴笑R]": "red_moji_pag/[捂嘴笑R].pag",
    "[栓QR]": "red_moji_pag/[栓QR].pag",
    "[棒R]": "red_moji_pag/[棒R].pag",
    "[棒棒糖R]": "red_moji_pag/[棒棒糖R].pag",
    "[泪崩R]": "red_moji_pag/[泪崩R].pag",
    "[笑哭了R]": "red_moji_pag/[笑哭了R].pag",
    "[谢谢宝R]": "red_moji_pag/[谢谢宝R].pag",
    "[贴贴R]": "red_moji_pag/[贴贴R].pag",
    "[超喜欢R]": "red_moji_pag/[超喜欢R].pag",
}

#: 官方 ``emoji/config`` 直接给出静态图的表情（有静态图就不必渲染动画首帧）。
_XIAOHONGSHU_STATIC: dict[str, str] = {
    "[贴贴R]": "https://fe-video-qc.xhscdn.com/fe-platform/d118cea97bbeb2e89b8fe0ffc81bf47273678e7f.png",
    "[栓QR]": "https://fe-video-qc.xhscdn.com/fe-platform/3189c46fa334f8b645e040a33831e9eb88d5a888.png",
    "[安排R]": "https://fe-video-qc.xhscdn.com/fe-platform/222affe21c8be521b8431df42212ef637aa943a3.png",
    "[谢谢宝R]": "https://fe-video-qc.xhscdn.com/fe-platform/f6abb97f38ac42c92ee4317a9affcb04a85c3b9d.png",
}

XIAOHONGSHU_EMOTE_SOURCES: Mapping[str, str] = dict(_XIAOHONGSHU_STATIC)
XIAOHONGSHU_PAG_PAGES: Mapping[str, str] = dict(_XIAOHONGSHU_PAG_PAGE)

# ---------------------------------------------------------------------------
# 酷安
# ---------------------------------------------------------------------------
#: 官方 CDN 前缀，文件名形如 ``coolapk_emotion_105_tnaikezui.png``。
COOLAPK_EMOTE_BASE = "https://static.coolapk.com/emoticons/v9/"
COOLAPK_EMOTE_MANIFEST = "coolapk_emote_files.json"


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class EmoteSources:
    """一个平台的表情来源表。"""

    static: Mapping[str, str]
    pag_pages: Mapping[str, str] = None  # type: ignore[assignment]
    base_url: str = ""

    def __post_init__(self) -> None:
        if self.pag_pages is None:
            object.__setattr__(self, "pag_pages", {})


def load_coolapk_sources(data_dir: Path) -> EmoteSources:
    files = load_json(data_dir / COOLAPK_EMOTE_MANIFEST)
    static = {
        str(name): f"{COOLAPK_EMOTE_BASE}{file_name}"
        for name, file_name in files.items()
        if isinstance(file_name, str)
        and file_name.endswith((".png", ".jpg", ".gif", ".webp"))
        and "/" not in file_name
    }
    return EmoteSources(static=static, base_url=COOLAPK_EMOTE_BASE)


def load_xiaohongshu_sources(data_dir: Path) -> EmoteSources:
    return EmoteSources(
        static=dict(_XIAOHONGSHU_STATIC),
        pag_pages=dict(_XIAOHONGSHU_PAG_PAGE),
    )


__all__ = [
    "COOLAPK_EMOTE_BASE",
    "COOLAPK_EMOTE_MANIFEST",
    "EmoteSources",
    "XIAOHONGSHU_EMOTE_SOURCES",
    "XIAOHONGSHU_PAG_BUNDLE_NAME",
    "XIAOHONGSHU_PAG_MANIFEST",
    "XIAOHONGSHU_PAG_PAGES",
    "load_coolapk_sources",
    "load_json",
    "load_xiaohongshu_sources",
]
