from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..comment_canvas import (
    CommentAuthor,
    CommentBadge,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    CommentTheme,
    XIAOHEIHE_THEME,
)
from ..comment_filter import CommentFilter
from ..comment_settings import CommentFilterSettings
from ..data import ImageContent
from .social_comment_feed import SocialCommentFeedBase

# 小黑盒评论接口：一次请求就能拿到一级楼，楼中楼内嵌在 comment 数组里。
COMMENT_PATH = "/bbs/app/link/tree"

# 正文里的表情是 [cube_鳄鱼] 这种方括号 token，映射表由正文解析阶段带下来。
_EMOTE_TOKEN_RE = re.compile(r"\[([a-zA-Z]+)_([^\[\]\n]{1,32})\]")

# 评论里出现的 HTML 标签：``<a data-user-id=".." href="..">@某人</a>`` 这类
# @人链接，以及偶尔出现的 ``<br>``。只保留标签内的文字。
_AT_TAG_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _clean_comment_html(text: str) -> str:
    """剥掉评论正文里的 HTML，只留下人眼能看到的内容。

    ``<a ...>@某人</a>`` -> ``@某人``；其余标签直接删除。
    保留下来的 ``@某人`` 还要交给评论过滤器判断是否属于「纯 @ 无信息」。
    """
    if "<" not in text:
        return text
    text = _AT_TAG_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    return text


def xiaoheihe_comment_theme() -> CommentTheme:
    """小黑盒评论区主题。

    评论图配色已统一写在 ``comment_render_base._CSS`` 里，这里只保留
    平台名，供 ``_LAYOUT_CSS`` 查找字号覆盖。
    """
    return XIAOHEIHE_THEME


