"""Zhihu answer comments through the public v4 comments API.

Independent implementation; no signing code or login bypass is included.

Endpoint (observed live on 2026-10-26):
    GET https://www.zhihu.com/api/v4/answers/{answer_id}/comments
        ?limit=20&offset=0&order=normal
        &include=author,like_count,child_comments,parent_comment

The ``include`` list is what makes ``author.member`` appear; without it every
comment reports ``author: null`` and only a bare ``content`` string is present.
The configured Cookie is sent only to that exact host/path shape, matching the
parser's own allow-list.
"""
from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urlencode

from astrbot.api import logger

from ..comment_canvas import (
    CommentAuthor,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    CommentTheme,
)
from ..utils import normalize_image_url
from .social_comment_feed import SocialCommentFeedBase

ZHIHU_THEME = CommentTheme(platform_name="知乎")

_COMMENT_PATH = "/api/v4/answers/{answer_id}/comments"
_INCLUDE = "author,like_count,child_comments,parent_comment"
_PAGE_SIZE = 20
# Nested replies (楼中楼): only the first reply of a thread is shown — the card
# is a preview, not a thread browser. Roots that get a thread fetch stay small.
_THREAD_FETCH_LIMIT = 8
_THREAD_PAGE = 1

# ``[捂嘴]``, ``[为难]``, ``[暗中学习]`` — Zhihu ships its emote set as bracketed
# token text when the payload has no <img> for it. Bounded to a handful of
# characters and no newlines so ordinary prose in square brackets is untouched.
_BRACKET_TOKEN = re.compile(r"\[([^\[\]\n]{1,8})\]")


# Keyword fallback, longest fragment first. Zhihu keeps inventing emote names
# (``脑爆``, ``飙泪笑``, ``裂开``), so an unlisted name should still resolve to a
# sensible glyph instead of leaking the raw ``[名字]`` token into the card.
_EMOTE_KEYWORDS: tuple[tuple[str, str], ...] = tuple(sorted({
    "爆": "🤯", "炸": "🤯", "裂": "💔", "哭": "😭", "泪": "😭", "笑": "😂",
    "怒": "😡", "气": "😤", "囧": "😳", "惊": "😲", "吓": "😱", "怕": "😨",
    "汗": "😓", "尬": "😬", "困": "😪", "睡": "😴", "饿": "🤤", "吃": "😋",
    "赞": "👍", "强": "👍", "棒": "👍", "牛": "🐮", "萌": "🥰", "爱": "😍",
    "心": "❤️", "花": "🌸", "火": "🔥", "冷": "🥶", "热": "🥵", "晕": "😵",
    "吐": "🤮", "跪": "🧎", "拜": "🙏", "谢": "🙏", "手": "👋", "拳": "👊",
    "瓜": "🍉", "茶": "🍵", "酒": "🍺", "饭": "🍚", "钱": "💰", "星": "⭐",
    "月": "🌙", "日": "☀️", "云": "☁️", "雨": "🌧️", "雪": "❄️", "雷": "⚡",
    "狗": "🐶", "猫": "🐱", "猪": "🐷", "熊": "🐻", "虎": "🐯", "兔": "🐰",
    "doge": "🐶", "憨": "🤪", "傻": "🤪", "呆": "😐", "懵": "😵", "默": "😶",
    "思": "🤔", "疑": "🤨", "问": "❓", "静静": "🤫", "嘘": "🤫",
    "看": "👀", "盯": "👀", "瞧": "👀", "望": "👀", "笑眼": "😍",
    "么么": "😘", "亲": "😘", "摸": "🤗", "搓": "🤲", "推": "👐",
}.items(), key=lambda kv: -len(kv[0])))


def emote_glyph(name: str) -> str | None:
    """Map a Zhihu emote name such as ``捂脸`` to its Unicode emoji, if known."""
    key = name.strip()
    glyph = _ZHIHU_EMOJI.get(key)
    if glyph or not key:
        return glyph
    for fragment, guessed in _EMOTE_KEYWORDS:
        if fragment in key:
            return guessed
    return None

