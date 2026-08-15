import asyncio
import base64
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
from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)
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
from .core.debug_page import (
    DebugCaptureEvent,
    DebugMediaRegistry,
    DebugMessageSerializer,
    DebugSessionManager,
    serialize_parse_result,
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

        # Remove stale switches for routes that are no longer registered so
        # upgraded installations do not keep displaying retired platforms.
        platforms = config.get("platforms", {})
        if isinstance(platforms, dict):
            for retired_platform in ("tieba", "xigua", "pipixia", "weishi"):
                platforms.pop(retired_platform, None)
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
        self.debug_media = DebugMediaRegistry()
        self.debug_sessions = DebugSessionManager()
        self._register_debug_page_apis()

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        await self.debug_sessions.close()
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

    def _debug_mode_enabled(self) -> bool:
        debug = self.config.get("debug", {})
        if not isinstance(debug, dict):
            return False
        return parse_bool(debug.get("enabled", False), False)

    def _register_debug_page_apis(self) -> None:
        routes = (
            ("debug/status", self.debug_page_status, ["GET"], "Debug Page status"),
            ("debug/start", self.debug_page_start, ["POST"], "Start debug parse"),
            ("debug/events", self.debug_page_events, ["GET"], "Debug parse events"),
            ("debug/cancel", self.debug_page_cancel, ["POST"], "Cancel debug parse"),
            (
                "debug/media/<token>/preview",
                self.debug_page_media_preview,
                ["GET"],
                "Preview debug media",
            ),
            (
                "debug/media/<token>",
                self.debug_page_media,
                ["GET"],
                "Download debug media",
            ),
        )
        for route, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}",
                handler,
                methods,
                description,
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
        content_blocks = result.extra.get("text_card_flow")
        if not isinstance(content_blocks, (list, tuple)):
            content_blocks = None
        if not text and not title and not media_url and not content_blocks:
            text = (result.url or "").strip()
        if not text and not title and not media_url and not content_blocks:
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
                    repr(content_blocks),
                    author_badge or "",
                    comment_signature,
                    accent_color,
                    accent_soft,
                    accent_source,
                    "text_card_v14_ordered_content_flow",
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
                content_blocks=content_blocks,
            )

        return ImageContent(out_path)

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

    async def _render_and_send_text_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        original_message_reply,
    ) -> bool:
        try:
            card = await self._build_text_card_content(result, None)
        except Exception as exc:
            logger.warning(f"统一正文卡渲染失败: {exc}")
            return False
        if card is None:
            return False
        return await self._send_card_reply(
            event,
            card,
            original_message_reply=original_message_reply,
        )

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

    async def _send_delivery_plan_legacy(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        node_uin: str,
        node_name: str,
        original_message_reply,
    ) -> None:
        plan = result.delivery
        if plan is None:
            return

        batches = list(plan.batches)
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

        show_download_fail_tip = self._show_download_fail_tip()
        download_tasks: dict[
            int,
            asyncio.Task[tuple[MediaContent, Path | None, str | None]],
        ] = {}

        async def resolve_media(
            content: MediaContent,
        ) -> tuple[Path | None, str | None]:
            task = download_tasks.get(id(content))
            if task is None:
                task = asyncio.create_task(
                    self._download_content(content),
                    name=f"parser_x_delivery_{type(content).__name__}",
                )
                download_tasks[id(content)] = task
            _, path, error = await task
            return path, error

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

        async def send_batch(batch: DeliveryBatch) -> None:
            media_parts = [
                part for part in batch.parts if isinstance(part, MediaContent)
            ]
            resolved_media = await asyncio.gather(
                *(resolve_media(content) for content in media_parts)
            )
            path_map = {
                id(content): resolved
                for content, resolved in zip(
                    media_parts,
                    resolved_media,
                    strict=True,
                )
            }

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
                return

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
                return

            if len(segments) == 1 and isinstance(segments[0], Video):
                await self._send_video_segment(event, result, segments[0])
                return

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

        card_task: asyncio.Task[bool] | None = None
        concurrent_video_batches: list[DeliveryBatch] = []
        video_tasks: list[asyncio.Task[None]] = []
        if result.extra.get("render_text_card"):
            card_task = asyncio.create_task(
                self._render_and_send_text_card(
                    event,
                    result,
                    original_message_reply=original_message_reply,
                ),
                name="parser_x_delivery_card_send",
            )
            concurrent_video_batches = [
                batch
                for batch in batches
                if len(batch.parts) == 1
                and isinstance(batch.parts[0], (VideoContent, DynamicContent))
            ]
            video_tasks = [
                asyncio.create_task(
                    send_batch(batch),
                    name=f"parser_x_delivery_video_{index}",
                )
                for index, batch in enumerate(concurrent_video_batches)
            ]

        card_sent = await card_task if card_task is not None else False
        if card_sent:
            if result.extra.get("delivery_text_card_consume_non_video"):
                cleaned_batches: list[DeliveryBatch] = []
                for batch in batches:
                    parts = [
                        part
                        for part in batch.parts
                        if isinstance(part, (VideoContent, DynamicContent))
                    ]
                    if parts:
                        cleaned_batches.append(
                            DeliveryBatch(
                                parts,
                                mode=batch.mode,
                                reply_original=batch.reply_original,
                            )
                        )
                batches = cleaned_batches
            else:
                try:
                    batch_index = int(result.extra.get("delivery_text_card_batch", 0))
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
                            reply_original=original.reply_original,
                        )
                    else:
                        batches.pop(batch_index)

                embedded_image = (
                    result.contents[0]
                    if self._card_embeds_single_image(result)
                    else None
                )
                cleaned_batches = []
                for batch in batches:
                    parts = [part for part in batch.parts if part is not embedded_image]
                    if parts:
                        cleaned_batches.append(
                            DeliveryBatch(
                                parts,
                                mode=batch.mode,
                                reply_original=batch.reply_original,
                            )
                        )
                batches = cleaned_batches

        concurrent_video_content_ids = {
            id(batch.parts[0]) for batch in concurrent_video_batches
        }
        for batch in batches:
            if (
                len(batch.parts) == 1
                and isinstance(batch.parts[0], (VideoContent, DynamicContent))
                and id(batch.parts[0]) in concurrent_video_content_ids
            ):
                continue
            await send_batch(batch)

        if video_tasks:
            video_results = await asyncio.gather(*video_tasks, return_exceptions=True)
            for error in video_results:
                if isinstance(error, BaseException):
                    logger.warning(f"投递计划视频并发发送失败: {error}")

    async def _send_parse_result_legacy(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        show_download_fail_tip = self._show_download_fail_tip()

        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        def original_message_reply() -> Reply | None:
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id in (None, ""):
                return None
            return Reply(id=message_id)

        comment_factory = result.extra.get("comment_image_task_factory")
        comment_task: asyncio.Task[object] | None = None
        comment_started_at: float | None = None
        comment_timeout = self._bounded_timeout(
            result.extra.get("comment_timeout", 90),
            90,
            180,
        )
        if callable(comment_factory):
            comment_started_at = asyncio.get_running_loop().time()
            comment_task = asyncio.create_task(
                comment_factory(),
                name="parser_x_comment_image_build",
            )

        async def prepare_comment_segments() -> list[BaseMessageComponent]:
            if comment_task is None or comment_started_at is None:
                return []

            def remaining_timeout() -> float:
                elapsed = asyncio.get_running_loop().time() - comment_started_at
                return max(0.001, comment_timeout - elapsed)

            try:
                raw_contents = await asyncio.wait_for(
                    comment_task,
                    timeout=remaining_timeout(),
                )
            except asyncio.TimeoutError:
                logger.warning("评论区生成超时，已跳过发送")
                return []
            except Exception as exc:
                logger.warning(f"评论区生成失败: {exc}")
                return []

            if not isinstance(raw_contents, (list, tuple)):
                logger.warning("评论区生成结果格式无效，已跳过发送")
                return []
            comment_contents = [
                content for content in raw_contents if isinstance(content, MediaContent)
            ]
            if not comment_contents:
                return []

            try:
                download_results = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            self._download_content(content)
                            for content in comment_contents
                        )
                    ),
                    timeout=remaining_timeout(),
                )
            except asyncio.TimeoutError:
                logger.warning("评论区图片渲染超时，已跳过发送")
                return []
            except Exception as exc:
                logger.warning(f"评论区图片准备失败: {exc}")
                return []

            segments: list[BaseMessageComponent] = []
            for content, path, error in download_results:
                if error:
                    logger.warning(f"评论区图片下载失败: {error}")
                    continue
                if path and (segment := self._convert_to_seg(content, path)):
                    segments.append(segment)
            return segments

        async def process_comment_content() -> None:
            segments = await prepare_comment_segments()
            if not segments:
                return

            comment_label = f"{result.platform.display_name} · 热门评论"
            for offset in range(0, len(segments), 19):
                group = segments[offset : offset + 19]
                nodes = Nodes(
                    [
                        Node(
                            uin=node_uin,
                            name=node_name,
                            content=[Plain(comment_label)],
                        ),
                        *[
                            Node(uin=node_uin, name=node_name, content=[segment])
                            for segment in group
                        ],
                    ]
                )
                try:
                    await event.send(event.chain_result([nodes]))
                    continue
                except Exception as exc:
                    logger.warning(f"评论区合并转发失败，降级逐张发送: {exc}")

                try:
                    await event.send(event.chain_result([Plain(comment_label)]))
                except Exception as exc:
                    logger.debug(f"评论区标题发送失败: {exc}")
                for segment in group:
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"评论区图片逐张发送失败: {exc}")

        async def prepare_body_card_segment() -> BaseMessageComponent | None:
            try:
                card = await self._build_text_card_content(result, None)
            except Exception as exc:
                logger.warning(f"附属正文卡渲染失败: {exc}")
                return None
            if card is None:
                return None

            _, path, error = await self._download_content(card)
            if error or path is None:
                if error:
                    logger.warning(f"附属正文卡准备失败: {error}")
                return None
            return self._convert_to_seg(card, path)

        async def send_forward_segments(
            segments: list[BaseMessageComponent],
            *,
            failure_label: str,
        ) -> None:
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
                    continue
                except Exception as exc:
                    logger.warning(f"{failure_label}，降级逐段发送: {exc}")

                for segment in group:
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"附属内容逐段发送失败: {exc}")

        async def process_split_image_post() -> bool:
            source_contents = [
                content
                for content in result.contents
                if isinstance(content, (ImageContent, GraphicsContent))
            ]
            if not source_contents:
                return False

            card_segment_task = asyncio.create_task(
                prepare_body_card_segment(),
                name="parser_x_forward_body_card",
            )
            comment_segments_task = asyncio.create_task(
                prepare_comment_segments(),
                name="parser_x_forward_comments",
            )
            source_downloads = await asyncio.gather(
                *(self._download_content(content) for content in source_contents)
            )

            source_segments: list[BaseMessageComponent] = []
            source_errors: list[str] = []
            for content, path, error in source_downloads:
                if error:
                    source_errors.append(error.strip())
                    continue
                if path and (segment := self._convert_to_seg(content, path)):
                    source_segments.append(segment)

            single_source = len(source_contents) == 1
            if single_source and source_segments:
                chain: list[BaseMessageComponent] = []
                if (reply := original_message_reply()) is not None:
                    chain.append(reply)
                chain.append(source_segments[0])
                try:
                    await event.send(event.chain_result(chain))
                except Exception as exc:
                    logger.warning(f"单图引用发送失败，降级直接发送: {exc}")
                    try:
                        await event.send(event.chain_result([source_segments[0]]))
                    except Exception as fallback_exc:
                        logger.warning(f"单图直接发送仍失败: {fallback_exc}")

            body_card, comment_segments = await asyncio.gather(
                card_segment_task,
                comment_segments_task,
            )
            detail_segments: list[BaseMessageComponent] = []
            if not single_source:
                detail_segments.extend(source_segments)
            if body_card is not None:
                detail_segments.append(body_card)
            else:
                fallback = self._format_media_summary(result)
                if fallback:
                    detail_segments.append(Plain(fallback))
            detail_segments.extend(comment_segments)
            if show_download_fail_tip:
                detail_segments.extend(Plain(error) for error in source_errors if error)

            if detail_segments:
                await send_forward_segments(
                    detail_segments,
                    failure_label="图文附属合并转发失败",
                )
            return True

        async def process_main_content():
            if result.extra.get("image_post_card_in_forward"):
                if await process_split_image_post():
                    return True

            parsed_contents = tuple(result.contents)
            if getattr(result, "delivery", None) is not None:
                await self._send_delivery_plan(
                    event,
                    result,
                    node_uin=node_uin,
                    node_name=node_name,
                    original_message_reply=original_message_reply,
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
            has_video = any(
                isinstance(content, (VideoContent, DynamicContent))
                for content in result.contents
            )
            if (
                card_requested
                and has_video
                and result.extra.get("video_separate_from_card")
            ):

                async def send_card_with_fallback() -> None:
                    card_sent = await self._render_and_send_text_card(
                        event,
                        result,
                        original_message_reply=original_message_reply,
                    )
                    if card_sent:
                        return
                    summary = self._format_media_summary(result)
                    if not summary:
                        return
                    try:
                        await event.send(event.plain_result(summary))
                    except Exception as exc:
                        logger.warning(f"正文卡降级文本发送失败: {exc}")

                async def send_separate_media(content: MediaContent) -> None:
                    _, path, error = await self._download_content(content)
                    if error:
                        if show_download_fail_tip:
                            try:
                                await event.send(event.plain_result(error.strip()))
                            except Exception as exc:
                                logger.warning(f"媒体下载错误提示发送失败: {exc}")
                        return
                    if path is None:
                        return
                    segment = self._convert_to_seg(content, path)
                    if segment is None:
                        return
                    if isinstance(segment, Video):
                        await self._send_video_segment(event, result, segment)
                        return
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"视频附属媒体发送失败: {exc}")

                concurrent_tasks = [
                    asyncio.create_task(
                        send_card_with_fallback(),
                        name="parser_x_card_send",
                    ),
                    *[
                        asyncio.create_task(
                            send_separate_media(content),
                            name=f"parser_x_media_{index}",
                        )
                        for index, content in enumerate(result.contents)
                    ],
                ]
                task_results = await asyncio.gather(
                    *concurrent_tasks,
                    return_exceptions=True,
                )
                for error in task_results:
                    if isinstance(error, BaseException):
                        logger.warning(f"正文卡或媒体并发发送失败: {error}")
                return

            card_sent = False
            if card_requested:
                card_sent = await self._render_and_send_text_card(
                    event,
                    result,
                    original_message_reply=original_message_reply,
                )

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

        try:
            comments_joined = await process_main_content()
        except BaseException:
            if comment_task is not None and not comment_task.done():
                comment_task.cancel()
            if comment_task is not None:
                await asyncio.gather(comment_task, return_exceptions=True)
            raise

        if not comments_joined:
            await process_comment_content()

    async def _send_delivery_plan(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        node_uin: str,
        node_name: str,
        original_message_reply,
        include_videos: bool = True,
        include_non_videos: bool = True,
    ) -> None:
        """Send an explicit native delivery plan without rendering a body card."""
        plan = result.delivery
        if plan is None:
            return

        show_download_fail_tip = self._show_download_fail_tip()
        download_tasks: dict[
            int,
            asyncio.Task[tuple[MediaContent, Path | None, str | None]],
        ] = {}

        async def resolve_media(
            content: MediaContent,
        ) -> tuple[Path | None, str | None]:
            task = download_tasks.get(id(content))
            if task is None:
                task = asyncio.create_task(
                    self._download_content(content),
                    name=f"parser_x_native_{type(content).__name__}",
                )
                download_tasks[id(content)] = task
            _, path, error = await task
            return path, error

        async def send_individually(
            segments: list[BaseMessageComponent],
            *,
            reply_first: bool = False,
        ) -> None:
            for index, segment in enumerate(segments):
                if isinstance(segment, Video):
                    await self._send_video_segment(event, result, segment)
                    continue
                chain: list[BaseMessageComponent] = []
                if (
                    reply_first
                    and index == 0
                    and (reply := original_message_reply()) is not None
                ):
                    chain.append(reply)
                chain.append(segment)
                try:
                    await event.send(event.chain_result(chain))
                except Exception as exc:
                    logger.warning(f"原生内容逐段发送失败: {exc}")

        async def send_batch(batch: DeliveryBatch) -> None:
            parts = [
                part
                for part in batch.parts
                if (
                    include_videos
                    if isinstance(part, (VideoContent, DynamicContent))
                    else include_non_videos
                )
            ]
            media_parts = [
                part for part in parts if isinstance(part, MediaContent)
            ]
            resolved_media = await asyncio.gather(
                *(resolve_media(content) for content in media_parts)
            )
            path_map = {
                id(content): resolved
                for content, resolved in zip(
                    media_parts,
                    resolved_media,
                    strict=True,
                )
            }

            segments: list[BaseMessageComponent] = []
            for part in parts:
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
                return

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
                        logger.warning(f"原生合并转发发送失败，降级逐段发送: {exc}")
                        await send_individually(group)
                return

            if len(segments) == 1 and isinstance(segments[0], Video):
                await self._send_video_segment(event, result, segments[0])
                return

            chain: list[BaseMessageComponent] = []
            if batch.reply_original and (reply := original_message_reply()) is not None:
                chain.append(reply)
            chain.extend(segments)
            try:
                await event.send(event.chain_result(chain))
            except Exception as exc:
                logger.warning(f"原生消息链发送失败，降级逐段发送: {exc}")
                await send_individually(
                    segments,
                    reply_first=batch.reply_original,
                )

        for batch in plan.batches:
            await send_batch(batch)

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        """Deliver only source media, native rich posts, or plain text."""
        show_download_fail_tip = self._show_download_fail_tip()
        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        def original_message_reply() -> Reply | None:
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id in (None, ""):
                return None
            return Reply(id=message_id)

        media_contents: list[MediaContent] = []
        seen_media: set[int] = set()
        planned_media = result.delivery.media_contents() if result.delivery else []
        for content in [*result.contents, *planned_media]:
            if id(content) in seen_media:
                continue
            seen_media.add(id(content))
            media_contents.append(content)

        video_contents = [
            content
            for content in media_contents
            if isinstance(content, (VideoContent, DynamicContent))
        ]
        image_contents = [
            content
            for content in media_contents
            if isinstance(content, (ImageContent, GraphicsContent))
        ]
        other_contents = [
            content
            for content in media_contents
            if not isinstance(
                content,
                (VideoContent, DynamicContent, ImageContent, GraphicsContent),
            )
        ]

        async def send_media_content(content: MediaContent) -> None:
            _, path, error = await self._download_content(content)
            if error:
                if show_download_fail_tip:
                    try:
                        await event.send(event.plain_result(error.strip()))
                    except Exception as exc:
                        logger.warning(f"媒体下载错误提示发送失败: {exc}")
                return
            if path is None:
                return
            segment = self._convert_to_seg(content, path)
            if segment is None:
                return
            if isinstance(segment, Video):
                await self._send_video_segment(event, result, segment)
                return
            try:
                await event.send(event.chain_result([segment]))
            except Exception as exc:
                logger.warning(f"媒体发送失败: {exc}")

        async def send_forward_segments(
            segments: list[BaseMessageComponent],
            *,
            failure_label: str,
        ) -> None:
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
                    continue
                except Exception as exc:
                    logger.warning(f"{failure_label}，降级逐段发送: {exc}")

                for segment in group:
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"媒体逐段发送失败: {exc}")

        async def prepare_comment_segments(
            comment_task: asyncio.Task[object],
            started_at: float,
            timeout: float,
        ) -> list[BaseMessageComponent]:
            def remaining_timeout() -> float:
                elapsed = asyncio.get_running_loop().time() - started_at
                return max(0.001, timeout - elapsed)

            try:
                raw_contents = await asyncio.wait_for(
                    comment_task,
                    timeout=remaining_timeout(),
                )
            except asyncio.TimeoutError:
                logger.warning("评论区生成超时，已跳过发送")
                return []
            except Exception as exc:
                logger.warning(f"评论区生成失败: {exc}")
                return []

            if not isinstance(raw_contents, (list, tuple)):
                logger.warning("评论区生成结果格式无效，已跳过发送")
                return []
            comment_contents = [
                content for content in raw_contents if isinstance(content, MediaContent)
            ]
            if not comment_contents:
                return []

            try:
                download_results = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            self._download_content(content)
                            for content in comment_contents
                        )
                    ),
                    timeout=remaining_timeout(),
                )
            except asyncio.TimeoutError:
                logger.warning("评论区图片准备超时，已跳过发送")
                return []
            except Exception as exc:
                logger.warning(f"评论区图片准备失败: {exc}")
                return []

            segments: list[BaseMessageComponent] = []
            for content, path, error in download_results:
                if error:
                    logger.warning(f"评论区图片下载失败: {error}")
                    continue
                if path and (segment := self._convert_to_seg(content, path)):
                    segments.append(segment)
            return segments

        async def process_comment_content(
            comment_task: asyncio.Task[object],
            started_at: float,
            timeout: float,
        ) -> None:
            segments = await prepare_comment_segments(
                comment_task,
                started_at,
                timeout,
            )
            if not segments:
                return

            comment_label = f"{result.platform.display_name} · 热门评论"
            for offset in range(0, len(segments), 19):
                group = segments[offset : offset + 19]
                nodes = Nodes(
                    [
                        Node(
                            uin=node_uin,
                            name=node_name,
                            content=[Plain(comment_label)],
                        ),
                        *[
                            Node(uin=node_uin, name=node_name, content=[segment])
                            for segment in group
                        ],
                    ]
                )
                try:
                    await event.send(event.chain_result([nodes]))
                    continue
                except Exception as exc:
                    logger.warning(f"评论区合并转发失败，降级逐张发送: {exc}")

                try:
                    await event.send(event.chain_result([Plain(comment_label)]))
                except Exception as exc:
                    logger.debug(f"评论区标题发送失败: {exc}")
                for segment in group:
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"评论区图片逐张发送失败: {exc}")

        native_delivery = bool(result.extra.get("native_delivery")) or (
            result.platform.name in {"xiaoheihe", "miyoushe"}
            and result.delivery is not None
        )
        if native_delivery and result.delivery is not None:
            one_image_flow = bool(
                result.extra.get("render_text_card")
                and result.extra.get("delivery_text_card_consume_non_video")
            )

            async def send_one_image_or_native_fallback() -> None:
                card_sent = await self._render_and_send_text_card(
                    event,
                    result,
                    original_message_reply=original_message_reply,
                )
                if card_sent:
                    return
                await self._send_delivery_plan(
                    event,
                    result,
                    node_uin=node_uin,
                    node_name=node_name,
                    original_message_reply=original_message_reply,
                    include_videos=False,
                )

            if one_image_flow:
                concurrent_tasks: list[asyncio.Task[None]] = [
                    asyncio.create_task(
                        send_one_image_or_native_fallback(),
                        name="parser_x_native_one_image_delivery",
                    )
                ]
                if video_contents:
                    concurrent_tasks.append(
                        asyncio.create_task(
                            self._send_delivery_plan(
                                event,
                                result,
                                node_uin=node_uin,
                                node_name=node_name,
                                original_message_reply=original_message_reply,
                                include_non_videos=False,
                            ),
                            name="parser_x_native_video_delivery",
                        )
                    )
            else:
                concurrent_tasks = [
                    asyncio.create_task(
                        self._send_delivery_plan(
                            event,
                            result,
                            node_uin=node_uin,
                            node_name=node_name,
                            original_message_reply=original_message_reply,
                        ),
                        name="parser_x_native_delivery",
                    )
                ]

            comment_factory = result.extra.get("comment_image_task_factory")
            if video_contents and callable(comment_factory):
                comment_timeout = self._bounded_timeout(
                    result.extra.get("comment_timeout", 90),
                    90,
                    180,
                )
                comment_started_at = asyncio.get_running_loop().time()
                comment_task = asyncio.create_task(
                    comment_factory(),
                    name="parser_x_comment_image_build",
                )
                concurrent_tasks.append(
                    asyncio.create_task(
                        process_comment_content(
                            comment_task,
                            comment_started_at,
                            comment_timeout,
                        ),
                        name="parser_x_comment_send",
                    )
                )

            results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)
            for error in results:
                if isinstance(error, BaseException):
                    logger.warning(f"原生投递或评论区并发发送失败: {error}")
            return

        if video_contents:
            concurrent_tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(
                    send_media_content(content),
                    name=f"parser_x_video_send_{index}",
                )
                for index, content in enumerate(video_contents)
            ]

            comment_factory = result.extra.get("comment_image_task_factory")
            if callable(comment_factory):
                comment_timeout = self._bounded_timeout(
                    result.extra.get("comment_timeout", 90),
                    90,
                    180,
                )
                comment_started_at = asyncio.get_running_loop().time()
                comment_task = asyncio.create_task(
                    comment_factory(),
                    name="parser_x_comment_image_build",
                )
                concurrent_tasks.append(
                    asyncio.create_task(
                        process_comment_content(
                            comment_task,
                            comment_started_at,
                            comment_timeout,
                        ),
                        name="parser_x_comment_send",
                    )
                )

            results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)
            for error in results:
                if isinstance(error, BaseException):
                    logger.warning(f"视频或评论区并发发送失败: {error}")
            return

        if other_contents:
            results = await asyncio.gather(
                *(send_media_content(content) for content in other_contents),
                return_exceptions=True,
            )
            for error in results:
                if isinstance(error, BaseException):
                    logger.warning(f"非图文媒体发送失败: {error}")
            return

        if image_contents:
            download_results = await asyncio.gather(
                *(self._download_content(content) for content in image_contents)
            )
            segments: list[BaseMessageComponent] = []
            errors: list[str] = []
            for content, path, error in download_results:
                if error:
                    errors.append(error.strip())
                    continue
                if path and (segment := self._convert_to_seg(content, path)):
                    segments.append(segment)

            if len(image_contents) == 1 and segments:
                chain: list[BaseMessageComponent] = []
                if (reply := original_message_reply()) is not None:
                    chain.append(reply)
                chain.append(segments[0])
                try:
                    await event.send(event.chain_result(chain))
                except Exception as exc:
                    logger.warning(f"单图引用发送失败，降级直接发送: {exc}")
                    await event.send(event.chain_result([segments[0]]))
            elif segments:
                await send_forward_segments(
                    segments,
                    failure_label="多图合并转发发送失败",
                )

            if errors and show_download_fail_tip:
                try:
                    await event.send(event.plain_result("\n".join(errors)))
                except Exception as exc:
                    logger.warning(f"图片下载错误提示发送失败: {exc}")
            return

        text = str(result.text or result.title or result.extra_info or "").strip()
        if text:
            await event.send(event.plain_result(text.replace("@", "@\u200b")))

    # endregion

    # region 事件监听

    # region Canvas debug Page

    @staticmethod
    def _debug_page_owner() -> str | None:
        username = request.username
        plugin_name = request.plugin_name
        if plugin_name != PLUGIN_NAME or not isinstance(username, str):
            return None
        username = username.strip()
        return username or None

    async def debug_page_status(self):
        owner = self._debug_page_owner()
        if owner is None:
            return error_response("请通过 AstrBot 插件 Page 访问调试台", status_code=403)

        comments = self.config.get("comments", {})
        if not isinstance(comments, dict):
            comments = {}
        platforms = sorted(
            {parser.platform.display_name for parser in self.parser_map.values()}
        )
        enabled = self._debug_mode_enabled()
        return json_response(
            {
                "enabled": enabled,
                "exclusive": enabled,
                "active_sessions": self.debug_sessions.active_count,
                "platforms": platforms,
                "comments": {
                    key: parse_bool(value, False)
                    for key, value in comments.items()
                    if key
                    in {
                        "bilibili",
                        "douyin",
                        "weibo",
                        "xiaoheihe",
                        "miyoushe",
                    }
                },
            }
        )

    async def debug_page_start(self):
        owner = self._debug_page_owner()
        if owner is None:
            return error_response("请通过 AstrBot 插件 Page 访问调试台", status_code=403)
        if not self._debug_mode_enabled():
            return error_response(
                "调试模式尚未开启，请先在插件配置中打开调试开关",
                status_code=403,
            )

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象")
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("请输入需要解析的分享文本或链接")
        if len(text) > 20_000:
            return error_response("调试文本不能超过 20000 个字符")

        matches = self._collect_parser_matches(text)
        if not matches:
            return error_response("没有匹配到已启用的解析平台")

        session = await self.debug_sessions.create(
            owner=owner,
            runner=lambda emit: self._run_debug_page_parse(
                text,
                owner=owner,
                emit=emit,
            ),
        )
        return json_response(
            {
                "session_id": session.session_id,
                "match_count": len(matches),
            }
        )

    async def debug_page_events(self):
        owner = self._debug_page_owner()
        if owner is None:
            return error_response("请通过 AstrBot 插件 Page 访问调试台", status_code=403)
        if not self._debug_mode_enabled():
            return error_response("调试模式已关闭", status_code=403)

        session_id = request.query.get("session_id", "")
        session = self.debug_sessions.get(str(session_id or ""), owner=owner)
        if session is None:
            return error_response("调试会话不存在或已过期", status_code=404)
        return stream_response(
            self.debug_sessions.stream(session),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def debug_page_cancel(self):
        owner = self._debug_page_owner()
        if owner is None:
            return error_response("请通过 AstrBot 插件 Page 访问调试台", status_code=403)
        payload = await request.json(default={})
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        cancelled = await self.debug_sessions.cancel(
            str(session_id or ""),
            owner=owner,
        )
        if not cancelled:
            return error_response("调试会话不存在或已结束", status_code=404)
        return json_response({"cancelled": True})

    async def debug_page_media_preview(self, token: str):
        owner = self._debug_page_owner()
        if owner is None or not self._debug_mode_enabled():
            return error_response("调试媒体不可用", status_code=403)
        entry = self.debug_media.get(token, owner=owner)
        if entry is None:
            return error_response("媒体不存在或已过期", status_code=404)
        if entry.size > 16 * 1024 * 1024:
            return error_response("媒体过大，请使用下载按钮查看", status_code=413)

        raw = await asyncio.to_thread(entry.path.read_bytes)
        encoded = base64.b64encode(raw).decode("ascii")
        return json_response(
            {
                "data_url": f"data:{entry.mime};base64,{encoded}",
                "name": entry.name,
                "mime": entry.mime,
                "size": entry.size,
            }
        )

    async def debug_page_media(self, token: str):
        owner = self._debug_page_owner()
        if owner is None or not self._debug_mode_enabled():
            return error_response("调试媒体不可用", status_code=403)
        entry = self.debug_media.get(token, owner=owner)
        if entry is None:
            return error_response("媒体不存在或已过期", status_code=404)
        return file_response(
            entry.path,
            filename=entry.name,
            content_type=entry.mime,
        )

    async def _run_debug_page_parse(self, text: str, *, owner: str, emit) -> None:
        started_at = asyncio.get_running_loop().time()
        matches = self._collect_parser_matches(text)
        await emit(
            {
                "event": "started",
                "match_count": len(matches),
                "exclusive": True,
            }
        )

        completed = 0
        message_index = 0
        for match_index, (parser, keyword, searched) in enumerate(matches, start=1):
            platform_name = parser.platform.display_name
            await emit(
                {
                    "event": "match",
                    "index": match_index,
                    "platform": platform_name,
                    "url": searched.group(0).strip(),
                }
            )
            parser.source_text = text
            parse_started_at = asyncio.get_running_loop().time()
            try:
                result = await parser.parse(keyword, searched)
            except SkipParseException:
                await emit(
                    {
                        "event": "skipped",
                        "platform": platform_name,
                        "message": "该分享被解析器主动跳过",
                    }
                )
                continue
            except (SizeLimitException, ParseException) as exc:
                await emit(
                    {
                        "event": "error",
                        "platform": platform_name,
                        "message": str(exc),
                    }
                )
                continue
            except Exception as exc:
                logger.exception("Canvas 调试页解析发生未知错误")
                await emit(
                    {
                        "event": "error",
                        "platform": platform_name,
                        "message": f"解析发生未知错误：{exc}",
                    }
                )
                continue

            await emit(
                {
                    "event": "parsed",
                    "platform": platform_name,
                    "parse_ms": round(
                        (asyncio.get_running_loop().time() - parse_started_at)
                        * 1000
                    ),
                    "result": serialize_parse_result(result),
                }
            )

            serializer = DebugMessageSerializer(self.debug_media, owner=owner)
            capture_event = DebugCaptureEvent(
                emit=emit,
                serializer=serializer,
                started_at=started_at,
                platform=platform_name,
                initial_message_index=message_index,
            )
            try:
                await self._send_parse_result(capture_event, result)
            except Exception as exc:
                logger.exception("Canvas 调试页模拟投递失败")
                await emit(
                    {
                        "event": "error",
                        "platform": platform_name,
                        "message": f"模拟投递失败：{exc}",
                    }
                )
                continue

            message_index = capture_event.message_count
            completed += 1
            await emit(
                {
                    "event": "delivered",
                    "platform": platform_name,
                    "elapsed_ms": round(
                        (asyncio.get_running_loop().time() - started_at) * 1000
                    ),
                }
            )

        await emit(
            {
                "event": "done",
                "completed": completed,
                "match_count": len(matches),
                "elapsed_ms": round(
                    (asyncio.get_running_loop().time() - started_at) * 1000
                ),
            }
        )

    # endregion

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

    def _collect_parser_matches(
        self,
        text: str,
    ) -> list[tuple[BaseParser, str, re.Match[str]]]:
        matches: list[tuple[int, str, re.Match[str]]] = []
        for keyword, pattern in self.key_pattern_list:
            if keyword not in text:
                continue
            for searched in pattern.finditer(text):
                if self._should_ignore_live_share(
                    keyword,
                    searched.group(0),
                    text,
                ):
                    continue
                matches.append((searched.start(), keyword, searched))

        matches.sort(key=lambda item: (item[0], -len(item[1])))
        processed_matches: set[tuple[str, str]] = set()
        accepted_spans: list[tuple[int, int]] = []
        accepted: list[tuple[BaseParser, str, re.Match[str]]] = []

        for _, keyword, searched in matches:
            parser = self.parser_map.get(keyword)
            if parser is None:
                continue

            span = (searched.start(), searched.end())
            if any(
                span[0] < accepted_end and accepted_start < span[1]
                for accepted_start, accepted_end in accepted_spans
            ):
                continue

            raw_match = searched.group(0).strip()
            match_url = re.sub(
                r"^https?://",
                "",
                raw_match,
                flags=re.IGNORECASE,
            )
            match_url = match_url.rstrip(").,;!?，。；！？）]")
            match_key = (parser.platform.name, match_url.casefold())
            if match_key in processed_matches:
                continue

            processed_matches.add(match_key)
            accepted_spans.append(span)
            accepted.append((parser, keyword, searched))

        return accepted

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if self._debug_mode_enabled():
            return
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

        collected_matches = self._collect_parser_matches(text)
        if not collected_matches:
            return

        if isinstance(event, AiocqhttpMessageEvent):
            if hasattr(event.message_obj, "message_id"):
                asyncio.create_task(
                    self.arbiter.notify(event.bot, event.message_obj.message_id)
                )

        for parser, keyword, searched in collected_matches:
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
