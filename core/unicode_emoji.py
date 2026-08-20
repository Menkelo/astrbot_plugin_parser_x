from __future__ import annotations

from collections.abc import Callable, Iterator

TWEMOJI_SVG_BASE = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/svg/"

_ZWJ = 0x200D
_KEYCAP = 0x20E3
_TEXT_VARIATION = 0xFE0E
_EMOJI_VARIATION = 0xFE0F


def _in_range(value: int, start: int, end: int) -> bool:
    return start <= value <= end


def _is_regional_indicator(value: int) -> bool:
    return _in_range(value, 0x1F1E6, 0x1F1FF)


def _is_modifier(value: int) -> bool:
    return _in_range(value, 0x1F3FB, 0x1F3FF)


def _is_tag(value: int) -> bool:
    return _in_range(value, 0xE0020, 0xE007E)


def _is_emoji_base(value: int) -> bool:
    if _in_range(value, 0x1F000, 0x1FAFF):
        return True
    if _in_range(value, 0x2600, 0x27BF):
        return True
    return value in {
        0x00A9,
        0x00AE,
        0x203C,
        0x2049,
        0x2122,
        0x2139,
        0x2194,
        0x2195,
        0x2196,
        0x2197,
        0x2198,
        0x2199,
        0x21A9,
        0x21AA,
        0x231A,
        0x231B,
        0x2328,
        0x23CF,
        0x23E9,
        0x23EA,
        0x23EB,
        0x23EC,
        0x23ED,
        0x23EE,
        0x23EF,
        0x23F0,
        0x23F1,
        0x23F2,
        0x23F3,
        0x23F8,
        0x23F9,
        0x23FA,
        0x24C2,
        0x25AA,
        0x25AB,
        0x25B6,
        0x25C0,
        0x25FB,
        0x25FC,
        0x25FD,
        0x25FE,
        0x2934,
        0x2935,
        0x2B05,
        0x2B06,
        0x2B07,
        0x2B1B,
        0x2B1C,
        0x2B50,
        0x2B55,
        0x3030,
        0x303D,
        0x3297,
        0x3299,
    }


def _consume_base(text: str, index: int) -> int:
    if index >= len(text) or not _is_emoji_base(ord(text[index])):
        return index
    index += 1
    if index < len(text) and ord(text[index]) in {
        _TEXT_VARIATION,
        _EMOJI_VARIATION,
    }:
        index += 1
    if index < len(text) and _is_modifier(ord(text[index])):
        index += 1
    return index


def _consume_cluster(text: str, index: int) -> int:
    if index >= len(text):
        return index

    if text[index] in "#*0123456789":
        cursor = index + 1
        if cursor < len(text) and ord(text[cursor]) == _EMOJI_VARIATION:
            cursor += 1
        if cursor < len(text) and ord(text[cursor]) == _KEYCAP:
            return cursor + 1
        return index

    value = ord(text[index])
    if not _is_emoji_base(value):
        return index

    cursor = _consume_base(text, index)
    if _is_regional_indicator(value):
        if cursor < len(text) and _is_regional_indicator(ord(text[cursor])):
            cursor += 1
        return cursor

    while cursor < len(text) and _is_tag(ord(text[cursor])):
        cursor += 1
    if cursor < len(text) and ord(text[cursor]) == 0xE007F:
        cursor += 1

    while cursor + 1 < len(text) and ord(text[cursor]) == _ZWJ:
        next_cursor = _consume_base(text, cursor + 1)
        if next_cursor == cursor + 1:
            break
        cursor = next_cursor
        while cursor < len(text) and _is_tag(ord(text[cursor])):
            cursor += 1
        if cursor < len(text) and ord(text[cursor]) == 0xE007F:
            cursor += 1
    return cursor


def twemoji_codepoint(cluster: str) -> str:
    values = [ord(character) for character in cluster]
    if _KEYCAP in values or _ZWJ not in values:
        values = [
            value
            for value in values
            if value not in {_TEXT_VARIATION, _EMOJI_VARIATION}
        ]
    return "-".join(f"{value:x}" for value in values)


def twemoji_url(cluster: str) -> str:
    return f"{TWEMOJI_SVG_BASE}{twemoji_codepoint(cluster)}.svg"


def iter_unicode_emoji(text: str) -> Iterator[tuple[int, int, str, str]]:
    index = 0
    while index < len(text or ""):
        end = _consume_cluster(text, index)
        if end > index:
            cluster = text[index:end]
            yield index, end, cluster, twemoji_url(cluster)
            index = end
        else:
            index += 1


def render_unicode_emoji_html(
    text: str,
    *,
    escape_text: Callable[[object], str],
    escape_url: Callable[[object], str],
    class_name: str = "unicode-emoji",
) -> str:
    parts: list[str] = []
    last = 0
    for start, end, cluster, url in iter_unicode_emoji(text or ""):
        parts.append(escape_text(text[last:start]))
        parts.append(
            f'<img class="{class_name}" src="{escape_url(url)}" '
            f'alt="{escape_url(cluster)}" '
            'onerror="this.replaceWith(document.createTextNode(this.alt))">'
        )
        last = end
    parts.append(escape_text(text[last:]))
    return "".join(parts)


__all__ = [
    "TWEMOJI_SVG_BASE",
    "iter_unicode_emoji",
    "render_unicode_emoji_html",
    "twemoji_codepoint",
    "twemoji_url",
]
