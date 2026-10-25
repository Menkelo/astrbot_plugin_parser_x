"""Conservative, offline recognition of known navigation (not work) URLs.

Unknown routes and opaque short-link tokens are deliberately left to parsers.
Only exact supported hosts and known navigation routes are denied; query values
are never scanned for navigation words.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


def is_nonwork_url(url: str | None) -> bool:
    """Whether a supported URL is a known home, profile, feed or search page."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    parts = path.lstrip("/").split("/")
    first = parts[0]
    query = parse_qs(parsed.query)

    if host in {"douyin.com", "www.douyin.com", "m.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}:
        # A work opened as a modal on a feed/profile remains a work.
        if any(value.isdigit() for value in query.get("modal_id", [])):
            return False
        return first in {"", "user", "search", "jingxuan", "follow", "discover", "recommend"} or parts[:2] == ["share", "user"]
    if host in {
        "kuaishou.com", "www.kuaishou.com", "m.gifshow.com",
        "chenzhongtech.com", "www.chenzhongtech.com",
        "m.chenzhongtech.com", "v.m.chenzhongtech.com",
    }:
        if any(
            re.fullmatch(r"[A-Za-z0-9_-]+", value)
            for key in ("photoId", "photo_id")
            for value in query.get(key, [])
        ):
            return False
        return first in {"", "new-reco", "search", "profile", "follow", "u"} or (
            first == "fw" and len(parts) > 1 and parts[1] in {"user", "profile", "search"}
        )
    if host in {"weibo.com", "www.weibo.com", "weibo.cn", "m.weibo.cn"}:
        if first == "u":
            parts = parts[1:]
            first = parts[0] if parts else ""
        return (
            first in {"", "home", "search", "mygroups", "hot"}
            or (first == "p" and len(parts) > 1 and parts[1].startswith("100505") and parts[1].isdigit())
            or (first.isdigit() and (len(parts) == 1 or parts[1] in {"follow", "fans"}))
        )
    if host == "space.bilibili.com":
        return True
    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "t.bilibili.com"}:
        return first in {"", "search", "account", "history"} or path in {"/opus", "/read/home"}
    if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        return first in {"", "user", "search_result", "search"} or path == "/explore"
    if host in {"xiaoheihe.cn", "www.xiaoheihe.cn"}:
        return not path or parts[:2] in (["app", "user"], ["app", "search"])
    if host in {"acfun.cn", "www.acfun.cn", "m.acfun.cn"}:
        return first in {"", "u", "search", "member"}
    return False
