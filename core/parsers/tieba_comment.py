"""Public root-floor comments, not nested replies.

Independent HTML adapter; field documentation (not copied source):
https://www.cnblogs.com/Liqiongyu/p/5936019.html
Documents l_post data-field.author.{user_id,user_name,portrait} and
content.{post_id,post_no,date}. The MIT ParseHub reference has no comments.
Admission additionally requires content.thread_id == requested tid for EVERY
floor on every fetched page. This is a conservative safety contract; presence
of this field on current real public HTML has NOT been verified. Missing or
conflicting identity suppresses the entire optional document, never the post.
"""
from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from .social_comment_feed import SocialCommentFeedBase


class _FloorHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.root_depth = None
        self.body_depth = None
        self.current = None
        self.floors = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"img", "br", "hr", "input", "meta", "link", "source", "wbr"}:
            self.depth += 1
        classes = attrs.get("class", "").split()
        if "l_post" in classes and self.current is None:
            try:
                fields = json.loads(attrs.get("data-field", ""))
            except (ValueError, TypeError):
                return
            if not isinstance(fields, dict):
                return
            self.current = {"fields": fields, "text": [], "images": []}
            self.root_depth = self.depth
        if self.current is not None:
            if "d_post_content" in classes:
                self.body_depth = self.depth
            if self.body_depth is not None:
                if tag == "br":
                    self.current["text"].append("\n")
                if tag == "img":
                    self.current["images"].append(attrs.get("data-original") or attrs.get("src") or "")

    def handle_endtag(self, tag):
        if tag in {"img", "br", "hr", "input", "meta", "link", "source", "wbr"}:
            return
        if self.body_depth == self.depth:
            self.body_depth = None
        if self.current is not None and self.root_depth == self.depth:
            self.floors.append(self.current)
            self.current = None
            self.root_depth = None
            self.body_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.current is not None and self.body_depth is not None:
            self.current["text"].append(data)


class TiebaCommentFeed(SocialCommentFeedBase):
    PLATFORM_SLUG = "tieba"
    AVATAR_REFERER = "https://tieba.baidu.com/"

    def __init__(self, parser):
        super().__init__(parser, limit=parser.comment_settings.display_count)

    async def fetch_document(self, tid, author_id=""):
        if not self.parser.comment_settings.enabled:
            return None
        try:
            async with asyncio.timeout(self.parser.comment_settings.timeout):
                return await self._fetch(tid, str(author_id))
        except Exception:
            # Authentication/captcha/HTML changes or comment failures never fail a post.
            return None

    async def _fetch(self, tid, author_id):
        if not re.fullmatch(r"[0-9]{1,20}", str(tid)):
            return None
        candidates, entries, seen = [], [], set()
        for page in range(1, 4):
            raw = await self.parser._request_bytes("GET", f"https://tieba.baidu.com/p/{tid}?pn={page}")
            document = _FloorHTML()
            document.feed(raw.decode("utf-8", errors="replace"))
            added = 0
            for item in document.floors:
                fields = item["fields"]
                meta, author = fields.get("content") or {}, fields.get("author") or {}
                if not isinstance(meta, dict) or not isinstance(author, dict):
                    return None
                # Conservative admission contract, not a claim this field is
                # currently present on live public HTML. Fail closed when no
                # per-floor thread identity is available; never infer from URL,
                # canonical links, executable JS, or a prior page's identity.
                if str(meta.get("thread_id", "")) != str(tid):
                    return None
                try:
                    floor = int(meta.get("post_no", 0))
                except (TypeError, ValueError):
                    continue
                identity = str(meta.get("post_id") or "")
                if floor <= 1 or not identity or identity in seen:
                    continue
                seen.add(identity)
                added += 1
                portrait = str(author.get("portrait") or "")
                avatar = self.parser._media_url("https://tb.himg.baidu.com/sys/portrait/item/" + portrait) if re.fullmatch(r"[A-Za-z0-9_-]+", portrait) else ""
                candidates.append(CommentEntry(
                    author=CommentAuthor(nickname=str(author.get("user_name") or "贴吧用户"), avatar=avatar,
                        is_up=bool(author_id and str(author.get("user_id")) == author_id)),
                    content=[CommentRichPart("text", "".join(item["text"]).strip())],
                    images=[url for value in item["images"] if (url := self.parser._media_url(value))],
                    time_text=str(meta.get("date") or ""), meta_items=[f"{floor}楼"],
                ))
            entries = await self.comment_filter.apply(candidates, limit=self.limit)
            if len(entries) >= self.limit or not added:
                break
            # Only follow an explicitly advertised next numbered page, never infer.
            if not re.search(r"[?&](?:amp;)?pn=" + str(page + 1) + r"(?:[&#\"']|$)", raw.decode("utf-8", errors="replace")):
                break
        if not entries:
            return None
        return CommentDocument(CommentTheme("贴吧"), "", "", "根回复（最多3页）", entries)


__all__ = ["TiebaCommentFeed"]
