"""Unified NGA comments from the already-fetched anonymous page-one response."""
from __future__ import annotations

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from ..platform_emotes import fallback_emote_map, iter_emote_matches
from .social_comment_feed import SocialCommentFeedBase


def _rich_parts(text: str):
    """把 BBCode 文本切成 文本/表情 片段，表情 token 保留给渲染层换图。"""
    parts = []
    cursor = 0
    catalog = fallback_emote_map("nga")
    for start, end, token, url in iter_emote_matches(text, "nga", catalog):
        if start > cursor:
            parts.append(CommentRichPart("text", text=text[cursor:start]))
        parts.append(CommentRichPart("emote", text=token, url=url) if url
                         else CommentRichPart("text", text=token))
        cursor = end
    if cursor < len(text):
        parts.append(CommentRichPart("text", text=text[cursor:]))
    return parts or [CommentRichPart("text", text=text)]


class NgaCommentFeed(SocialCommentFeedBase):
    PLATFORM_SLUG = "nga"
    AVATAR_REFERER = "https://bbs.nga.cn/"

    async def build_document(self, data: dict, tid: str) -> CommentDocument | None:
        # Local import avoids a parser/feed cycle; both consume the same BBCode rules.
        from .nga import bbcode_flow

        hot = data.get("hot_post")
        hot = hot if isinstance(hot, list) else []
        posts = data.get("result")
        posts = posts if isinstance(posts, list) else []
        entries = []
        seen = set()
        for post in hot + posts:
            if not isinstance(post, dict) or str(post.get("tid")) != tid:
                continue
            if str(post.get("comment_to_id") or "") not in {"", "0"}:
                continue
            pid = str(post.get("pid", ""))
            if not pid.isdigit() or int(pid) == 0 or pid in seen:
                continue
            seen.add(pid)
            flow = bbcode_flow(str(post.get("content") or ""), str(data.get("attachPrefix") or ""))
            author = post.get("author")
            author = author if isinstance(author, dict) else {}
            entries.append(CommentEntry(
                author=CommentAuthor(nickname=str(author.get("nickname") or author.get("username") or "匿名用户")),
                content=[part for kind, value in flow if kind == "text" for part in _rich_parts(value)],
                images=[value for kind, value in flow if kind == "image"],
                time_text=str(post.get("postdate") or ""),
                like_text=self.count_text(post.get("vote_good")),
            ))
        entries = await self.comment_filter.apply(entries, limit=self.limit)
        if not entries:
            return None
        return CommentDocument(theme=CommentTheme("NGA"), work_title=str(data.get("tsubject") or ""),
                               cover="", total_text=self.count_text(data.get("vrows")), entries=entries)