_BLOCK_TAGS = {"p", "div", "li", "blockquote", "section"}

# Zhihu's payload only ever carries the bracketed *name* of an emote, never an
# image URL (its image assets are not publicly addressable), so map the common
# set onto Unicode emoji — those render as real colour glyphs in every client.
_ZHIHU_EMOJI: dict[str, str] = {
    "doge": "🐶",
    "捂脸": "🤦",
    "脑爆": "🤯",
    "哇": "🤩",
    "飙泪笑": "😂",
    "飙泪": "😭",
    "哭死": "😭",
    "心塞": "😖",
    "崩溃": "🤯",
    "裂开": "💔",
    "离谱": "🙃",
    "嗨": "🤗",
    "happy": "😊",
    "亲亲": "😘",
    "抱抱": "🤗",
    "贴贴": "🫂",
    "摸头": "🤗",
    "捏": "🤏",
    "点赞": "👍",
    "赞一个": "👍",
    "喜欢": "😍",
    "爱了": "😍",
    "心心": "😍",
    "期待": "🤩",
    "星星眼": "🤩",
    "鼓掌叫好": "👏",
    "nice": "👍",
    "厉害": "👍",
    "佩服": "🫡",
    "敬礼": "🫡",
    "加油鸭": "💪",
    "冲鸭": "🚀",
    "yes": "✌️",
    "好笑": "😆",
    "笑死": "😂",
    "笑喷": "🤣",
    "滑稽脸": "😏",
    "傲慢": "😤",
    "不爽": "😒",
    "鄙视": "😒",
    "嫌弃": "😒",
    "嫌弃脸": "😒",
    "得意笑": "😎",
    "doge微笑": "🐶",
    "不简单": "🤨",
    "感动哭": "🥹",
    "哭唧唧": "🥺",
    "委屈巴巴": "🥺",
    "委屈脸": "🥺",
    "凉凉": "🥶",
    "发抖": "🥶",
    "冻": "🥶",
    "真香": "😋",
    "香": "😋",
    "吃": "🍽️",
    "喝茶": "🍵",
    "摸鱼": "🐟",
    "搬砖": "🧱",
    "上班": "💼",
    "下班": "🏃",
    "睡觉": "😴",
    "晚安": "🌙",
    "早": "☀️",
    "牛气": "🐮",
    "马": "🐴",
    "猴": "🐵",
    "鼠": "🐭",
    "鸟": "🐦",
    "狼": "🐺",
    "狐狸": "🦊",
    "熊": "🐻",
    "虎": "🐯",
    "狮子": "🦁",
    "章鱼": "🐙",
    "鲸": "🐳",
    "海豚": "🐬",
    "蝴蝶": "🦋",
    "蜜蜂": "🐝",
    "蚂蚁": "🐜",
    "恐龙": "🦕",
    "独角兽": "🦄",
    "龙": "🐲",
    "神": "🧙",
    "鬼": "👻",
    "天使": "😇",
    "恶魔": "😈",
    "外星人": "👽",
    "机器人": "🤖",
    "小丑": "🤡",
    "礼物盒": "🎁",
    "红包": "🧧",
    "金币": "🪙",
    "钱": "💰",
    "赚": "💰",
    "哭穷": "💸",
    "书本": "📚",
    "学习": "📚",
    "笔": "🖊️",
    "电脑": "💻",
    "手机": "📱",
    "相机": "📷",
    "音乐": "🎵",
    "麦克风": "🎤",
    "跳舞": "💃",
    "球": "⚽",
    "游戏": "🎮",
    "睡觉觉": "😴",
    "打脸": "🤦",
    "扶额": "🤦",
    "摊手": "🤷",
    "耸肩": "🤷",
    "鼓掌掌": "👏",
    "比耶": "✌️",
    "招手": "👋",
    "拜拜": "👋",
    "hi": "👋",
    "你好": "👋",
    "hug": "🤗",
    "加油哦": "💪",
    "不抬杠": "🤐",
    "抬杠": "😤",
    "杠精": "🤨",
    "笑哭": "😂",
    "大笑": "😄",
    "开心": "😊",
    "微笑": "🙂",
    "思考": "🤔",
    "捂嘴": "🤭",
    "调皮": "😜",
    "滑稽": "😏",
    "害羞": "☺️",
    "不好意思": "😅",
    "哭": "😢",
    "流泪": "😭",
    "委屈": "🥺",
    "可怜": "🥹",
    "衰": "😞",
    "失落": "😔",
    "无语": "😑",
    "白眼": "🙄",
    "尴尬": "😬",
    "汗": "😓",
    "汗颜": "😰",
    "衰笑": "😆",
    "惊喜": "🤩",
    "惊讶": "😲",
    "震惊": "😱",
    "害怕": "😨",
    "生气": "😠",
    "愤怒": "😡",
    "怒": "😤",
    "哼": "😤",
    "舔": "😋",
    "馋": "😋",
    "吃瓜": "🍉",
    "机智": "🤓",
    "酷": "😎",
    "酷炫": "😎",
    "得意": "😎",
    "赞": "👍",
    "赞同": "👍",
    "强": "👍",
    "踩": "👎",
    "弱": "👎",
    "鼓掌": "👏",
    "握爪": "🤝",
    "握手": "🤝",
    "抱拳": "🙏",
    "感谢": "🙏",
    "拜托": "🙏",
    "祈祷": "🙏",
    "合十": "🙏",
    "拜托了": "🙏",
    "比心": "🫶",
    "爱心": "❤️",
    "心": "❤️",
    "红心": "❤️",
    "心碎": "💔",
    "鲜花": "🌹",
    "玫瑰": "🌹",
    "花": "🌸",
    "火": "🔥",
    "热": "🥵",
    "冷": "🥶",
    "骷髅": "💀",
    "屎": "💩",
    "便便": "💩",
    "猪头": "🐷",
    "牛": "🐮",
    "狗": "🐶",
    "猫": "🐱",
    "熊猫": "🐼",
    "兔": "🐰",
    "蛇": "🐍",
    "鸡": "🐔",
    "蛙": "🐸",
    "螃蟹": "🦀",
    "鱼": "🐟",
    "月亮": "🌙",
    "太阳": "☀️",
    "星星": "⭐",
    "彩虹": "🌈",
    "闪电": "⚡",
    "炸弹": "💣",
    "烟花": "🎉",
    "庆祝": "🎉",
    "撒花": "🎉",
    "礼物": "🎁",
    "蛋糕": "🍰",
    "咖啡": "☕",
    "茶": "🍵",
    "啤酒": "🍺",
    "干杯": "🍻",
    "米": "🍚",
    "饭": "🍚",
    "西瓜": "🍉",
    "苹果": "🍎",
    "柠檬": "🍋",
    "doge_": "🐶",
    "暗中学习": "🤓",
    "暗中观察": "👀",
    "看": "👀",
    "围观": "👀",
    "微笑面对": "🙂",
    "你说": "🙃",
    "doge脸": "🐶",
    "为难": "😖",
    "难受": "😣",
    "痛苦": "😖",
    "哈哈": "😄",
    "嘿嘿": "😁",
    "耶": "✌️",
    "OK": "👌",
    "ok": "👌",
    "不行": "🙅",
    "拒绝": "🙅",
    "好的": "👌",
    "what": "🤨",
    "问号": "❓",
    "疑问": "❓",
    "感叹": "❗",
    "注意": "⚠️",
    "警告": "⚠️",
    "禁止": "🚫",
    "对": "✅",
    "错": "❌",
    "勾": "✅",
    "叉": "❌",
    "100": "💯",
    "满分": "💯",
    "冲": "🚀",
    "飞机": "✈️",
    "加油": "💪",
    "努力": "💪",
    "跪": "🧎",
    "磕头": "🙇",
    "鞠躬": "🙇",
    "睡": "😴",
    "困": "😪",
    "打哈欠": "🥱",
    "生病": "🤒",
    "口罩": "😷",
    "受伤": "🤕",
    "晕": "😵",
    "转圈": "🌀",
    "吐": "🤮",
    "呕吐": "🤮",
    "安静": "🤐",
    "闭嘴": "🤐",
    "嘘": "🤫",
    "秘密": "🤫",
    "装傻": "😳",
    "脸红": "😳",
    "惊呆": "😯",
    "无语凝噎": "🥲",
    "感动": "🥹",
    "坏笑": "😏",
    "偷笑": "🤭",
    "嘿嘿嘿": "😁",
    "哭笑不得": "😂",
    "飙泪": "😭",
    "大哭": "😭",
    "小声哭": "🥲",
    "嘤嘤嘤": "🥺",
    "可怜巴巴": "🥺",
    "拜托拜托": "🙏",
    "磕头了": "🙇",
}


