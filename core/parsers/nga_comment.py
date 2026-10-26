"""Unified NGA comments from the already-fetched anonymous page-one response."""
from __future__ import annotations

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from .social_comment_feed import SocialCommentFeedBase


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
                content=[CommentRichPart("text", text=value) for kind, value in flow if kind == "text"],
                images=[value for kind, value in flow if kind == "image"],
                time_text=str(post.get("postdate") or ""),
                like_text=self.count_text(post.get("vote_good")),
            ))
        entries = await self.comment_filter.apply(entries, limit=self.limit)
        if not entries:
            return None
        return CommentDocument(theme=CommentTheme("NGA"), work_title=str(data.get("tsubject") or ""),
                               cover="", total_text=self.count_text(data.get("vrows")), entries=entries)
