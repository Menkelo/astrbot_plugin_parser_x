"""正文话题标签清理。

各平台的话题写法不同（``#话题#``、``#话题[话题]#``、``#话题``），
卡片上统一不显示，避免正文被一串 # 号切碎。
"""

from __future__ import annotations

import re

# 小红书原生写法：#话题[话题]#
_BRACKET_TOPIC = re.compile(r"#[^#\r\n]{1,80}?\[话题\]#")


def _strip_topic_tokens(text: str) -> str:
    """逐字符扫描去掉话题片段。

    一次正则 ``#...#`` 或 ``#\\S+`` 都不可靠：前者会跨过分隔空格把连续的
    多个话题吃成一个匹配，后者又只删掉第一个。这里从每个 ``#`` 起一直
    吃到空白为止（``#话题#`` 会顺带吃掉结尾的 ``#``），逐个处理，
    无论话题粘在正文后面还是排成一行都能完整清掉。
    """

    if "#" not in text:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "#":
            out.append(text[index])
            index += 1
            continue
        # 跳过 # 之后连续的 #（如 ##），再吃到空白
        index += 1
        while index < length and not text[index].isspace() and text[index] != "#":
            index += 1
        while index < length and text[index] == "#":
            index += 1
        # 话题后面紧跟的空白吞掉一个，避免留下多余空格
        while index < length and text[index] in " \t":
            index += 1
        out.append(" ")
    return "".join(out)


def strip_topic_tags(text: str) -> str:
    """删掉正文里的话题标记，并把多余空白收拢。"""

    value = str(text or "")
    if "#" not in value:
        return value
    value = _BRACKET_TOPIC.sub(" ", value)
    value = _strip_topic_tokens(value)
    # 折叠因删除话题留下的连续空白（保留换行结构）
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


__all__ = ["strip_topic_tags"]
