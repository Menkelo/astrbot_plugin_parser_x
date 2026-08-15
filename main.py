import asyncio
import hashlib
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import EmojiLikeArbiter
from .core.card_theme import resolve_card_theme
from .core.clean import CacheCleaner
from .core.comment_settings import parse_bool
from .core.data import (
    AudioContent,
    DeliveryBatch,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    MediaContent,
    ParseResult,
    VideoContent,
)
from .core.download import Downloader
from .core.exception import (
    DownloadException,
    DownloadLimitException,
    ParseException,
    SizeLimitException,
    SkipParseException,
    ZeroSizeException,
)
from .core.html_renderer import HtmlRenderService
from .core.parsers import BaseParser
from .core.text_renderer import TextCardRenderer
from .core.utils import exec_ffmpeg_cmd, extract_json_url

PLUGIN_NAME = "astrbot_plugin_parser_x"


class ParserXPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        config["data_dir"] = str(self.data_dir)
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)

        # v1 used a top-level bili_comment switch. Keep the user's previous
        # choice when upgrading to the shared multi-platform comment settings.
        if not parse_bool(config.get("comment_settings_migrated", False), False):
            comment_config = config.get("comments", {})
            if not isinstance(comment_config, dict):
                comment_config = {}
            comment_config["bilibili"] = parse_bool(
                config.get("bili_comment", True),
                True,
            )
            config["comments"] = comment_config
            config["comment_settings_migrated"] = True

        # v2 groups session and notification settings under ``behavior`` for a
        # cleaner WebUI. Preserve values from the previous top-level fields.
        if not parse_bool(config.get("config_v2_migrated", False), False):
            behavior = config.get("behavior", {})
            if not isinstance(behavior, dict):
                behavior = {}
            legacy_sessions = config.get("disabled_sessions", [])
            if not behavior.get("disabled_sessions") and isinstance(
                legacy_sessions, list
            ):
                behavior["disabled_sessions"] = list(legacy_sessions)
            behavior["show_download_fail_tip"] = parse_bool(
                config.get("show_download_fail_tip", True),
                True,
            )
            config["behavior"] = behavior
            config["config_v2_migrated"] = True

        # Tieba was previously exposed through an unreliable third-party
        # detail service. Remove stale values from upgraded installations so
        # the deleted route does not linger in the persisted configuration.
        platforms = config.get("platforms", {})
        if isinstance(platforms, dict):
            platforms.pop("tieba", None)
            config["platforms"] = platforms
        integrations = config.get("integrations", {})
        if isinstance(integrations, dict):
            integrations.pop("tieba_api_base", None)
            if integrations:
                config["integrations"] = integrations
            else:
                config.pop("integrations", None)
        self.config.save_config()

        self.parser_map: dict[str, BaseParser] = {}
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.downloader = Downloader(config)
        self.arbiter = EmojiLikeArbiter()
        self.cleaner = CacheCleaner(self.context, self.config)
        self.render_service = HtmlRenderService.from_config(config, self.html_render)
        self.text_renderer = TextCardRenderer(self.render_service)

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        await self.downloader.close()
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        await self.cleaner.stop()

    def _register_parser(self):
        all_subclass = BaseParser.get_all_subclass()
        platform_names = []
        for _cls in all_subclass:
            platform_name = _cls.platform.name
            if not self._platform_enabled(platform_name):
                logger.info(f"Parser X 已禁用平台: {_cls.platform.display_name}")
                continue
            parser = _cls(self.config, self.downloader)
            if hasattr(parser, "set_render_service"):
                parser.set_render_service(self.render_service)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(f"Parser X 已加载平台: {'、'.join(platform_names) or '无'}")

        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in all_subclass
            if self._platform_enabled(cls.platform.name)
            for kw, pt in cls._key_patterns
        ]
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

    def _platform_enabled(self, platform_name: str) -> bool:
        platforms = self.config.get("platforms", {})
        value = platforms.get(platform_name, True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no"}
        return bool(value)

    def _behavior(self) -> dict:
        behavior = self.config.get("behavior", {})
        return behavior if isinstance(behavior, dict) else {}

    def _disabled_sessions(self) -> list:
        sessions = self._behavior().get("disabled_sessions", [])
        if isinstance(sessions, list):
            return sessions
        legacy = self.config.get("disabled_sessions", [])
        return legacy if isinstance(legacy, list) else []

    def _save_disabled_sessions(self, sessions: list) -> None:
        behavior = self._behavior()
        behavior["disabled_sessions"] = sessions
        self.config["behavior"] = behavior
        self.config.save_config()

    def _show_download_fail_tip(self) -> bool:
        return parse_bool(
            self._behavior().get(
                "show_download_fail_tip",
                self.config.get("show_download_fail_tip", True),
            ),
            True,
        )

    # endregion

    # region 核心逻辑 (Download / Convert / Send)

    async def _download_content(
        self, cont: MediaContent
    ) -> tuple[MediaContent, Path | None, str | None]:
        try:
            path = await cont.get_path()
            return cont, path, None
        except SizeLimitException as e:
            return cont, None, str(e)
        except (DownloadLimitException, ZeroSizeException):
            return cont, None, None
        except DownloadException as e:
            return cont, None, f"[下载失败: {e}]"
        except ParseException as e:
            return cont, None, f"[{e}]"
        except Exception as e:
            logger.error(f"下载未知错误: {e}")
            return cont, None, "[下载错误]"

    def _convert_to_seg(
        self, cont: MediaContent, path: Path
    ) -> BaseMessageComponent | None:
        match cont:
            case ImageContent():
                return Image(str(path))
            case GraphicsContent():
                return Image(str(path))
            case VideoContent() | DynamicContent():
                if hasattr(cont, "is_file_upload") and cont.is_file_upload:
                    return File(name=path.name, file=str(path))
                return Video(str(path))
            case AudioContent():
                return File(name=path.name, file=str(path))
            case FileContent():
                return File(name=path.name, file=str(path))
        return None

    def _format_text_fallback(self, result: ParseResult) -> str:
        parts = []
        header = result.platform.display_name
        if result.author:
            header += f" @{result.author.name}"
        if header:
            parts.append(header)
        if result.text and result.text.strip():
            parts.append(result.text.strip())
        if result.extra_info:
            parts.append(result.extra_info.strip())
        if result.url:
            parts.append(f"链接：{result.url}")
        return "\n\n".join(parts).replace("@", "@\u200b")

    def _format_media_summary(self, result: ParseResult) -> str:
        """Build an opt-in text segment for parsers that also return media."""
        parts = []
        if result.header:
            parts.append(result.header)
        if result.text and result.text.strip():
            parts.append(result.text.strip())
        if result.extra_info:
            parts.append(result.extra_info.strip())
        if result.url:
            parts.append(f"链接：{result.url}")
        return "\n\n".join(parts).replace("@", "@\u200b")

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
    def _card_embeds_single_image(result: ParseResult) -> bool:
        """Return whether the shared card fully replaces the only source image."""

        if result.extra.get("keep_single_image_after_card"):
            return False
        media_url = result.extra.get("text_card_media")
        return bool(
            isinstance(media_url, str)
            and media_url.strip()
            and len(result.contents) == 1
            and isinstance(result.contents[0], (ImageContent, GraphicsContent))
        )

    @staticmethod
    def _bounded_timeout(value: object, default: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        if number != number:
            number = default
        return max(1.0, min(number, maximum))

    async def _resolve_card_palette(
        self,
        result: ParseResult,
    ) -> tuple[str, str, str]:
        theme = resolve_card_theme(result.platform.name, result.platform.display_name)
        return theme.accent, theme.accent_soft, "platform"

    async def _build_text_card_content(
        self,
        result: ParseResult,
        comment_document: object | None = None,
    ) -> ImageContent | None:
        text = self._text_card_body(result)
        title = (result.title or "").strip() or None
        media_url = result.extra.get("text_card_media")
        if not isinstance(media_url, str) or not media_url.strip():
            media_url = None
        if not text and not title and not media_url:
            text = (result.url or "").strip()
        if not text and not title and not media_url:
            return None

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
        media_fit = str(
            requested_media_fit
            or ("contain" if self._card_embeds_single_image(result) else "cover")
        )
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
        comment_signature = (
            repr(comment_document) if comment_document is not None else ""
        )
        accent_color, accent_soft, accent_source = await self._resolve_card_palette(
            result
        )

        digest = hashlib.md5(
            "\n".join(
                [
                    result.platform.name,
                    author_name or "",
                    author_avatar or "",
                    title or "",
                    timestamp_text or "",
                    result.url or "",
                    text,
                    media_url or "",
                    media_fit,
                    content_kind or "",
                    repr(result.extra.get("card_metrics")),
                    repr(result.extra.get("card_info")),
                    repr(result.extra.get("card_emotes")),
                    author_badge or "",
                    comment_signature,
                    accent_color,
                    accent_soft,
                    accent_source,
                    "text_card_v13_single_image_inline_plain_header",
                ]
            ).encode("utf-8")
        ).hexdigest()[:12]

        platform_slug = re.sub(
            r"[^A-Za-z0-9_-]+", "_", result.platform.name or "text"
        ).strip("_")
        if not platform_slug:
            platform_slug = "text"

        out_path = self.cache_dir / f"text_card_{platform_slug}_{digest}.png"
        if not out_path.exists():
            await self.text_renderer.render_text_card(
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
                comment_document=comment_document,
                accent_color=accent_color,
                accent_soft=accent_soft,
                accent_source=accent_source,
                emotes=result.extra.get("card_emotes"),
            )

        return ImageContent(out_path)

    async def _build_text_card_with_comment_fallback(
        self,
        result: ParseResult,
        comment_document: object | None,
    ) -> tuple[ImageContent | None, bool]:
        can_embed_comments = TextCardRenderer.supports_comment_document(
            comment_document
        )
        try:
            card = await self._build_text_card_content(result, comment_document)
        except Exception:
            if not can_embed_comments:
                raise
            logger.warning("评论合并渲染失败，重试生成无评论正文卡")
            card = await self._build_text_card_content(result, None)
            return card, False
        return card, bool(card is not None and can_embed_comments)

    async def _transcode_to_h264(self, input_path: Path) -> Path:
        output_path = input_path.with_name(f"{input_path.stem}_h264.mp4")
        logger.info(
            f"正在转码视频为 H.264 (极速模式): {input_path.name} -> {output_path.name}"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-preset",
            "superfast",
            "-tune",
            "zerolatency",
            "-crf",
            "28",
            "-vf",
            "scale='min(1280,iw)':-2",
            "-maxrate",
            "1.5M",
            "-bufsize",
            "3M",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(output_path),
        ]
        await exec_ffmpeg_cmd(cmd)
        return output_path

    async def _send_card_reply(
        self,
        event: AstrMessageEvent,
        card: ImageContent,
        *,
        original_message_reply,
    ) -> bool:
        """Send the unified long card as one dedicated reply message."""
        _, path, error = await self._download_content(card)
        if error:
            logger.warning(f"统一长卡下载失败，保留原生正文链路: {error}")
            return False
        if path is None:
            logger.warning("统一长卡没有可发送的本地文件，保留原生正文链路")
            return False

        segment = self._convert_to_seg(card, path)
        if segment is None:
            logger.warning("统一长卡无法转换为消息段，保留原生正文链路")
            return False

        reply = original_message_reply()
        if reply is None:
            logger.warning("原消息缺少 message_id，已跳过统一长卡并保留原生正文链路")
            return False
        chain: list[BaseMessageComponent] = [reply, segment]
        try:
            await event.send(event.chain_result(chain))
        except Exception as exc:
            logger.warning(f"统一长卡引用发送失败，保留原生正文链路: {exc}")
            return False
        return True

    async def _send_video_segment(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        segment: Video,
    ) -> None:
        try:
            await event.send(event.chain_result([segment]))
            return
        except Exception as exc:
            error_text = str(exc)
            should_transcode = any(
                marker in error_text for marker in ("rich media", "1200", "Timeout")
            )
            path_text = getattr(segment, "file", None)
            if should_transcode and path_text:
                try:
                    input_path = Path(path_text)
                    new_path = await self._transcode_to_h264(input_path)
                    await event.send(event.chain_result([Video(str(new_path))]))
                    try:
                        if input_path.exists():
                            await asyncio.to_thread(input_path.unlink)
                    except Exception:
                        pass
                    return
                except Exception as transcode_exc:
                    logger.warning(f"视频转码重试失败: {transcode_exc}")

            logger.warning(f"视频发送失败: {exc}")
            await event.send(
                event.plain_result(f"⚠️ 媒体发送失败\n🔗 原链接: {result.url or '未知'}")
            )

    async def _send_delivery_plan(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        node_uin: str,
        node_name: str,
        original_message_reply,
        comment_document: object | None = None,
    ) -> bool:
        plan = result.delivery
        if plan is None:
            return False

        batches = list(plan.batches)
        comments_embedded = False
        body = (result.text or "").strip()
        if result.platform.name == "weibo" and body:
            planned_text = "\n".join(
                part
                for batch in batches
                for part in batch.parts
                if isinstance(part, str)
            )
            normalized_body = re.sub(r"\s+", " ", body).strip()
            normalized_planned = re.sub(r"\s+", " ", planned_text).strip()
            if normalized_body not in normalized_planned:
                if (
                    batches
                    and batches[0].mode == "direct"
                    and not batches[0].reply_original
                    and batches[0].parts
                    and all(isinstance(part, str) for part in batches[0].parts)
                ):
                    first = batches[0]
                    first_parts = list(first.parts)
                    first_parts[0] = f"{first_parts[0].rstrip()}\n{body}".strip()
                    batches[0] = DeliveryBatch(
                        first_parts,
                        mode=first.mode,
                        reply_original=first.reply_original,
                    )
                else:
                    batches.insert(0, DeliveryBatch([f"识别：微博\n{body}"]))
                logger.warning("微博投递计划缺少正文，已自动补回")

        if result.extra.get("render_text_card"):
            try:
                (
                    card,
                    card_comments_embedded,
                ) = await self._build_text_card_with_comment_fallback(
                    result,
                    comment_document,
                )
            except Exception as exc:
                logger.warning(f"delivery text-card render failed: {exc}")
            else:
                if card is not None:
                    card_sent = await self._send_card_reply(
                        event,
                        card,
                        original_message_reply=original_message_reply,
                    )
                    if card_sent:
                        try:
                            batch_index = int(
                                result.extra.get("delivery_text_card_batch", 0)
                            )
                        except (TypeError, ValueError):
                            batch_index = 0
                        if 0 <= batch_index < len(batches):
                            original = batches[batch_index]
                            replace_text = bool(
                                result.extra.get(
                                    "delivery_text_card_replace_text",
                                    True,
                                )
                            )
                            remaining = [
                                part
                                for part in original.parts
                                if not replace_text or not isinstance(part, str)
                            ]
                            if remaining:
                                batches[batch_index] = DeliveryBatch(
                                    remaining,
                                    mode=original.mode,
                                )
                            else:
                                batches.pop(batch_index)

                        embedded_image = (
                            result.contents[0]
                            if self._card_embeds_single_image(result)
                            else None
                        )
                        cleaned_batches: list[DeliveryBatch] = []
                        for batch in batches:
                            parts = [
                                part
                                for part in batch.parts
                                if part is not embedded_image
                            ]
                            if parts:
                                cleaned_batches.append(
                                    DeliveryBatch(parts, mode=batch.mode)
                                )
                        batches = cleaned_batches
                        comments_embedded = card_comments_embedded

        show_download_fail_tip = self._show_download_fail_tip()
        path_map: dict[int, tuple[Path | None, str | None]] = {}

        async def send_individually(
            segments: list[BaseMessageComponent],
            *,
            reply_first: bool = False,
        ) -> None:
            for index, segment in enumerate(segments):
                if isinstance(segment, Video):
                    await self._send_video_segment(event, result, segment)
                    continue
                try:
                    chain: list[BaseMessageComponent] = []
                    if (
                        reply_first
                        and index == 0
                        and (reply := original_message_reply()) is not None
                    ):
                        chain.append(reply)
                    chain.append(segment)
                    await event.send(event.chain_result(chain))
                except Exception as exc:
                    logger.warning(f"平台消息逐段发送失败: {exc}")

        for batch in batches:
            pending_media = [
                part
                for part in batch.parts
                if isinstance(part, MediaContent) and id(part) not in path_map
            ]
            if pending_media:
                download_results = await asyncio.gather(
                    *(self._download_content(content) for content in pending_media)
                )
                path_map.update(
                    {
                        id(content): (path, error)
                        for content, path, error in download_results
                    }
                )

            segments: list[BaseMessageComponent] = []
            for part in batch.parts:
                if isinstance(part, str):
                    text = part.strip()
                    if text:
                        segments.append(Plain(text.replace("@", "@\u200b")))
                    continue

                path, error = path_map.get(id(part), (None, None))
                if error:
                    if show_download_fail_tip:
                        segments.append(Plain(error.strip()))
                    continue
                if path and (segment := self._convert_to_seg(part, path)):
                    segments.append(segment)

            if not segments:
                continue

            if batch.mode == "forward":
                for offset in range(0, len(segments), 20):
                    group = segments[offset : offset + 20]
                    nodes = Nodes(
                        [
                            Node(uin=node_uin, name=node_name, content=[segment])
                            for segment in group
                        ]
                    )
                    try:
                        await event.send(event.chain_result([nodes]))
                    except Exception as exc:
                        logger.warning(f"平台合并转发发送失败，降级逐段发送: {exc}")
                        await send_individually(group)
                continue

            if len(segments) == 1 and isinstance(segments[0], Video):
                await self._send_video_segment(event, result, segments[0])
                continue

            chain: list[BaseMessageComponent] = []
            if batch.reply_original and (reply := original_message_reply()) is not None:
                chain.append(reply)
            chain.extend(segments)
            try:
                await event.send(event.chain_result(chain))
            except Exception as exc:
                logger.warning(f"平台消息链发送失败，降级逐段发送: {exc}")
                await send_individually(
                    segments,
                    reply_first=batch.reply_original,
                )

        return comments_embedded

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        show_download_fail_tip = self._show_download_fail_tip()

        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        def original_message_reply() -> Reply | None:
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id in (None, ""):
                return None
            return Reply(id=message_id)

        comment_document: object | None = None
        comment_document_attempted = False
        comment_document_factory = result.extra.get("comment_document_task_factory")
        if result.extra.get("render_text_card") and callable(comment_document_factory):
            timeout = self._bounded_timeout(
                result.extra.get("comment_timeout", 90),
                90,
                180,
            )
            merge_timeout = min(
                timeout,
                self._bounded_timeout(
                    result.extra.get("comment_merge_timeout", 15),
                    15,
                    15,
                ),
            )
            comment_document_attempted = True
            try:
                comment_document = await asyncio.wait_for(
                    comment_document_factory(),
                    timeout=merge_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("评论区结构化抓取超时，正文卡将不含评论")
            except Exception as exc:
                logger.warning(f"评论区结构化抓取失败，正文卡将不含评论: {exc}")

        comments_embedded = False

        async def process_main_content():
            nonlocal comments_embedded
            parsed_contents = tuple(result.contents)
            if getattr(result, "delivery", None) is not None:
                comments_embedded = await self._send_delivery_plan(
                    event,
                    result,
                    node_uin=node_uin,
                    node_name=node_name,
                    original_message_reply=original_message_reply,
                    comment_document=comment_document,
                )
                return

            if not result.contents and result.extra.get("plain_text_only"):
                text = (result.text or "").strip()
                if text:
                    await event.send(event.plain_result(text.replace("@", "@\u200b")))
                return

            card_requested = bool(result.extra.get("render_text_card")) or not bool(
                result.contents
            )
            card_sent = False
            if card_requested:
                try:
                    (
                        card,
                        card_comments_embedded,
                    ) = await self._build_text_card_with_comment_fallback(
                        result,
                        comment_document,
                    )
                    if card:
                        card_sent = await self._send_card_reply(
                            event,
                            card,
                            original_message_reply=original_message_reply,
                        )
                        if card_sent:
                            comments_embedded = card_comments_embedded
                except Exception as e:
                    logger.warning(f"unified text-card render failed: {e}")

            if not result.contents:
                if card_sent:
                    return
                fallback = self._format_text_fallback(result)
                if fallback:
                    await event.send(event.plain_result(fallback))
                return

            if card_sent and self._card_embeds_single_image(result):
                return

            tasks = [self._download_content(c) for c in result.contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}

            segs = []
            if result.extra.get("send_text") or (card_requested and not card_sent):
                summary = self._format_media_summary(result)
                if summary:
                    segs.append(Plain(summary))
            for cont in result.contents:
                path, error = path_map.get(id(cont), (None, None))

                if error:
                    if show_download_fail_tip:
                        segs.append(Plain(error.strip()))
                    continue

                if path:
                    if seg := self._convert_to_seg(cont, path):
                        segs.append(seg)

            if not segs:
                error_msgs = [err for _, _, err in download_results if err]
                if error_msgs and show_download_fail_tip:
                    msg = "\n".join(error_msgs)
                    await event.send(event.plain_result(msg.strip()))
                return

            force_direct_media = bool(result.extra.get("force_direct_media", False))
            if force_direct_media:
                for seg in segs:
                    await event.send(event.chain_result([seg]))
                return

            has_video = any(
                isinstance(c, (VideoContent, DynamicContent)) for c in result.contents
            )

            if has_video:
                if result.extra.get("video_separate_from_card"):
                    for seg in segs:
                        if isinstance(seg, Video):
                            await self._send_video_segment(event, result, seg)
                            continue
                        try:
                            await event.send(event.chain_result([seg]))
                        except Exception as exc:
                            logger.warning(f"视频附属卡片发送失败: {exc}")
                    return

                media_count = sum(
                    1 for s in segs if isinstance(s, (Video, Image, File, Record))
                )
                if media_count >= 2:
                    try:
                        nodes = Nodes([])
                        for seg in segs:
                            nodes.nodes.append(
                                Node(uin=node_uin, name=node_name, content=[seg])
                            )
                        await event.send(event.chain_result([nodes]))
                        return
                    except Exception as e:
                        logger.warning(f"合并转发发送失败，降级逐条发送: {e}")

                for seg in segs:
                    try:
                        await event.send(event.chain_result([seg]))
                    except Exception as e:
                        err_msg = str(e)
                        if isinstance(seg, Video) and (
                            "rich media" in err_msg
                            or "1200" in err_msg
                            or "Timeout" in err_msg
                        ):
                            logger.warning(
                                "视频发送失败(编码不兼容/超时)，尝试转码 H.264 重试..."
                            )
                            path_str = getattr(seg, "file", None)
                            if path_str:
                                try:
                                    input_path = Path(path_str)
                                    new_path = await self._transcode_to_h264(input_path)
                                    await event.send(
                                        event.chain_result([Video(str(new_path))])
                                    )
                                    try:
                                        if input_path.exists():
                                            await asyncio.to_thread(input_path.unlink)
                                    except Exception:
                                        pass
                                    continue
                                except Exception:
                                    pass

                        await event.send(
                            event.plain_result(
                                f"⚠️ 媒体发送失败\n🔗 原链接: {result.url or '未知'}"
                            )
                        )
            else:
                source_is_single_image = len(parsed_contents) == 1 and isinstance(
                    parsed_contents[0], (ImageContent, GraphicsContent)
                )
                if not card_sent and (
                    result.extra.get("reply_original_for_single_image")
                    or source_is_single_image
                ):
                    if segs and all(isinstance(seg, (Plain, Image)) for seg in segs):
                        chain: list[BaseMessageComponent] = []
                        if (reply := original_message_reply()) is not None:
                            chain.append(reply)
                        chain.extend(segs)
                        await event.send(event.chain_result(chain))
                        return

                if len(segs) == 1:
                    chain: list[BaseMessageComponent] = []
                    if (
                        not card_sent
                        and len(parsed_contents) == 1
                        and isinstance(
                            parsed_contents[0], (ImageContent, GraphicsContent)
                        )
                        and isinstance(segs[0], Image)
                        and (reply := original_message_reply()) is not None
                    ):
                        chain.append(reply)
                    chain.append(segs[0])
                    await event.send(event.chain_result(chain))
                    return

                # 先尝试合并转发；若 send_group_forward_msg 超时/失败（NapCat 上传多图
                # 时较常见的 WebSocket API call timeout），降级为逐条发送，避免整条消息
                # 因合并转发失败而完全发不出（此前图文/图集分支无兜底，超时即静默丢失）。
                for offset in range(0, len(segs), 20):
                    group = segs[offset : offset + 20]
                    nodes = Nodes(
                        [
                            Node(uin=node_uin, name=node_name, content=[segment])
                            for segment in group
                        ]
                    )
                    try:
                        await event.send(event.chain_result([nodes]))
                    except Exception as e:
                        logger.warning(f"合并转发发送失败，降级逐条发送: {e}")
                        for segment in group:
                            try:
                                await event.send(event.chain_result([segment]))
                            except Exception as segment_exc:
                                logger.warning(f"图片逐条发送失败: {segment_exc}")
                return

        await process_main_content()

        if (
            comment_document_attempted
            and comment_document is not None
            and not comments_embedded
        ):
            logger.warning("统一长卡未能嵌入评论，已省略评论且不再生成旧式独立评论图")

    # endregion

    # region 事件监听

    @staticmethod
    def _looks_like_live_share(text: str) -> bool:
        low = (text or "").lower()
        return any(
            key in text
            for key in (
                "正在直播",
                "直接观看直播",
                "观看直播",
                "直播很精彩",
                "来看直播",
            )
        ) or any(
            key in low
            for key in (
                "livestream",
                "live.kuaishou.com",
                "live.douyin.com",
                "webcast.douyin",
                "/live/",
            )
        )

    def _should_ignore_live_share(
        self,
        keyword: str,
        raw_match: str,
        text: str,
    ) -> bool:
        low_url = (raw_match or "").lower()
        low_text = (text or "").lower()

        if keyword in {"v.kuaishou", "kuaishou", "chenzhongtech"}:
            return (
                "live.kuaishou.com" in low_url
                or "/live/" in low_url
                or self._looks_like_live_share(text)
            )

        if keyword in {"xhslink.com", "xhslink.cn"}:
            return "livestream" in low_text or (
                "小红书" in text and self._looks_like_live_share(text)
            )

        if keyword in {"v.douyin", "jx.douyin"}:
            return self._looks_like_live_share(text)

        return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        umo = event.unified_msg_origin
        text = (event.message_str or "").strip()

        if umo in self._disabled_sessions():
            return

        if not text:
            chain = event.get_messages()
            if chain:
                for segment in chain:
                    if not isinstance(segment, Json):
                        continue
                    try:
                        text = extract_json_url(segment.data) or ""
                    except Exception:
                        text = ""
                    if text:
                        break

        if not text:
            return

        prefixes = self.context.get_config().get("command_prefixes", ["/"])
        is_command = any(text.startswith(p) for p in prefixes)
        if is_command:
            return

        self_id = event.get_self_id()
        chain = event.get_messages()
        if chain and isinstance(chain[0], At) and str(chain[0].qq) != self_id:
            return

        matches: list[tuple[int, str, re.Match[str]]] = []
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            for m in pat.finditer(text):
                matches.append((m.start(), kw, m))

        matches = [
            (start, kw, m)
            for start, kw, m in matches
            if not self._should_ignore_live_share(kw, m.group(0), text)
        ]

        if not matches:
            return

        if isinstance(event, AiocqhttpMessageEvent):
            if hasattr(event.message_obj, "message_id"):
                asyncio.create_task(
                    self.arbiter.notify(event.bot, event.message_obj.message_id)
                )

        matches.sort(key=lambda x: (x[0], -len(x[1])))
        processed_matches: set[tuple[str, str]] = set()
        accepted_spans: list[tuple[int, int]] = []

        for _, keyword, searched in matches:
            parser = self.parser_map.get(keyword)
            if parser is None:
                continue

            # 同一段文字被多个关键词规则命中时只处理一次。
            # 例如 https://v.kuaishou.com/xxx 会同时匹配 "v.kuaishou" 与 "kuaishou"
            # （后者是前者的子串），此前会被解析两次：第一次出视频、第二次失败
            # 又补发一条 ⚠️ 提示。按字符区间去重可避免重复解析/重复发送。
            span = (searched.start(), searched.end())
            if any(
                span[0] < a_end and a_start < span[1]
                for a_start, a_end in accepted_spans
            ):
                continue

            raw_match = searched.group(0).strip()
            match_url = re.sub(r"^https?://", "", raw_match, flags=re.IGNORECASE)
            match_url = match_url.rstrip(").,;!?，。；！？）]")
            match_key = (parser.platform.name, match_url.casefold())
            if match_key in processed_matches:
                continue
            processed_matches.add(match_key)
            accepted_spans.append(span)

            try:
                parser.source_text = text
                parse_res = await parser.parse(keyword, searched)
                await self._send_parse_result(event, parse_res)
            except SkipParseException:
                continue
            except SizeLimitException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except ParseException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except Exception:
                logger.exception("解析过程中发生未知错误")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        sessions = self._disabled_sessions()
        if umo in sessions:
            sessions.remove(umo)
            self._save_disabled_sessions(sessions)
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        sessions = self._disabled_sessions()
        if umo not in sessions:
            sessions.append(umo)
            self._save_disabled_sessions(sessions)
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("解析状态")
    async def parser_status(self, event: AstrMessageEvent):
        """查看 Parser X 当前会话状态与已启用平台。"""
        enabled = sorted({p.platform.display_name for p in self.parser_map.values()})
        state = (
            "关闭" if event.unified_msg_origin in self._disabled_sessions() else "开启"
        )
        yield event.plain_result(
            f"Parser X 当前会话：{state}\n已启用平台：{'、'.join(enabled) or '无'}"
        )