class XiaoHeiHeCommentFeed(SocialCommentFeedBase):
    """小黑盒社区帖子评论区。

    接口 ``/bbs/app/link/tree`` 返回的 ``result.comments`` 每项形如
    ``{"comment": [主楼, 楼中楼...]}``，主楼取第一项，第二条作为楼中楼展示。
    表情、头像、楼层信息都直接取自接口字段，不额外请求。
    """

    CACHE_VERSION = "xiaoheihe_comment_v2"
    PLATFORM_SLUG = "xiaoheihe"
    AVATAR_REFERER = "https://www.xiaoheihe.cn/"

    def __init__(self, parser, *, limit: int = 10):
        super().__init__(parser, limit=limit)
        self.comment_filter = CommentFilter(
            parser,
            CommentFilterSettings.from_config(getattr(parser, "config", {})),
            platform="小黑盒",
            headers=getattr(parser, "headers", {}),
            referer=self.AVATAR_REFERER,
        )
        self._emotes: dict[str, str] = {}

    # ---------------------------------------------------------------- 渲染
    async def render_images(
        self,
        document: Any,
        *,
        link_id: str,
    ) -> list[ImageContent]:
        """把评论文档渲染成一张图，供视频帖单独发送。"""
        if document is None:
            return []
        serialised = json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.CACHE_VERSION}|{serialised}".encode()
        ).hexdigest()[:12]
        out_path = self.cache_dir / f"xiaoheihe_comment_{link_id}_{digest}.jpg"
        if out_path.is_file() and out_path.stat().st_size > 0:
            return [ImageContent(out_path)]

        async def render() -> Path:
            await self.canvas.render(out_path, document)
            return out_path

        task = self.parser.task_factory(render) if hasattr(
            self.parser, "task_factory"
        ) else None
        if task is not None:
            with contextlib.suppress(Exception):
                await task
            return [ImageContent(out_path)]
        await render()
        return [ImageContent(out_path)]

    # ---------------------------------------------------------------- 抓取
    async def fetch_tree(self, link_id: str) -> dict[str, Any]:
        """拉一页评论树。接口风控需要登录态，失败返回空结构。"""
        try:
            resp = await self.parser.http_get(
                self.parser.build_comment_url(link_id, limit=self.limit),
                headers=self.parser.headers,
                retries=1,
            )
            data = resp.json() or {}
        except Exception:
            return {}
        if data.get("status") != "ok":
            return {}
        return data.get("result") or {}

    # ---------------------------------------------------------------- 转换
    def _rich_parts(self, text: str) -> list[CommentRichPart]:
        """把正文 token 拆成富文本片段，表情交给画布换图。

        评论正文里会夹杂 HTML：``@某人`` 是
        ``<a data-user-id="..." href="..." target="_blank">@某人</a>``。
        标签必须先剥掉只留 ``@某人`` 这段文字，否则会把整串属性当正文发出去
        （早期版本就是这样把 ``href`` 原样漏到群里的）。
        """
        text = _clean_comment_html(text)
        parts: list[CommentRichPart] = []
        cursor = 0
        for match in _EMOTE_TOKEN_RE.finditer(text):
            if match.start() > cursor:
                parts.append(
                    CommentRichPart(kind="text", text=text[cursor : match.start()])
                )
            url = self._emotes.get(match.group(0))
            if url:
                parts.append(
                    CommentRichPart(kind="emote", text=match.group(0), url=url)
                )
            else:
                parts.append(CommentRichPart(kind="text", text=match.group(0)))
            cursor = match.end()
        if cursor < len(text):
            parts.append(CommentRichPart(kind="text", text=text[cursor:]))
        return parts or [CommentRichPart(kind="text", text=text)]

    def _entry(self, raw: object, *, pinned: bool = False) -> CommentEntry | None:
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        user = raw.get("user") or {}
        nickname = str(user.get("username") or "").strip() or "匿名用户"
        avatar = str(user.get("avatar") or user.get("avartar") or "").strip()

        badges: list[CommentBadge] = []
        if raw.get("is_link_owner"):
            badges.append(
                CommentBadge(
                    text="作者",
                    foreground="#000000",
                    background="#ffffff",
                    border="#ffffff",
                )
            )
        level = (user.get("level_info") or {}).get("level")
        if isinstance(level, int) and level > 0:
            badges.append(CommentBadge(text=f"Lv{level}"))

        entry = CommentEntry(
            author=CommentAuthor(nickname=nickname, avatar=avatar, badges=badges),
            content=self._rich_parts(text),
            time_text=self.time_text(raw.get("create_at")),
            location=str(raw.get("ip_location") or "").strip(),
            like_text=self.count_text(raw.get("up")),
            reply_text="回复",
            # 置顶判定不按字段名直取（详见 _is_pinned_head）：is_top 实际是
            # 「热门排序命中」标记，一条正常帖子里能有十几条同时为 1。
            pinned=pinned,
        )
        return entry

    @staticmethod
    def _is_pinned_head(head: dict[str, Any], floors: list[dict[str, Any]]) -> bool:
        """判断评论列表的首条是不是平台置顶的那条。

        小黑盒的置顶信号有两套，且各自都有失效场景（实测）：

        - ``top_comment == 1``：最准，但 193973610 这类帖子整个字段都是 None；
        - ``floor_num`` 乱序：置顶条会被提到索引 0，而它的楼层号不是最小的，
          194561740 却恰好是正序（首条楼层就是全表最小），单看楼层会漏判。

        所以两个条件取「或」：任一命中即视为置顶。``is_top`` 不能用——它是
        热门排序标记，非视频帖里常见十几条同时为 1。
        """
        if not isinstance(head, dict):
            return False
        if head.get("top_comment") == 1:
            return True
        first_floor = head.get("floor_num")
        floors_num = [
            (floor or {}).get("comment", [{}])[0].get("floor_num")
            for floor in floors
            if (floor or {}).get("comment")
        ]
        floors_num = [n for n in floors_num if isinstance(n, int)]
        if isinstance(first_floor, int) and floors_num:
            return first_floor > min(floors_num)
        return False

    async def build_document(
        self,
        tree: dict[str, Any],
        *,
        work_title: str = "",
        cover: str | None = None,
        emotes: dict[str, str] | None = None,
    ) -> CommentDocument | None:
        """把 ``link/tree`` 的 result 转成画布认识的文档；没有可用评论时返回 None。"""
        self._emotes = dict(emotes or {})
        floors = tree.get("comments") or []
        entries: list[CommentEntry] = []
        pinned_head = self._is_pinned_head(
            ((floors[0] or {}).get("comment") or [{}])[0] if floors else {}, floors
        )
        for index, floor in enumerate(floors):
            block = (floor or {}).get("comment") or []
            if not block:
                continue
            main = self._entry(
                block[0], pinned=(index == 0 and pinned_head)
            )
            if main is None:
                continue
            if len(block) > 1:
                if nested := self._entry(block[1]):
                    main.first_reply = nested
            entries.append(main)
            if len(entries) >= self.limit:
                break
        if not entries:
            return None
        entries = await self.comment_filter.apply(entries, limit=self.limit)
        if not entries:
            return None
        return CommentDocument(
            theme=xiaoheihe_comment_theme(),
            work_title=work_title,
            cover=cover or "",
            total_text=self.count_text(tree.get("total_floor_num")),
            entries=entries,
        )


__all__ = ["COMMENT_PATH", "XiaoHeiHeCommentFeed", "xiaoheihe_comment_theme"]
