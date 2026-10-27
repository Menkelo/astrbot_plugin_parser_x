"""Tieba-specific portrait normalization; never accept a path or URL."""
import re


def emote_url(value):
    """Resolve a client emote asset id, independently of its optional label."""
    if not isinstance(value, str) or not re.fullmatch(r'image_emoticon[0-9]{1,3}', value):
        return ''
    return 'https://tb2.bdstatic.com/tb/editor/images/client/' + value + '.png'


def portrait_url(value):
    if not isinstance(value, str):
        return ""
    token = value.split("?", 1)[0]
    if (not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,511}", token)
            or ".." in token or "#" in value or "\\" in value):
        return ""
    return "https://himg.bdimg.com/sys/portrait/item/" + token
