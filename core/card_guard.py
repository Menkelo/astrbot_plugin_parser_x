"""Non-standard message components that must never trigger the plugin.

Groups receive a lot of traffic the plugin cannot parse: merged forwards, poke,
dice, location cards, music cards, link cards and unknown components. On those
messages the plugin must stay completely silent — no reply, no parse, and above
all no network request.

Share cards (JSON/XML mini-program cards) are *not* dropped wholesale: a card
that carries a supported post URL (a Bilibili video share, for instance) still
parses. A card without a usable URL is dropped silently here.
"""

from __future__ import annotations

# Label shown in the log line that explains why a message was dropped.
IGNORED_COMPONENTS: dict[str, str] = {
    "Forward": "合并转发",
    "Nodes": "合并转发",
    "Node": "转发节点",
    "Poke": "戳一戳",
    "Dice": "骰子",
    "RPS": "猜拳",
    "Rps": "猜拳",
    "Shake": "窗口抖动",
    "Music": "音乐卡片",
    "Share": "链接卡片",
    "Location": "位置卡片",
    "Unknown": "未知组件",
}

# Card payload components whose embedded URL may still be a parseable post.
CARD_COMPONENTS: frozenset[str] = frozenset({"Json", "Xml"})

# Components that carry the message text the plugin reads.
TEXT_COMPONENTS: frozenset[str] = frozenset({"Plain"})


def component_name(segment: object) -> str:
    """Component class name, tolerating stubs that only expose ``type``."""
    name = type(segment).__name__
    if name and name != "object":
        return name
    value = getattr(getattr(segment, "type", None), "name", None)
    return str(value or "")


def is_card_segment(segment: object) -> bool:
    """Whether a segment is a JSON/XML share card."""
    return component_name(segment) in CARD_COMPONENTS


def ignored_component_reason(chain: list | tuple) -> str:
    """Reason to drop a message because of a non-standard component.

    Returns an empty string when the message can be handled normally. Cards are
    deliberately excluded: the caller decides whether their URL is usable.
    """
    for segment in chain or ():
        name = component_name(segment)
        if name in CARD_COMPONENTS:
            continue
        label = IGNORED_COMPONENTS.get(name)
        if label:
            return label
    return ""


__all__ = [
    "CARD_COMPONENTS",
    "IGNORED_COMPONENTS",
    "TEXT_COMPONENTS",
    "component_name",
    "ignored_component_reason",
    "is_card_segment",
]
