from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from typing import Any

from astrbot.api import logger

BILIBILI_EMOTE_URL = "https://api.bilibili.com/x/emote/package"
BILIBILI_EMOTE_PANEL_URL = "https://api.bilibili.com/x/emote/user/panel/web"

_SQUARE_TOKEN_RE = re.compile(r"\[[^\[\]\n]{1,64}\]")
_SQUARE_TOKEN_PLATFORMS = {
    "bilibili",
    "douyin",
    "weibo",
}

# The official catalog APIs are the primary source. These small fallbacks keep the
# most common expressions usable during a transient API failure.

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


def clean_emote_token(platform_key: str, token: str) -> str:
    value = str(token or "").strip()
    if platform_key in _SQUARE_TOKEN_PLATFORMS:
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return value.strip()
    return value


def fallback_emote_map(platform_key: str) -> dict[str, str]:
    if platform_key == "bilibili":
        return dict(_BILIBILI_FALLBACK)
    return {}


def _register(
    output: dict[str, str],
    platform_key: str,
    name: object,
    url: object,
) -> None:
    name_text = str(name or "").strip()
    url_text = str(url or "").strip()
    if not name_text or not url_text.startswith(("http://", "https://")):
        return
    keys = {name_text, clean_emote_token(platform_key, name_text)}
    for key in keys:
        if not key:
            continue
        output[key] = url_text


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
    pattern = _SQUARE_TOKEN_RE if platform_key in _SQUARE_TOKEN_PLATFORMS else None
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
    if platform_key != "bilibili" or not callable(http_get):
        setattr(parser, cache_attr, output)
        setattr(parser, deadline_attr, now + 10 * 60)
        return output

    try:
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
    "clean_emote_token",
    "contains_platform_emotes",
    "fallback_emote_map",
    "iter_emote_matches",
    "load_platform_emotes",
    "resolve_emote_url",
    "select_text_emotes",
]
