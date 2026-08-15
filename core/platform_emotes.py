from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

from astrbot.api import logger

MIYOUSHE_EMOTE_URL = "https://bbs-api.miyoushe.com/misc/api/emoticon_set"
XIAOHEIHE_EMOTE_URL = "https://api.xiaoheihe.cn/bbs/app/api/emojis/list"

_MIYOUSHE_TOKEN_RE = re.compile(r"_\([^()\n]{1,64}\)")
_XIAOHEIHE_TOKEN_RE = re.compile(r"\[[^\[\]\n]{1,48}\]")

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

_XIAOHEIHE_FALLBACK = {
    "cube_开心": "https://imgheybox.max-c.com/heybox/emoji/cube_21.png",
    "cube_喜欢": "https://imgheybox.max-c.com/heybox/emoji/cube_14.png",
    "cube_滑稽": "https://imgheybox.max-c.com/heybox/emoji/cube_34.png",
    "cube_doge": "https://imgheybox.max-c.com/heybox/emoji/cube_13.png",
    "cube_赞": "https://imgheybox.max-c.com/heybox/emoji/cube_41.png",
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
    if platform_key == "xiaoheihe":
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return value.strip()
    return value


def fallback_emote_map(platform_key: str) -> dict[str, str]:
    if platform_key == "miyoushe":
        return dict(_MIYOUSHE_FALLBACK)
    if platform_key == "xiaoheihe":
        return dict(_XIAOHEIHE_FALLBACK)
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
        else _XIAOHEIHE_TOKEN_RE
        if platform_key == "xiaoheihe"
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
        if platform_key == "miyoushe":
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
    "build_miyoushe_emote_map",
    "build_xiaoheihe_emote_map",
    "clean_emote_token",
    "contains_platform_emotes",
    "fallback_emote_map",
    "iter_emote_matches",
    "load_platform_emotes",
    "resolve_emote_url",
    "select_text_emotes",
]