class _ZhihuRichTextParser(HTMLParser):
    """Turn a Zhihu comment body (plain text or HTML) into rich parts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[CommentRichPart] = []
        self._highlight_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "br":
            self.parts.append(CommentRichPart("line-break"))
        elif tag in _BLOCK_TAGS:
            self.parts.append(CommentRichPart("line-break"))
        elif tag == "a":
            self._highlight_depth += 1
        elif tag == "img":
            url = normalize_image_url(attr_map.get("src")) or ""
            text = attr_map.get("alt") or attr_map.get("title") or ""
            if url:
                self.parts.append(CommentRichPart("emote", text=text, url=url))
            elif text:
                self.parts.append(CommentRichPart("emoji-text", text=text))

    def handle_endtag(self, tag: str):
        if tag == "a" and self._highlight_depth > 0:
            self._highlight_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(CommentRichPart("line-break"))

    def handle_data(self, data: str):
        if not data:
            return
        kind = "highlight" if self._highlight_depth else "text"
        if kind == "highlight":
            self.parts.append(CommentRichPart(kind, text=data))
            return
        self.parts.append(CommentRichPart(kind, text=data))

    def handle_comment(self, data: str):  # pragma: no cover - defensive
        pass


def _collapse_breaks(parts: list[CommentRichPart]) -> list[CommentRichPart]:
    """Trim redundant leading/trailing and doubled line breaks.

    ``<p>a</p><p>b</p>`` yields break a break break b break; consecutive breaks
    collapse to at most two so paragraph spacing survives without empty runs.
    """
    out: list[CommentRichPart] = []
    for part in parts:
        if part.kind == "line-break":
            if not out or out[-1].kind == "line-break" and len(out) == 1:
                continue
            if len(out) >= 2 and out[-1].kind == "line-break" and out[-2].kind == "line-break":
                continue
            out.append(part)
        elif part.text or part.url:
            out.append(part)
    while out and out[-1].kind == "line-break":
        out.pop()
    while out and out[0].kind == "line-break":
        out.pop(0)
    return out


def _promote_bracket_tokens(parts: list[CommentRichPart]) -> list[CommentRichPart]:
    """Mark ``[x]`` segments as ``emoji-text`` so they render as chips."""
    promoted: list[CommentRichPart] = []
    for part in parts:
        if part.kind != "text":
            promoted.append(part)
            continue
        text = part.text
        cursor = 0
        for match in _BRACKET_TOKEN.finditer(text):
            if match.start() > cursor:
                promoted.append(CommentRichPart("text", text=text[cursor:match.start()]))
            glyph = emote_glyph(match.group(1))
            if glyph:
                # A real emoji we can actually draw — keep it inline as text so
                # it sits on the baseline instead of in a chipped box.
                promoted.append(CommentRichPart("text", text=glyph))
            else:
                promoted.append(CommentRichPart("emoji-text", text=match.group(0)))
            cursor = match.end()
        if cursor < len(text):
            tail = text[cursor:]
            if tail:
                promoted.append(CommentRichPart("text", text=tail))
    return [p for p in promoted if p.kind != "text" or p.text]


class ZhihuCommentFeed(SocialCommentFeedBase):
    CACHE_VERSION = "zhihu_comment_v1"
    PLATFORM_SLUG = "zhihu"
    AVATAR_REFERER = "https://www.zhihu.com/"

    @staticmethod
    def comment_url(answer_id: str) -> str:
        return "https://www.zhihu.com" + _COMMENT_PATH.format(answer_id=answer_id)

    async def fetch_page(self, answer_id: str, *, offset: int = 0) -> dict:
        """One bounded page. Raises ParseException from the parser transport."""
        params = {
            "limit": _PAGE_SIZE,
            "offset": max(0, int(offset)),
            "order": "normal",
            "include": _INCLUDE,
        }
        url = self.comment_url(answer_id) + "?" + urlencode(params)
        response = await self.parser.http_get(
            url, headers=self.parser._zhihu_headers(url),
            timeout=10, retries=1, allow_redirects=False,
        )
        if response.status_code != 200:
            return {}
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _member(item: dict) -> dict:
        """author.member holds the profile; older payloads inline the fields."""
        author = item.get("author")
        if not isinstance(author, dict):
            return {}
        member = author.get("member")
        return member if isinstance(member, dict) else author

    @staticmethod
    def _rich_parts(html: str) -> list[CommentRichPart]:
        """Zhihu comment bodies mix plain text and HTML.

        Depending on the endpoint generation the same field arrives either as
        bare text with ``[捂嘴]``-style emote tokens, or as markup such as
        ``<p>...</p>``, ``<br>``, ``<a>`` and ``<img>`` emote stickers. Parsing
        through HTMLParser handles both shapes: entities are decoded, ``<p>``
        and ``<br>`` become line breaks, ``<a>`` becomes a highlight, and
        ``<img>`` becomes an emote (or its ``alt`` text when no src is set).
        Bracket tokens are additionally promoted to ``emoji-text`` so the
        renderer can style them instead of leaving raw ``[捂嘴]`` in the body.
        """
        parser = _ZhihuRichTextParser()
        parser.feed(str(html or ""))
        parser.close()
        return _promote_bracket_tokens(_collapse_breaks(parser.parts))

    def adapt_comment(self, item: dict, *, owner_id: str = "") -> CommentEntry | None:
        if not isinstance(item, dict):
            return None
        if item.get("is_delete") or item.get("reviewing") or item.get("collapsed"):
            return None
        content = str(item.get("content") or "").strip()
        if not content:
            return None
        member = self._member(item)
        nickname = str(member.get("name") or "").strip() or "知乎用户"
        avatar = str(member.get("avatar_url") or "").strip()
        author = CommentAuthor(
            nickname=nickname,
            avatar=avatar if avatar.startswith("https://") else "",
        )
        member_id = str(member.get("id") or "")
        author.is_up = bool(owner_id) and member_id == str(owner_id)
        entry = CommentEntry(
            author=author,
            content=self._rich_parts(content),
            time_text=self.time_text(item.get("created_time")),
            like_text=self.count_text(item.get("vote_count")),
            # Zhihu's v4 comment API exposes no usable pin flag: ``top`` is
            # always False (0 of 66 sampled comments) and ``featured`` marks a
            # *hot* comment, not a pinned one. Treating featured as pinned put a
            # 置顶 badge on ordinary top-liked comments, so neither is used.
            pinned=False,
            comment_id=str(item.get("id") or ""),
        )
        # Zhihu returns at most one nested reply per root in this payload.
        children = item.get("child_comments") or []
        if isinstance(children, list) and children:
            child = self.adapt_comment(children[0], owner_id=owner_id)
            if child is not None:
                entry.first_reply = child
        return entry

    async def fetch_child_comments(
        self, comment_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """One page of 楼中楼 replies for a root comment.

        ``/api/v4/comments/{id}/child_comments`` returns up to 20 rows per page
        plus ``paging.totals``. Bounded on purpose: the canvas shows a handful
        of nested replies, not an unbounded thread walk.
        """
        if not comment_id:
            return []
        url = (
            f"https://www.zhihu.com/api/v4/comments/{comment_id}/child_comments?"
            + urlencode(
                {
                    "limit": max(1, min(int(limit), 20)),
                    "offset": max(0, int(offset)),
                    "include": "author,like_count,parent_comment",
                }
            )
        )
        try:
            response = await self.parser.http_get(
                url, headers=self.parser._zhihu_headers(url),
                timeout=10, retries=1, allow_redirects=False,
            )
        except Exception:  # noqa: BLE001 - nested replies are optional garnish
            return []
        if response.status_code != 200:
            return []
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    async def build_document(
        self,
        answer_id: str,
        *,
        work_title: str = "",
        cover: str | None = None,
        owner_id: str = "",
    ) -> CommentDocument | None:
        if not answer_id:
            return None
        payload = await self.fetch_page(answer_id)
        raw_items = payload.get("data")
        if not isinstance(raw_items, list) or not raw_items:
            logger.debug(
                "[评论区][知乎] stage=fetch result=empty answer=%s", answer_id
            )
            return None

        candidates = [
            entry for item in raw_items
            if (entry := self.adapt_comment(item, owner_id=owner_id)) is not None
        ]
        # A pinned comment must survive the limit and stay first. Keep the
        # pin out of the filter's window, then re-attach it at the head.
        entries = await self.comment_filter.apply_keeping_pinned(
            candidates, limit=self.limit,
        )
        if not entries:
            return None

        await self._attach_thread_replies(raw_items, entries, owner_id=owner_id)

        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        try:
            total = int(paging.get("totals") or 0)
        except (TypeError, ValueError):
            total = 0
        if total <= 0:
            total = len(entries)

        document = CommentDocument(
            theme=ZHIHU_THEME,
            work_title=work_title or "知乎回答",
            cover=cover or "",
            total_text=f"{self.count_text(total)} 条评论",
            entries=entries,
        )
        await self._embed_avatars(document.entries)
        return document

    async def _attach_thread_replies(
        self, raw_items: list, entries: list[CommentEntry], *, owner_id: str = ""
    ) -> None:
        """Fill ``replies`` (楼中楼) for the roots that are about to be shown.

        The list endpoint returns ``child_comments: None`` even when a thread
        has dozens of replies, so the per-comment endpoint is the only way to
        see them. Fetched concurrently and bounded by ``_THREAD_FETCH_LIMIT``
        so a busy answer cannot turn one card render into hundreds of calls;
        failures are silent because replies are an enhancement, not the feed.
        """
        roots = [e for e in entries[:_THREAD_FETCH_LIMIT] if e.comment_id]
        async def fill(entry: CommentEntry) -> None:
            rows = await self.fetch_child_comments(entry.comment_id, limit=_THREAD_PAGE)
            replies: list[CommentEntry] = []
            for row in rows:
                child = self.adapt_comment(row, owner_id=owner_id)
                if child is not None:
                    replies.append(child)
            if not replies:
                return
            entry.replies = replies
            # ``first_reply`` stays the first row so older render paths show one.
            if entry.first_reply is None:
                entry.first_reply = replies[0]
        await asyncio.gather(*(fill(entry) for entry in roots))


__all__ = ["ZHIHU_THEME", "ZhihuCommentFeed"]
