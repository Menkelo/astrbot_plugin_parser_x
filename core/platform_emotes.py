from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

from astrbot.api import logger

BILIBILI_EMOTE_URL = "https://api.bilibili.com/x/emote/package"
BILIBILI_EMOTE_PANEL_URL = "https://api.bilibili.com/x/emote/user/panel/web"
MIYOUSHE_EMOTE_URL = "https://bbs-api.miyoushe.com/misc/api/emoticon_set"
XIAOHEIHE_EMOTE_URL = "https://api.xiaoheihe.cn/bbs/app/api/emojis/list"
XIAOHONGSHU_EMOTE_URL = "https://edith.xiaohongshu.com/api/im/redmoji/detail"

_MIYOUSHE_TOKEN_RE = re.compile(r"_\([^()\n]{1,64}\)")
_SQUARE_TOKEN_RE = re.compile(r"\[[^\[\]\n]{1,64}\]")
_SQUARE_TOKEN_PLATFORMS = {
    "bilibili",
    "douyin",
    "weibo",
    "xiaoheihe",
    "xiaohongshu",
}

# The official catalog APIs are the primary source. These small fallbacks keep the
# most common expressions usable during a transient API failure.
_MIYOUSHE_FALLBACK = {
    "米游姬-期待": (
        "https://img-static.mihoyo.com/communityweb/upload/"
        "6adaac5ed9b16311259d3bbb6c108125.png"
    ),
    "米游姬-吃瓜": (
        "https://img-static.mihoyo.com/communityweb/upload/"
        "613a2b262af0319edde21587b88a9c6e.png"
    ),
    "米游兔-加油": (
        "https://upload-bbs.miyoushe.com/upload/2023/01/18/"
        "5857b8a3d4023bd05954225b0d578845_8473504187038159665.png"
    ),
}

_BILIBILI_FALLBACK = {
    "[doge]": (
        "https://i0.hdslb.com/bfs/emote/3087d273a78ccaff4bb1e9972e2ba2a7583c9f11.png"
    ),
    "[笑哭]": (
        "https://i0.hdslb.com/bfs/emote/c3043ba94babf824dea03ce500d0e73763bf4f40.png"
    ),
    "[吃瓜]": (
        "https://i0.hdslb.com/bfs/emote/4191ce3c44c2b3df8fd97c33f85d3ab15f4f3c84.png"
    ),
    "[滑稽]": (
        "https://i0.hdslb.com/bfs/emote/d15121545a99ac46774f1f4465b895fe2d1411c3.png"
    ),
    "[点赞]": (
        "https://i0.hdslb.com/bfs/emote/1a67265993913f4c35d15a6028a30724e83e7d35.png"
    ),
}

_XIAOHEIHE_FALLBACK = {
    "cube_开心": "https://imgheybox.max-c.com/heybox/emoji/cube_21.png",
    "cube_喜欢": "https://imgheybox.max-c.com/heybox/emoji/cube_14.png",
    "cube_滑稽": "https://imgheybox.max-c.com/heybox/emoji/cube_34.png",
    "cube_doge": "https://imgheybox.max-c.com/heybox/emoji/cube_13.png",
    "cube_赞": "https://imgheybox.max-c.com/heybox/emoji/cube_41.png",
}

_XIAOHONGSHU_FALLBACK = {
    "[微笑R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/"
        "9366d16631e3e208689cbc95eefb7cfb0901001e.png"
    ),
    "[生气R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/"
        "91515ae9718d8cce4f8de909683011b538d35327.png"
    ),
    "[哭惹R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/"
        "14b005f7afd5f7c88620478b610bf1de90c4ceab.png"
    ),
    "[斜眼R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/"
        "6062be312a922da7998f99fb773e06cea0a640df.png"
    ),
    "[doge]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/"
        "b7c0498189d449e8f22946be494d6bad48eda5ab.png"
    ),
}


def _normalise_miyoushe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[\s_—–－]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


def clean_emote_token(platform_key: str, token: str) -> str:
    value = str(token or "").strip()
    if platform_key == "miyoushe":
        if value.startswith("_(") and value.endswith(")"):
            value = value[2:-1]
        return _normalise_miyoushe_name(value)
    if platform_key in _SQUARE_TOKEN_PLATFORMS:
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return value.strip()
    return value


def fallback_emote_map(platform_key: str) -> dict[str, str]:
    if platform_key == "bilibili":
        return dict(_BILIBILI_FALLBACK)
    if platform_key == "miyoushe":
        return dict(_MIYOUSHE_FALLBACK)
    if platform_key == "xiaoheihe":
        return dict(_XIAOHEIHE_FALLBACK)
    if platform_key == "xiaohongshu":
        return dict(_XIAOHONGSHU_FALLBACK)
    return {}


