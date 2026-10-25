"""Live-stream links are ignored on every platform, without any reply.

The plugin only understands finished posts, videos and notes. A live room has
no caption or media to deliver, so it must never produce a message: neither a
card, nor a "解析失败" tip, nor a download attempt. Two layers enforce that:

* ``is_live_url`` lets the message router drop the link before any parser runs
  (no reply, no network request);
* the same check guards ``BaseParser.parse`` and the post-redirect parsers, so a
  short link that resolves into a live room still ends silently.
"""

from __future__ import annotations

# Short-link hosts hide their target; a share card that carries one of these plus
# live wording is a live share (e.g. a Bilibili live mini-program card).
SHORT_LINK_HOSTS: tuple[str, ...] = (
    "b23.tv",
    "bili2233.cn",
    "v.douyin.com",
    "jx.douyin.com",
    "xhslink.com",
    "xhslink.cn",
    "v.kuaishou.com",
    "chenzhongtech.com",
)

# Hosts whose pages are live rooms on the supported platforms.
LIVE_HOSTS: tuple[str, ...] = (
    "live.bilibili.com",
    "live.douyin.com",
    "webcast.douyin.com",
    "live.kuaishou.com",
    "live.acfun.cn",
    "live.weibo.com",
    "live.xiaohongshu.com",
    "live.iqiyi.com",
)

# Path fragments that identify a live room (Weibo's ``/l/wblive/`` et al).
LIVE_PATH_MARKERS: tuple[str, ...] = (
    "/wblive/",
    "/live/",
    "/livestream/",
)

# Captions that may describe a live share. The broad list keeps the historic
# Kuaishou/Xiaohongshu heuristics working; only the strong phrases below are
# trusted to drop a short link before its target is known.
LIVE_TEXT_HINTS: tuple[str, ...] = (
    "正在直播",
    "直播中",
    "直播很精彩",
    "来看直播",
    "直接观看直播",
    "观看直播",
    "直播间",
    "进入直播间",
    "开播",
)

# Card titles that only appear on a live share.
LIVE_CARD_HINTS: tuple[str, ...] = (
    "直播间",
    "正在直播",
    "直播中",
    "开播提醒",
)

# Unambiguous live phrasing. A short link whose caption carries one of these is
# dropped without a request; ordinary captions that merely mention "直播间" are
# still parsed, and the resolved URL is checked again afterwards.
LIVE_SHARE_HINTS: tuple[str, ...] = (
    "正在直播",
    "直播很精彩",
    "来看直播",
    "直接观看直播",
    "观看直播",
    "进入直播间",
)


def url_host(url: str | None) -> str:
    """Lower-cased host of a URL, without user info, port or trailing dot."""
    low = (url or "").strip().lower()
    if not low:
        return ""
    if "//" in low:
        low = low.split("//", 1)[1]
    host = low.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def is_live_url(url: str | None) -> bool:
    """Whether a URL points at a live room rather than a post or video."""
    low = (url or "").strip().lower()
    if not low:
        return False
    host = url_host(low)
    if host and any(host == live or host.endswith("." + live) for live in LIVE_HOSTS):
        return True
    return any(marker in low for marker in LIVE_PATH_MARKERS)


def is_short_link(url: str | None) -> bool:
    """Whether a URL is a share short link whose target is unknown up front."""
    host = url_host(url)
    return bool(host) and any(
        host == short or host.endswith("." + short) for short in SHORT_LINK_HOSTS
    )


def looks_like_live_card(payload: str | None) -> bool:
    """Whether a share-card payload (raw JSON/XML) describes a live room.

    Only called for card segments: the wording is reliable there because the
    card carries the platform's own title, e.g. "⚡直播中⚡某某的直播间".
    """
    raw = payload or ""
    if not raw:
        return False
    low = raw.lower()
    if any(live in low for live in LIVE_HOSTS):
        return True
    return any(hint in raw for hint in LIVE_CARD_HINTS)


def looks_like_live_share(text: str | None) -> bool:
    """Whether a share caption may describe a live room (broad heuristic)."""
    lowered = (text or "").lower()
    if not lowered:
        return False
    if "livestream" in lowered:
        return True
    return any(hint in lowered for hint in LIVE_TEXT_HINTS)


def is_live_share_caption(text: str | None) -> bool:
    """Whether a caption is unambiguous enough to drop a short link unopened."""
    lowered = (text or "").lower()
    if not lowered:
        return False
    if "livestream" in lowered:
        return True
    return any(hint in lowered for hint in LIVE_SHARE_HINTS)


__all__ = [
    "LIVE_CARD_HINTS",
    "LIVE_HOSTS",
    "SHORT_LINK_HOSTS",
    "is_short_link",
    "looks_like_live_card",
    "url_host",
    "LIVE_PATH_MARKERS",
    "LIVE_SHARE_HINTS",
    "LIVE_TEXT_HINTS",
    "is_live_share_caption",
    "is_live_url",
    "looks_like_live_share",
]
