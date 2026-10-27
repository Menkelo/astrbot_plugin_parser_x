"""Bounded public client replies with embedded first nested reply; HTML fallback.

Anonymous /c/f/pb/page with lz=0, with_floor=1 independently observed on
2026-10-26: sub_post_list={pid, sub_post_list:[{id,author_id,content,time,...}]}.
Client page identity, parent pid, and user_list association are checked.
Only one client page is requested; existing model stores one child per root.

Independent HTML adapter; field documentation (not copied source):
https://www.cnblogs.com/Liqiongyu/p/5936019.html
Documents l_post data-field.author.{user_id,user_name,portrait} and
content.{post_id,post_no,date}. The MIT ParseHub reference has no comments.
Every fetched page must identify the requested thread through head canonical
or og:url metadata. Per-floor content.thread_id, when present, must agree.
Authentic historical HTML: Wayback snapshot 20161213193806 of
http://tieba.baidu.com/p/3138733512 (tests/fixtures/tieba_3138733512_archived_20161213.html).
It contains a canonical URL and thread_id on all 29 actual floors. Anonymous
live HTML returned HTTP 403 / 百度安全验证 on 2026-10-26, so current live
compatibility remains unverified. Identity conflicts/captcha suppress only
this optional document, never the post.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..comment_canvas import CommentAuthor, CommentDocument, CommentEntry, CommentRichPart, CommentTheme
from .social_comment_feed import SocialCommentFeedBase
from .tieba_media import portrait_url, emote_url


class _FloorHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.root_depth = None
        self.body_depth = None
        self.current = None
        self.floors = []
        self.in_head = False
        self.page_urls = []
        self.page_title = []
        self.in_title = False

    def identifies(self, tid):
        """All advertised head identities must agree; never trust the request URL."""
        if not self.page_urls or "百度安全验证" in "".join(self.page_title):
            return False
        for value in self.page_urls:
            try:
                url = urlsplit(value)
                if (url.scheme not in {"http", "https"} or url.hostname != "tieba.baidu.com"
                        or url.username or url.password or url.port not in {None, 80, 443}
                        or url.path != f"/p/{tid}" or url.fragment):
                    return False
            except ValueError:
                return False
        return True

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.in_head = True
        if self.in_head:
            if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
                self.page_urls.append(attrs.get("href", ""))
            if tag == "meta" and attrs.get("property", "").lower() == "og:url":
                self.page_urls.append(attrs.get("content", ""))
            if tag == "title":
                self.in_title = True
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
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self.in_title = False
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
        if self.in_title:
            self.page_title.append(data)
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
                try:
                    return await self._fetch_client(tid, str(author_id))
                except Exception as exc:
                    logging.getLogger(__name__).warning("Tieba client comments unavailable (%s); trying public HTML", type(exc).__name__)
                    return await self._fetch(tid, str(author_id))
        except Exception as exc:
            # Only a safe classification/status, never credentials or response bodies.
            status = re.search(r"HTTP [0-9]{3}", str(exc))
            logging.getLogger(__name__).warning("Tieba optional comments unavailable: %s %s; post retained",
                type(exc).__name__, status.group(0) if status else "")
            return None

    async def _fetch_client(self, tid, author_id):
        if not re.fullmatch(r"[0-9]{1,20}", str(tid)):
            return None
        # One public client page, at most 30 roots; embedded replies avoid N+1.
        data = {"_client_type": "2", "_client_version": "12.35.1.0",
                "kz": str(tid), "lz": "0", "pn": "1",
                "rn": str(min(30, max(10, self.limit + 1))), "with_floor": "1"}
        source = "".join(f"{key}={value}" for key, value in sorted(data.items()))
        data["sign"] = hashlib.md5((source + "tiebaclient!!!").encode()).hexdigest().upper()
        payload = await self.parser._request_json("POST", "https://tieba.baidu.com/c/f/pb/page", data=data)
        if (str(payload.get("error_code")) != "0" or not isinstance(payload.get("thread"), dict)
                or str(payload["thread"].get("id")) != str(tid)):
            raise ValueError("Tieba comment thread identity unavailable")
        users = payload.get("user_list")
        posts = payload.get("post_list")
        if not isinstance(users, list) or not isinstance(posts, list):
            raise ValueError("Tieba comment lists unavailable")
        users = {str(user.get("id")): user for user in users if isinstance(user, dict)}

        def entry(post):
            user = users.get(str(post.get("author_id")))
            if not user or not post.get("id") or not isinstance(post.get("content"), list):
                return None
            rich, images = [], []
            for part in post["content"]:
                if not isinstance(part, dict):
                    continue
                if str(part.get("type")) == "0":
                    rich.append(CommentRichPart('text', str(part.get('text') or '')))
                elif str(part.get("type")) == "2":
                    label = '[' + str(part.get('c') or part.get('text') or '表情') + ']'
                    url = emote_url(part.get('text'))
                    rich.append(CommentRichPart('emote', label, url=url) if url
                                else CommentRichPart('text', label))
                elif str(part.get("type")) == "3":
                    image = next((u for key in ("origin_src", "big_cdn_src", "cdn_src")
                                  if (u := self.parser._media_url(part.get(key)))), "")
                    if image:
                        images.append(image)
            return CommentEntry(author=CommentAuthor(
                nickname=str(user.get("name_show") or user.get("name") or "贴吧用户"),
                avatar=portrait_url(user.get("portrait")),
                is_up=bool(author_id and str(post.get("author_id")) == author_id)),
                content=rich, images=images,
                time_text=self.time_text(post.get("time")))

        candidates, seen = [], set()
        for post in posts[:30]:
            if not isinstance(post, dict):
                continue
            try:
                floor = int(post.get("floor", 0))
            except (TypeError, ValueError):
                continue
            identity = str(post.get("id") or "")
            if floor <= 1 or identity in seen:
                continue
            seen.add(identity)
            root = entry(post)
            if root is None:
                continue
            root.meta_items = [f"{floor}楼"]
            nested = post.get("sub_post_list")
            if isinstance(nested, dict) and str(nested.get("pid")) == identity:
                children = nested.get("sub_post_list")
                if isinstance(children, list):
                    for child in children[:10]:
                        if isinstance(child, dict) and str(child.get("id")) != identity:
                            root.first_reply = entry(child)
                            if root.first_reply:
                                break
            candidates.append(root)
        entries = await self.comment_filter.apply_keeping_pinned(candidates, limit=self.limit)
        if not entries:
            return None
        return CommentDocument(CommentTheme("贴吧"), "", "", "公开回复（客户端首1页，含首条楼中楼）", entries)

    async def _fetch(self, tid, author_id):
        if not re.fullmatch(r"[0-9]{1,20}", str(tid)):
            return None
        candidates, entries, seen = [], [], set()
        for page in range(1, 4):
            raw = await self.parser._request_bytes("GET", f"https://tieba.baidu.com/p/{tid}?pn={page}")
            document = _FloorHTML()
            document.feed(raw.decode("utf-8", errors="replace"))
            if not document.identifies(tid):
                return None
            added = 0
            for item in document.floors:
                fields = item["fields"]
                meta, author = fields.get("content") or {}, fields.get("author") or {}
                if not isinstance(meta, dict) or not isinstance(author, dict):
                    return None
                # A floor may omit thread_id, but an explicit conflicting or
                # empty identity invalidates the whole page, even after the limit.
                if "thread_id" in meta and str(meta["thread_id"]) != str(tid):
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
                avatar = portrait_url(author.get("portrait"))
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