def _register(
    output: dict[str, str],
    platform_key: str,
    name: object,
    url: object,
    *,
    overwrite: bool = True,
) -> None:
    name_text = str(name or "").strip()
    url_text = str(url or "").strip()
    if not name_text or not url_text.startswith(("http://", "https://")):
        return
    keys = {name_text, clean_emote_token(platform_key, name_text)}
    for key in keys:
        if not key:
            continue
        if overwrite:
            output[key] = url_text
        else:
            output.setdefault(key, url_text)


def build_miyoushe_emote_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    output: dict[str, str] = {}
    for group in data.get("list") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("list") or []:
            if not isinstance(item, dict):
                continue
            _register(output, "miyoushe", item.get("name"), item.get("icon"))
    return output


def build_bilibili_emote_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    output: dict[str, str] = {}
    for package in data.get("packages") or []:
        if not isinstance(package, dict):
            continue
        for item in package.get("emote") or []:
            if not isinstance(item, dict):
                continue
            _register(
                output,
                "bilibili",
                item.get("text") or item.get("name"),
                item.get("url") or item.get("gif_url"),
            )
    return output


def build_xiaoheihe_emote_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}

    output: dict[str, str] = {}
    for group in result.get("emoji_groups") or []:
        if not isinstance(group, dict):
            continue
        group_code = str(group.get("group_code") or "").strip()
        for item in group.get("emojis") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("name") or "").strip()
            image = item.get("img") or item.get("url")
            if group_code and code:
                _register(output, "xiaoheihe", f"{group_code}_{code}", image)
            # Code-only aliases are useful for old payloads, but do not replace an
            # earlier group when two packs use the same display name.
            _register(
                output,
                "xiaoheihe",
                code,
                image,
                overwrite=False,
            )
    return output


def build_xiaohongshu_emote_map(payload: object) -> dict[str, str]:
    """Build a token -> image map from Xiaohongshu's redmoji catalog.

    The endpoint has changed its envelope a few times.  Current responses put
    ``tabs[].collection[].emoji[]`` below ``data.emoji``; accepting the direct
    ``emoji``/``tabs`` forms as well keeps old cached fixtures and future minor
    envelope changes compatible.
    """
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    emoji = data.get("emoji") if isinstance(data, dict) else None
    if not isinstance(emoji, dict):
        emoji = data if isinstance(data, dict) else {}
    tabs = emoji.get("tabs") if isinstance(emoji, dict) else None
    if not isinstance(tabs, list):
        tabs = data.get("tabs") if isinstance(data, dict) else []

    output: dict[str, str] = {}
    for tab in tabs or []:
        if not isinstance(tab, dict):
            continue
        collections = tab.get("collection") or tab.get("collections") or []
        if isinstance(collections, dict):
            collections = [collections]
        for collection in collections:
            if not isinstance(collection, dict):
                continue
            items = collection.get("emoji") or collection.get("emojis") or []
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = (
                    item.get("image_name")
                    or item.get("imageName")
                    or item.get("display_name")
                    or item.get("name")
                    or item.get("text")
                )
                image = (
                    item.get("image")
                    or item.get("url")
                    or item.get("src")
                    or item.get("image_url")
                )
                _register(output, "xiaohongshu", name, image)
    return output


