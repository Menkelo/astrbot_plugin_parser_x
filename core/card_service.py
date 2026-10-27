"""Card preparation and comment work reuse for the shared plugin pipeline."""

import asyncio
import hashlib
import json
import re
import uuid

from astrbot.api import logger

from .async_cache import AsyncSingleFlight
from . import cache_policy
from .cache_policy import reusable_card
from .comment_canvas import CommentDocument
from .comment_settings import parse_bool
from .data import ImageContent, ParseResult
from .delivery import _GeneratedCardContent
from .parsers.bilibili.comment_canvas import BiliCommentDocument
from .result_cache import _config_signature


class CardServiceMixin:
    """Prepare cards using the concrete plugin's caches and renderer."""

    #: 正文可以自己拉官方目录的平台（评论目录只覆盖评论区，正文需要单独取）。
    _BODY_EMOTE_CATALOG_PLATFORMS = {"xiaohongshu", "douyin"}

    async def _resolve_card_emotes(self, result: ParseResult) -> object:
        """仅下载正文实际用到的表情；受管理平台失败时直接保留原文。"""
        emotes = result.extra.get("card_emotes")
        platform_key = str(getattr(result.platform, "name", "") or "")
        body = self._text_card_body(result) + '\n' + str(result.title or '')
        if platform_key in self._BODY_EMOTE_CATALOG_PLATFORMS and not isinstance(emotes, dict):
            # 正文不依赖评论开关/Cookie。解析器未挂目录时用官方目录补齐。
            from .platform_emotes import fallback_emote_map, load_platform_emotes, contains_platform_emotes

            emotes = fallback_emote_map(platform_key)
            if contains_platform_emotes(body, platform_key):
                parsers = getattr(self, 'parser_map', {})
                parser = next((p for p in parsers.values()
                               if getattr(getattr(p, 'platform', None), 'name', '') == platform_key), None)
                if parser is not None:
                    try:
                        emotes = await asyncio.wait_for(load_platform_emotes(parser, platform_key), timeout=3)
                    except Exception:
                        logger.debug(f'{platform_key} 正文表情目录读取失败，使用内置兜底', exc_info=True)
        if not isinstance(emotes, dict) or platform_key not in {"coolapk", "xiaohongshu", "douyin"}:
            return emotes
        resolver = getattr(self, "emote_resolver", None)
        if resolver is None:
            return {}
        from .platform_emotes import iter_emote_matches

        blocks = result.extra.get('text_card_flow') or []
        for block in blocks:
            if isinstance(block, dict) and block.get('type') == 'text':
                body += '\n' + str(block.get('text') or '')
            elif isinstance(block, (tuple, list)) and len(block) == 2 and block[0] == 'text':
                body += '\n' + str(block[1])
        tokens = {token for _, _, token, _ in iter_emote_matches(body, platform_key, emotes)}
        captured = result.extra.get("card_emote_captures")
        if isinstance(captured, (list, tuple, set)):
            tokens.update(str(token) for token in captured if token)
        resolved: dict[str, str] = {}
        try:
            resolve_urls = getattr(resolver, 'resolve_urls', None)
            if callable(resolve_urls):
                resolved.update(await resolve_urls(platform_key, {
                    token: emotes[token] for token in tokens if emotes.get(token)
                }))
            resolved.update(await resolver.resolve(
                platform_key,
                set(tokens) - set(resolved),
                sources=emotes,
            ))
        except Exception as exc:
            logger.debug(f"正文表情解析失败，回落文本: {exc}")
        return resolved

    @staticmethod
    def _text_card_body(result: ParseResult) -> str:
        override = result.extra.get("text_card_text")
        if isinstance(override, str):
            return override.strip()

        parts: list[str] = []
        text = (result.text or "").strip()
        if text:
            parts.append(text)
        extra_info = (result.extra_info or "").strip()
        if extra_info and extra_info not in text:
            parts.append(extra_info)
        return "\n\n".join(parts)


    @staticmethod
    def _bounded_timeout(value: object, default: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        if number != number:
            number = default
        return max(1.0, min(number, maximum))

    async def _build_text_card_content(
        self,
        result: ParseResult,
        *,
        include_comments: bool = True,
    ) -> ImageContent | None:
        """Reuse successful work, but return a fresh delivery object to every caller."""
        comments = getattr(self, "config", {}).get("comments", {})
        if isinstance(comments, dict) and not parse_bool(comments.get("enabled", True), True):
            return None
        result.extra.pop('_video_card_suppressed', None)
        cached_card = result.extra.get("_disk_card_path")
        # Persistent results cannot serialize comment factories. A cached card is
        # reusable for video only when its manifest records actual comments.
        if (result.platform.name != 'wechat_article' and reusable_card(cached_card)
                and (not result.has_video or (include_comments and result.extra.get('_disk_has_comments') is True))):
            return _GeneratedCardContent(cached_card)
        if result.has_video and not result.extra.get('mixed_post_body_card') and (not include_comments or not callable(result.extra.get('text_card_comment_factory'))):
            result.extra['_video_card_suppressed'] = True
            return None
        result.extra.pop("_disk_card_path", None)
        for name in ("comment_document_cache", "comment_render_cache"):
            if not hasattr(self, name):
                setattr(self, name, AsyncSingleFlight())
        # Do not fingerprint factory objects: parsers rebuild closures for the
        # same work. Include every renderer input and settings, never raw cookies.
        extra = {
            key: value for key, value in result.extra.items()
            if not key.startswith("_disk_") and not callable(value) and not isinstance(value, asyncio.Future)
        }
        identity = [
            _config_signature(getattr(self, "config", {})), include_comments,
            callable(result.extra.get("text_card_comment_factory")),
            result.platform.name, result.platform.display_name,
            result.author.name if result.author else None,
            result.title, result.text, result.extra_info, result.timestamp, result.url, extra,
            cache_policy.CACHE_BEHAVIOR_VERSION,
        ]
        if result.platform.name == 'wechat_article':
            identity.append('wx-inline-loaded-v1')
        cache_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()

        card, complete = await self.comment_render_cache.get(
            cache_key,
            lambda: self._build_text_card_content_uncached(
                result, include_comments=include_comments, cache_key=cache_key
            ),
        )
        if card is None and result.has_video:
            result.extra['_video_card_suppressed'] = True
        if complete and card is not None:
            result.extra["_disk_card_path"] = await card.get_path()
        return _GeneratedCardContent(card.path_task) if card is not None else None

    async def _build_text_card_content_uncached(
        self, result: ParseResult, *, include_comments: bool, cache_key: str,
    ) -> tuple[ImageContent | None, bool]:
        """Build one card; incomplete comment attempts must remain retryable."""
        text = self._text_card_body(result)
        title = (result.title or "").strip() or None
        media_url = result.extra.get("text_card_media")
        if not isinstance(media_url, str) or not media_url.strip():
            media_url = None
        content_blocks = result.extra.get("text_card_flow")
        if not isinstance(content_blocks, (list, tuple)):
            content_blocks = None
        if not text and not title and not media_url and not content_blocks:
            text = (result.url or "").strip()
        if not text and not title and not media_url and not content_blocks:
            return None, False

        author_name = result.author.name if result.author else None
        author_avatar = result.extra.get("text_card_avatar")
        if not isinstance(author_avatar, str) or not author_avatar.strip():
            author_avatar = None
        platform_name = str(
            result.extra.get("card_platform_name")
            or result.platform.display_name
            or result.platform.name
        )
        timestamp_text = result.formatted_datetime
        requested_media_fit = result.extra.get("text_card_media_fit")
        media_fit = str(requested_media_fit or "cover")
        if media_fit not in {"cover", "contain"}:
            media_fit = "cover"
        content_kind = result.extra.get("card_kind")
        if not isinstance(content_kind, str):
            content_kind = None
        author_badge = result.extra.get("card_author_badge")
        if not isinstance(author_badge, str):
            author_badge = None
        info_title = result.extra.get("card_info_title")
        if not isinstance(info_title, str):
            info_title = None

        platform_slug = re.sub(
            r"[^A-Za-z0-9_-]+", "_", result.platform.name or "text"
        ).strip("_") or "text"
        stable_path = self.cache_dir / f"text_card_{platform_slug}_{cache_key}.png"
        if not result.has_video and reusable_card(stable_path):
            return ImageContent(stable_path), True

        # 评论区：仅支持 text_card_comment_factory（返回 CommentDocument）
        # 统一渲染器内联评论区，无需 PIL 拼接、无需下载评论图。
        comment_document = None
        comment_factory = (
            result.extra.get("text_card_comment_factory")
            if include_comments
            else None
        )
        if callable(comment_factory):
            try:
                async def fetch_document():
                    document = await asyncio.wait_for(
                        comment_factory(),
                        timeout=self._bounded_timeout(
                            result.extra.get("comment_timeout", 90), 30, 180,
                        ),
                    )
                    if isinstance(document, (CommentDocument, BiliCommentDocument)):
                        return document
                    return None

                comment_document = await self.comment_document_cache.get(
                    cache_key, fetch_document,
                )
            except Exception as exc:
                logger.warning(f"评论区获取失败，视频跳过卡片、图文保留正文卡: {exc}")
                comment_document = None

        if result.has_video and not result.extra.get('mixed_post_body_card'):
            # A pure video card is a comment card, not a metadata-only fallback.
            # Count captured entries, never the platform's advertised total.
            if comment_document is None or not comment_document.entries:
                result.extra['_video_card_suppressed'] = True
                return None, False
            if reusable_card(stable_path):
                return ImageContent(stable_path), True

        if result.platform.name == 'coolapk':
            from .article_images import local_coolapk_images
            content_blocks, media_url, author_avatar = await local_coolapk_images(
                result, self.downloader, content_blocks, media_url, author_avatar,
            )

        card_emotes = await self._resolve_card_emotes(result)
        complete = not callable(comment_factory) or comment_document is not None
        # Publish only complete renders. Temporary fallbacks remain retryable.
        out_path = self.cache_dir / f"text_card_{platform_slug}_temporary_{uuid.uuid4().hex}.png"
        await self.text_renderer.render_card(
            out_path=out_path,
            platform_name=platform_name,
            author_name=author_name,
            author_avatar=author_avatar,
            text=text,
            title=title,
            timestamp_text=timestamp_text,
            platform_key=result.platform.name,
            media_url=media_url,
            media_fit=media_fit,
            content_kind=content_kind,
            metrics=result.extra.get("card_metrics"),
            info_items=result.extra.get("card_info"),
            info_title=info_title,
            author_badge=author_badge,
            emotes=card_emotes,
            content_blocks=content_blocks,
            content_layout=result.extra.get("text_card_layout"),
            author_meta=result.extra.get("text_card_meta"),
            comment_document=comment_document,
        )

        if complete and out_path.is_file() and out_path.stat().st_size > 0:
            out_path.replace(stable_path)
            out_path = stable_path
        return ImageContent(out_path), complete