def resolve_emote_url(
    platform_key: str,
    token: str,
    emotes: Mapping[str, str] | None,
) -> str:
    catalog = emotes or {}
    raw = str(token or "").strip()
    clean = clean_emote_token(platform_key, raw)
    for key in (raw, clean):
        value = str(catalog.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def iter_emote_matches(
    text: str,
    platform_key: str,
    emotes: Mapping[str, str] | None = None,
) -> Iterator[tuple[int, int, str, str]]:
    pattern = (
        _MIYOUSHE_TOKEN_RE
        if platform_key == "miyoushe"
        else _SQUARE_TOKEN_RE
        if platform_key in _SQUARE_TOKEN_PLATFORMS
        else None
    )
    if pattern is None:
        return
    for matched in pattern.finditer(text or ""):
        token = matched.group(0)
        yield (
            matched.start(),
            matched.end(),
            token,
            resolve_emote_url(
                platform_key,
                token,
                emotes,
            ),
        )


def contains_platform_emotes(text: str, platform_key: str) -> bool:
    return any(iter_emote_matches(text, platform_key))


def select_text_emotes(
    text: str,
    platform_key: str,
    catalog: Mapping[str, str] | None,
) -> dict[str, str]:
    selected: dict[str, str] = {}
    for _, _, token, url in iter_emote_matches(text, platform_key, catalog):
        if url:
            selected[token] = url
            selected[clean_emote_token(platform_key, token)] = url
    return selected


async def load_platform_emotes(
    parser: Any,
    platform_key: str,
    *,
    gids: int | str = 2,
) -> dict[str, str]:
    cache_attr = f"_parser_x_{platform_key}_emote_map"
    deadline_attr = f"{cache_attr}_deadline"
    now = time.monotonic()
    cached = getattr(parser, cache_attr, None)
    deadline = float(getattr(parser, deadline_attr, 0.0) or 0.0)
    if isinstance(cached, dict) and cached and now < deadline:
        return cached

    output = fallback_emote_map(platform_key)
    http_get = getattr(parser, "http_get", None)
    if not callable(http_get):
        setattr(parser, cache_attr, output)
        setattr(parser, deadline_attr, now + 10 * 60)
        return output

    try:
        if platform_key == "bilibili":
            request_headers = dict(getattr(parser, "headers", None) or {})
            bili_cookie = str(getattr(parser, "bili_ck", "") or "").strip()
            if bili_cookie:
                request_headers["Cookie"] = bili_cookie
            response = await http_get(
                BILIBILI_EMOTE_URL,
                params={"business": "reply", "ids": "1"},
                headers=request_headers,
                timeout=8,
                retries=1,
            )
            parsed = build_bilibili_emote_map(response.json())
            if bili_cookie:
                try:
                    panel_response = await http_get(
                        BILIBILI_EMOTE_PANEL_URL,
                        params={"business": "reply"},
                        headers=request_headers,
                        timeout=8,
                        retries=1,
                    )
                    panel_payload = panel_response.json()
                    parsed.update(build_bilibili_emote_map(panel_payload))
                    packages = (
                        (panel_payload.get("data") or {}).get("packages") or []
                        if isinstance(panel_payload, dict)
                        else []
                    )
                    package_ids = [
                        str(package.get("id"))
                        for package in packages
                        if isinstance(package, dict)
                        and package.get("id") not in (None, "", 1, "1")
                    ]
                    if package_ids:
                        owned_response = await http_get(
                            BILIBILI_EMOTE_URL,
                            params={
                                "business": "reply",
                                "ids": ",".join(dict.fromkeys(package_ids)),
                            },
                            headers=request_headers,
                            timeout=8,
                            retries=1,
                        )
                        parsed.update(build_bilibili_emote_map(owned_response.json()))
                except Exception as exc:
                    logger.debug(f"[bilibili] 用户表情包读取失败，保留公共表情: {exc}")
        elif platform_key == "miyoushe":
            response = await http_get(
                MIYOUSHE_EMOTE_URL,
                params={"gids": str(gids)},
                headers=getattr(parser, "headers", None),
                timeout=8,
                retries=1,
            )
            parsed = build_miyoushe_emote_map(response.json())
        elif platform_key == "xiaoheihe":
            response = await http_get(
                XIAOHEIHE_EMOTE_URL,
                params={
                    "web_version": "2.5",
                    "x_app": "heybox_website",
                    "_time": int(time.time()),
                },
                headers=getattr(parser, "headers", None),
                timeout=8,
                retries=1,
            )
            parsed = build_xiaoheihe_emote_map(response.json())
        elif platform_key == "xiaohongshu":
            response = await http_get(
                XIAOHONGSHU_EMOTE_URL,
                headers=getattr(parser, "headers", None),
                timeout=8,
                retries=1,
            )
            parsed = build_xiaohongshu_emote_map(response.json())
        else:
            parsed = {}

        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        output.update(parsed)
        ttl = 6 * 60 * 60 if parsed else 10 * 60
    except Exception as exc:
        logger.debug(f"[{platform_key}] 表情目录读取失败，使用内置兜底: {exc}")
        ttl = 10 * 60

    setattr(parser, cache_attr, output)
    setattr(parser, deadline_attr, now + ttl)
    return output


__all__ = [
    "build_bilibili_emote_map",
    "build_miyoushe_emote_map",
    "build_xiaoheihe_emote_map",
    "build_xiaohongshu_emote_map",
    "clean_emote_token",
    "contains_platform_emotes",
    "fallback_emote_map",
    "iter_emote_matches",
    "load_platform_emotes",
    "resolve_emote_url",
    "select_text_emotes",
]
