import asyncio
import base64
import hashlib
import json
import re
import uuid
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
from .core.clean import CacheCleaner
from .core.comment_canvas import CommentDocument
from .core.card_guard import ignored_component_reason, is_card_segment
from .core.comment_settings import parse_bool
from .core.config_policy import migrate_config
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
    debug_issue_event,
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
from .core.live_guard import (
    is_live_share_caption,
    is_live_url,
    is_short_link,
    looks_like_live_card,
    looks_like_live_share,
)
from .core.nonwork_guard import is_nonwork_url
from .core.parsers import BaseParser
from .core.parsers.bilibili.comment_canvas import BiliCommentDocument
from .core.result_cache import ParseResultCache, _config_signature
from .core.async_cache import AsyncSingleFlight
from .core.unified_renderer import UnifiedCardRenderer
from .core.utils import exec_ffmpeg_cmd, extract_json_url, pick_supported_share_url

PLUGIN_NAME = "astrbot_plugin_parser_x"


class _GeneratedCardContent(ImageContent):
    """Delivery marker: generated cards keep their creation-time retention."""


def _prepare_media_file(path: Path, *, touch: bool = True) -> bool:
    """Check a resolved media path right before it becomes a message segment.

    Parses resolve downloads to ``Path`` objects that can outlive the file: the
    24h cache cleaner, a transcode/re-download, or manual cleanup may remove it
    between resolution and send. Handing such a path to ``event.send`` surfaces
    as a Node-side ``ENOENT: no such file or directory, realpath`` ActionFailed,
    which users read as a delivery problem rather than an expired cache entry.
    """
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    try:
        # Touching keeps media that is still being sent inside the cleaner's
        # retention window.
        if touch:
            path.touch()
    except OSError:
        pass
    return True


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

        migrate_config(config)
        self.config.save_config()

        self.parser_map: dict[str, BaseParser] = {}
        self.result_cache = ParseResultCache()
        self.comment_document_cache = AsyncSingleFlight()
        self.comment_render_cache = AsyncSingleFlight()
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.downloader = Downloader(config)
        self.arbiter = EmojiLikeArbiter()
        self.cleaner = CacheCleaner(self.context, self.config)
        self.render_service = HtmlRenderService.from_config(config)
        self.text_renderer = UnifiedCardRenderer(self.render_service)
        self.debug_media = DebugMediaRegistry()
        self.debug_sessions = DebugSessionManager()
        self._register_debug_page_apis()

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        await self.debug_sessions.close()
        for name in ("comment_render_cache", "comment_document_cache", "result_cache"):
            if cache := getattr(self, name, None):
                await cache.close()
        await self.downloader.close()
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        await self.cleaner.stop()
        await self.render_service.aclose()

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
        return True

    def _behavior(self) -> dict:
        behavior = self.config.get("behavior", {})
        return behavior if isinstance(behavior, dict) else {}

    def _response_enabled(self) -> bool:
        return bool(self._behavior().get("response_enabled", True))

    def _should_respond(self, event: AstrMessageEvent) -> bool:
        return self._response_enabled() and self._session_key(event) not in self._disabled_sessions()

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        origin = event.unified_msg_origin
        parts = origin.rsplit(":", 2)
        if len(parts) == 3 and parts[1] == "GroupMessage" and parts[2].isdigit():
            return parts[2]
        return origin

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
        return True

    def _debug_mode_enabled(self) -> bool:
        config = getattr(self, "config", {})
        debug = config.get("debug", {}) if hasattr(config, "get") else {}
        if not isinstance(debug, dict):
            return False
        return parse_bool(debug.get("enabled", False), False)


    def _register_debug_page_apis(self) -> None:
        routes = (
            ("debug/status", self.debug_page_status, ["GET"], "Debug Page status"),
            ("debug/start", self.debug_page_start, ["POST"], "Start debug parse"),
            ("debug/mode", self.debug_page_mode, ["POST"], "Set debug-only mode"),
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
        if isinstance(cont, DynamicContent) and cont.fallback_image is not None:
            try:
                path = await cont.get_path()
                if _prepare_media_file(path):
                    return cont, path, None
            except Exception:
                pass
            logger.debug("实况片段不可用，使用对应静态图")
            return await self._download_content(cont.fallback_image)
        try:
            path = await cont.get_path()
            if not _prepare_media_file(path, touch=not isinstance(cont, _GeneratedCardContent)):
                # A resolved path can outlive its file: the 24h cache cleaner,
                # a concurrent re-download, or an evicted temp file remove it
                # after the parse finished. Sending it raises Node-side
                # "ENOENT ... realpath" from the bot backend.
                return cont, None, f"[下载失败: 媒体文件已不存在: {path.name}]"
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

    @staticmethod
    def _comment_forward_segments(result: ParseResult, card_segment) -> list:
        """Only video works get a separate comment-forward preview heading."""
        if result.has_video and callable(result.extra.get("text_card_comment_factory")):
            return [Plain(f"{result.platform.display_name} · 热门评论"), card_segment]
        return [card_segment]

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
        for name in ("comment_document_cache", "comment_render_cache"):
            if not hasattr(self, name):
                setattr(self, name, AsyncSingleFlight())
        # Do not fingerprint factory objects: parsers rebuild closures for the
        # same work. Include every renderer input and settings, never raw cookies.
        extra = {
            key: value for key, value in result.extra.items()
            if not callable(value) and not isinstance(value, asyncio.Future)
        }
        identity = [
            _config_signature(getattr(self, "config", {})), include_comments,
            callable(result.extra.get("text_card_comment_factory")),
            result.platform.name, result.platform.display_name,
            result.author.name if result.author else None,
            result.title, result.text, result.extra_info, result.timestamp, result.url, extra,
            "text_card_unified_v13_preserve_mentions",
        ]
        cache_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()

        card, _ = await self.comment_render_cache.get(
            cache_key,
            lambda: self._build_text_card_content_uncached(
                result, include_comments=include_comments, cache_key=cache_key
            ),
        )
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
        try:
            if stable_path.is_file() and stable_path.stat().st_size > 0:
                return ImageContent(stable_path), True
        except OSError:
            pass

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
                logger.warning(f"评论区获取失败，正文卡照常渲染: {exc}")
                comment_document = None

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
            emotes=result.extra.get("card_emotes"),
            content_blocks=content_blocks,
            content_layout=result.extra.get("text_card_layout"),
            author_meta=result.extra.get("text_card_meta"),
            comment_document=comment_document,
        )

        if complete and out_path.is_file() and out_path.stat().st_size > 0:
            out_path.replace(stable_path)
            out_path = stable_path
        return ImageContent(out_path), complete

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
        as_forward: bool = False,
        node_uin: str | None = None,
        node_name: str | None = None,
    ) -> bool:
        """Send the unified long card, optionally as a single-node forward."""
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

        if as_forward:
            nodes = Nodes(
                [
                    Node(
                        uin=node_uin or str(event.get_sender_id()),
                        name=node_name or event.get_sender_name() or "R-Parser",
                        content=[segment],
                    )
                ]
            )
            try:
                await event.send(event.chain_result([nodes]))
                return True
            except Exception as exc:
                logger.warning(f"统一长卡合并转发失败，降级引用发送: {exc}")

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
        as_forward: bool = False,
        node_uin: str | None = None,
        node_name: str | None = None,
    ) -> bool:
        try:
            card = await self._build_text_card_content(result)
        except Exception as exc:
            logger.warning(f"统一正文卡渲染失败: {exc}")
            return False
        if card is None:
            return False
        return await self._send_card_reply(
            event,
            card,
            original_message_reply=original_message_reply,
            as_forward=as_forward,
            node_uin=node_uin,
            node_name=node_name,
        )

    async def _remux_fresh_copy(self, input_path: Path) -> Path:
        """无损重新封装一份新文件：内容不变，容器化字节不同，md5 随之改变。

        QQ 侧会按文件 md5 复用已上传的媒体资源；当那份缓存资源坏掉时，
        重发同一字节只会被静默丢弃，换一份新字节即可绕过。
        """
        output_path = input_path.with_name(
            f"{input_path.stem}_fresh_{uuid.uuid4().hex[:8]}{input_path.suffix or '.mp4'}"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(input_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        await exec_ffmpeg_cmd(cmd)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 重新封装未生成有效文件")
        return output_path

    async def _resend_with_fresh_media(
        self,
        event: AstrMessageEvent,
        segment: Video,
        *,
        prefer_transcode: bool,
    ) -> bool:
        """换一份新文件重发一次；成功返回 True，并把新字节回填到原路径自愈缓存。"""
        path_text = getattr(segment, "file", None)
        if not path_text:
            return False
        input_path = Path(path_text)
        if not input_path.exists():
            logger.warning(f"媒体文件不存在，跳过重建重试: {input_path}")
            return False

        steps = ("transcode", "remux") if prefer_transcode else ("remux", "transcode")
        for step in steps:
            label = "转码" if step == "transcode" else "重新封装"
            try:
                new_path = (
                    await self._transcode_to_h264(input_path)
                    if step == "transcode"
                    else await self._remux_fresh_copy(input_path)
                )
            except Exception as exc:
                logger.warning(f"视频{label}失败: {exc}")
                continue
            try:
                await event.send(event.chain_result([Video(str(new_path))]))
            except Exception as exc:
                logger.warning(f"视频{label}后重发失败: {exc}")
                continue
            logger.info(f"媒体发送失败后{label}重发成功: {new_path.name}")
            try:
                await asyncio.to_thread(new_path.replace, input_path)
            except Exception as exc:  # 自愈失败不影响已经发出的消息
                logger.debug(f"重发成功后回填缓存失败: {exc}")
                try:
                    await asyncio.to_thread(new_path.unlink)
                except Exception:
                    pass
            return True
        return False

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
            prefer_transcode = any(
                marker in error_text for marker in ("rich media", "1200", "Timeout")
            )
            if await self._resend_with_fresh_media(
                event, segment, prefer_transcode=prefer_transcode
            ):
                return

            logger.warning(f"视频发送失败: {exc}")
            try:
                await event.send(
                    event.plain_result(f"⚠️ 媒体发送失败\n🔗 原链接: {result.url or '未知'}")
                )
            except Exception as notice_exc:
                logger.warning(f"媒体发送失败提示也未能发出: {notice_exc}")



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
        lead_card_segment: BaseMessageComponent | None = None,
        trailing_segments: list[BaseMessageComponent] | None = None,
    ) -> None:
        """Send an explicit native delivery plan without rendering a body card.

        ``lead_card_segment`` / ``trailing_segments`` 让图文帖的正文卡与评论区
        作为合并转发的首/尾节点，与原图处在同一条转发记录里。
        """
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
        ) -> tuple[MediaContent, Path | None, str | None]:
            task = download_tasks.get(id(content))
            if task is None:
                task = asyncio.create_task(
                    self._download_content(content),
                    name=f"parser_x_native_{type(content).__name__}",
                )
                download_tasks[id(content)] = task
            return await task

        async def send_individually(
            segments: list[BaseMessageComponent],
            *,
            reply_first: bool = False,
        ) -> None:
            for index, segment in enumerate(segments):
                if isinstance(segment, Video):
                    try:
                        await self._send_video_segment(event, result, segment)
                    except Exception as exc:
                        logger.warning(f"视频发送失败，继续后续媒体: {exc}")
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

        async def resolve_batch_segments(
            parts: list[object],
        ) -> list[BaseMessageComponent]:
            """把一批纯图文内容转成消息段，供合并转发统一收割。"""
            media_parts = [part for part in parts if isinstance(part, MediaContent)]
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
                content, path, error = path_map.get(id(part), (part, None, None))
                if error or path is None:
                    if error and show_download_fail_tip:
                        segments.append(Plain(error.strip()))
                    continue
                segment = self._convert_to_seg(content, path)
                if segment is not None:
                    segments.append(segment)
            return segments

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
            media_parts = [part for part in parts if isinstance(part, MediaContent)]
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

                resolved, path, error = path_map.get(id(part), (part, None, None))
                if error:
                    if show_download_fail_tip:
                        segments.append(Plain(error.strip()))
                    continue
                if path and (segment := self._convert_to_seg(resolved, path)):
                    segments.append(segment)

            if not segments:
                return

            if not include_non_videos:
                # Split video batches are direct sends even if the source plan
                # grouped a mixed image/video post into a forward.
                await send_individually(segments)
                return

            if batch.mode == "forward":
                if segments:
                    group = segments
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
                try:
                    await self._send_video_segment(event, result, segments[0])
                except Exception as exc:
                    logger.warning(f"视频发送失败，继续后续媒体: {exc}")
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

        # 图文帖：正文卡 + 原图 + 评论必须落在同一条合并转发里。
        # 所以卡片存在时先把计划里的非视频段收拢，统一拼成一条转发记录再发；
        # 视频仍走各自独立的发送路径，不参与这条转发。
        card_in_forward = lead_card_segment is not None
        collected_segments: list[BaseMessageComponent] = []
        if card_in_forward:
            collected_segments.extend(self._comment_forward_segments(result, lead_card_segment))

        for batch in plan.batches:
            if not card_in_forward:
                await send_batch(batch)
                continue
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
                part
                for part in parts
                if isinstance(part, (VideoContent, DynamicContent))
            ]
            if media_parts:
                await send_batch(batch)
                continue
            collected_segments.extend(await resolve_batch_segments(parts))

        if card_in_forward:
            collected_segments.extend(trailing_segments or [])
            if collected_segments:
                group = collected_segments
                nodes = Nodes(
                    [
                        Node(uin=node_uin, name=node_name, content=[segment])
                        for segment in group
                    ]
                )
                try:
                    await event.send(event.chain_result([nodes]))
                    return
                except Exception as exc:
                    logger.warning(f"图文合并转发失败，降级逐段发送: {exc}")
                await send_individually(group)
        elif trailing_segments:
            if trailing_segments:
                group = trailing_segments
                nodes = Nodes(
                    [
                        Node(uin=node_uin, name=node_name, content=[segment])
                        for segment in group
                    ]
                )
                try:
                    await event.send(event.chain_result([nodes]))
                    return
                except Exception as exc:
                    logger.warning(f"评论区合并转发失败，降级逐段发送: {exc}")
                for segment in group:
                    await event.send(event.chain_result([segment]))

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        """Deliver source media and one unified body/comment card per parse."""
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
            content, path, error = await self._download_content(content)
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
            if segments:
                group = segments
                nodes = Nodes(
                    [
                        Node(uin=node_uin, name=node_name, content=[segment])
                        for segment in group
                    ]
                )
                try:
                    await event.send(event.chain_result([nodes]))
                    return
                except Exception as exc:
                    logger.warning(f"{failure_label}，降级逐段发送: {exc}")

                for segment in group:
                    if isinstance(segment, Video):
                        await self._send_video_segment(event, result, segment)
                        continue
                    try:
                        await event.send(event.chain_result([segment]))
                    except Exception as exc:
                        logger.warning(f"媒体逐段发送失败: {exc}")


        async def prepare_media_segments(
            contents: list[MediaContent],
        ) -> tuple[list[BaseMessageComponent], list[str]]:
            download_results = await asyncio.gather(
                *(self._download_content(content) for content in contents)
            )
            segments: list[BaseMessageComponent] = []
            errors: list[str] = []
            for content, path, error in download_results:
                if error:
                    errors.append(error.strip())
                    continue
                if path and (segment := self._convert_to_seg(content, path)):
                    segments.append(segment)
            return segments, errors

        async def prepare_body_card_segment() -> BaseMessageComponent | None:
            try:
                card = await self._build_text_card_content(result)
            except Exception as exc:
                logger.warning(f"正文卡渲染失败: {exc}")
                return None
            if card is None:
                return None
            _, path, error = await self._download_content(card)
            if error or path is None:
                if error:
                    logger.warning(f"正文卡准备失败: {error}")
                return None
            return self._convert_to_seg(card, path)

        comments = getattr(self, "config", {}).get("comments", {})
        cards_enabled = not isinstance(comments, dict) or parse_bool(
            comments.get("enabled", True), True
        )
        if not cards_enabled and media_contents:
            # Preserve the source body, not the platform/author/title/link summary.
            has_native_text = result.delivery is not None and any(
                isinstance(part, str) and part.strip()
                for batch in result.delivery.batches for part in batch.parts
            )
            if not has_native_text and (body := str(result.text or "").strip()):
                try:
                    await event.send(event.plain_result(body.replace("@", "@\u200b")))
                except Exception as exc:
                    logger.warning(f"原始正文发送失败，继续原始媒体: {exc}")
        native_delivery = bool(result.extra.get("native_delivery"))
        if native_delivery and result.delivery is not None:
            # 图文帖：内联评论的正文卡 → 原图，一起进入合并转发。
            # 只有视频仍然直发。卡片不再单独引用发送，避免「一条卡片 + 一堆散图」。
            # 只要出了正文卡，卡片就必须进合并转发（图文帖、视频帖都一样）；
            # 视频本身仍由下面的独立任务直发，不会和卡片混在一条里。
            image_post_flow = cards_enabled and bool(result.extra.get("render_text_card"))

            async def build_card_segment_async() -> BaseMessageComponent | None:
                return await prepare_body_card_segment()

            async def send_one_image_or_native_fallback() -> None:
                card_segment = await build_card_segment_async()
                if video_delivery_task is not None:
                    await asyncio.gather(video_delivery_task, return_exceptions=True)
                if card_segment is None:
                    # Media-only native plans (e.g. Weibo) omit the summary
                    # because the card normally carries it. Restore it on failure.
                    has_native_text = any(
                        isinstance(part, str) and part.strip()
                        for batch in result.delivery.batches for part in batch.parts
                    )
                    if not has_native_text and (summary := self._format_media_summary(result)):
                        try:
                            await event.send(event.plain_result(summary))
                        except Exception as exc:
                            logger.warning(f"正文降级发送失败，继续原始媒体: {exc}")
                    await self._send_delivery_plan(
                        event,
                        result,
                        node_uin=node_uin,
                        node_name=node_name,
                        original_message_reply=original_message_reply,
                        include_videos=False,
                    )
                    return
                await self._send_delivery_plan(
                    event,
                    result,
                    node_uin=node_uin,
                    node_name=node_name,
                    original_message_reply=original_message_reply,
                    include_videos=False,
                    lead_card_segment=card_segment,
                )

            video_delivery_task: asyncio.Task[None] | None = None
            if image_post_flow:
                if video_contents:
                    video_delivery_task = asyncio.create_task(
                        self._send_delivery_plan(
                            event, result, node_uin=node_uin, node_name=node_name,
                            original_message_reply=original_message_reply,
                            include_non_videos=False,
                        ),
                        name="parser_x_native_video_delivery",
                    )
                concurrent_tasks = [
                    asyncio.create_task(send_one_image_or_native_fallback(),
                                        name="parser_x_native_card_delivery")
                ]
                if video_delivery_task is not None:
                    concurrent_tasks.append(video_delivery_task)
            else:
                concurrent_tasks = [asyncio.create_task(
                    self._send_delivery_plan(
                        event, result, node_uin=node_uin, node_name=node_name,
                        original_message_reply=original_message_reply,
                    ), name="parser_x_native_delivery")]

            try:
                results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)
                for error in results:
                    if isinstance(error, BaseException):
                        logger.warning(f"原生投递并发发送失败: {type(error).__name__}")
            finally:
                for task in concurrent_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*concurrent_tasks, return_exceptions=True)
            return

        # Disabled cards are not render failures. Keep text-only fallback output,
        # but do not synthesize a replacement summary for source media.
        card_requested = bool(result.extra.get("render_text_card")) and (
            cards_enabled or not media_contents
        )
        if result.delivery is not None and not card_requested:
            await self._send_delivery_plan(
                event,
                result,
                node_uin=node_uin,
                node_name=node_name,
                original_message_reply=original_message_reply,
            )
            return

        video_delivery_tasks: list[asyncio.Task[None]] = []
        card_preparation_tasks: list[asyncio.Task] = []

        async def process_main_content() -> None:
            if not media_contents:
                if card_requested:
                    card_sent = await self._render_and_send_text_card(
                        event,
                        result,
                        original_message_reply=original_message_reply,
                        as_forward=True,
                        node_uin=node_uin,
                        node_name=node_name,
                    )
                    if card_sent:
                        return
                    fallback = self._format_text_fallback(result)
                else:
                    fallback = (
                        str(result.text or result.title or result.extra_info or "")
                        .strip()
                        .replace("@", "@\u200b")
                    )
                if fallback:
                    await event.send(event.plain_result(fallback))
                return

            # 单图帖也要带正文卡一起进合并转发，否则会变成「一条卡片 + 一张散图」。
            # 只有真正没有卡片可渲染的纯图片结果才退化成直发。
            combine_in_forward = bool(
                image_contents
                and (
                    card_requested
                    or video_contents
                    or other_contents
                    or len(image_contents) > 1
                )
            )
            if combine_in_forward:
                card_task = (
                    asyncio.create_task(
                        prepare_body_card_segment(),
                        name="parser_x_main_forward_card",
                    )
                    if card_requested
                    else None
                )
                if card_task is not None:
                    card_preparation_tasks.append(card_task)
                media_segments, errors = await prepare_media_segments(
                    [content for content in media_contents if content not in video_contents]
                )
                card_segment = await card_task if card_task is not None else None
                segments: list[BaseMessageComponent] = []
                if card_segment is not None:
                    segments.append(card_segment)
                elif cards_enabled and (card_requested or result.extra.get("send_text")) and (
                    summary := self._format_media_summary(result)
                ):
                    segments.append(Plain(summary))
                segments.extend(media_segments)
                if show_download_fail_tip:
                    segments.extend(Plain(error) for error in errors if error)
                if segments:
                    if video_delivery_tasks:
                        await asyncio.gather(*video_delivery_tasks, return_exceptions=True)
                    await send_forward_segments(
                        segments,
                        failure_label="正文与混合媒体合并转发发送失败",
                    )
                return

            if image_contents:
                card_task = (
                    asyncio.create_task(
                        prepare_body_card_segment(),
                        name="parser_x_single_image_card",
                    )
                    if card_requested
                    else None
                )
                if card_task is not None:
                    card_preparation_tasks.append(card_task)
                media_segments, errors = await prepare_media_segments(image_contents)
                card_segment = await card_task if card_task is not None else None
                segments: list[BaseMessageComponent] = []
                if card_segment is not None:
                    segments.append(card_segment)
                elif cards_enabled and (card_requested or result.extra.get("send_text")) and (
                    summary := self._format_media_summary(result)
                ):
                    segments.append(Plain(summary))
                segments.extend(media_segments)
                if show_download_fail_tip:
                    segments.extend(Plain(error) for error in errors if error)
                # 单图帖也走合并转发：正文卡与源图在同一条记录里，不再引用原消息。
                if segments:
                    if video_delivery_tasks:
                        await asyncio.gather(*video_delivery_tasks, return_exceptions=True)
                    await send_forward_segments(
                        segments,
                        failure_label="正文卡与单图合并转发发送失败",
                    )
                return

            async def send_card_with_fallback() -> None:
                if not card_requested:
                    return
                # Render concurrently, then gate only delivery on completed video sends.
                card_segment = await prepare_body_card_segment()
                if video_delivery_tasks:
                    await asyncio.gather(*video_delivery_tasks, return_exceptions=True)
                if card_segment is not None:
                    try:
                        await event.send(event.chain_result([Nodes([
                            Node(uin=node_uin, name=node_name, content=[segment])
                            for segment in self._comment_forward_segments(result, card_segment)
                        ])]))
                        return
                    except Exception as exc:
                        logger.warning(f"统一长卡合并转发失败，降级引用发送: {exc}")
                    reply = original_message_reply()
                    chain = ([reply] if reply is not None else []) + [card_segment]
                    try:
                        await event.send(event.chain_result(chain))
                        return
                    except Exception as exc:
                        logger.warning(f"统一长卡引用发送失败: {exc}")
                summary = self._format_media_summary(result)
                if summary:
                    await event.send(event.plain_result(summary))
                return
            tasks: list[asyncio.Task[None]] = []
            if card_requested:
                tasks.append(
                    asyncio.create_task(
                        send_card_with_fallback(),
                        name="parser_x_body_card_send",
                    )
                )
            tasks.extend(
                asyncio.create_task(
                    send_media_content(content),
                    name=f"parser_x_media_send_{index}",
                )
                for index, content in enumerate(media_contents) if content not in video_contents
            )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for error in results:
                if isinstance(error, BaseException):
                    logger.warning(f"正文卡或媒体并发发送失败: {type(error).__name__}")

        video_delivery_tasks = [
            asyncio.create_task(send_media_content(content),
                                name=f"parser_x_video_send_{index}")
            for index, content in enumerate(video_contents)
        ]
        main_task = asyncio.create_task(process_main_content(),
                                        name="parser_x_main_content_send")
        tasks = [*video_delivery_tasks, main_task]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for error in results:
                if isinstance(error, BaseException):
                    logger.warning(f"正文卡或媒体发送失败: {type(error).__name__}")
        finally:
            pending_tasks = [*tasks, *card_preparation_tasks]
            for task in pending_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    @staticmethod
    def _debug_page_owner() -> str | None:
        username = request.username
        plugin_name = request.plugin_name
        if plugin_name != PLUGIN_NAME or not isinstance(username, str):
            return None
        username = username.strip()
        return username or None

    @staticmethod
    def _debug_page_error_response(
        *,
        code: str,
        title: str,
        stage: str,
        message: str,
        action: str,
        status_code: int = 400,
    ):
        issue = debug_issue_event(
            code=code,
            title=title,
            stage=stage,
            message=message,
            action=action,
        )
        return error_response(
            f"{issue['title']}：{issue['message']} 建议：{issue['action']}",
            status_code=status_code,
            data={key: value for key, value in issue.items() if key != "event"},
        )

    async def debug_page_status(self):
        owner = self._debug_page_owner()
        if owner is None:
            return self._debug_page_error_response(
                code="page_access_required",
                title="无法访问调试接口",
                stage="访问",
                message="当前请求不是从 Parser X 插件页面发起的。",
                action="请返回 AstrBot 插件详情页并重新打开解析调试台。",
                status_code=403,
            )

        comments = self.config.get("comments", {})
        if not isinstance(comments, dict):
            comments = {}
        platforms = sorted(
            {parser.platform.display_name for parser in self.parser_map.values()}
        )
        enabled = self._debug_mode_enabled()
        return json_response(
            {
                "available": True,
                "enabled": enabled,
                "exclusive": enabled,
                "debug_only": enabled,
                "active_sessions": self.debug_sessions.active_count,
                "platforms": platforms,
                "comments": {"enabled": parse_bool(comments.get("enabled", True), True)},
            }
        )

    async def debug_page_mode(self):
        if self._debug_page_owner() is None:
            return self._debug_page_error_response(
                code="page_access_required", title="无法修改调试模式", stage="访问",
                message="当前请求不是从 Parser X 插件页面发起的。",
                action="请返回插件页面操作。", status_code=403,
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return self._debug_page_error_response(
                code="invalid_request_body", title="无法修改调试模式", stage="输入",
                message="enabled 必须为布尔值。", action="请刷新调试页后重试。", status_code=400,
            )
        previous = self.config.get("debug", {})
        self.config["debug"] = {"enabled": payload["enabled"]}
        try:
            self.config.save_config()
        except Exception:
            self.config["debug"] = previous
            raise
        return json_response({"debug_only": payload["enabled"], "exclusive": payload["enabled"]})

    async def debug_page_start(self):
        owner = self._debug_page_owner()
        if owner is None:
            return self._debug_page_error_response(
                code="page_access_required",
                title="无法启动解析",
                stage="访问",
                message="当前请求不是从 Parser X 插件页面发起的。",
                action="请返回 AstrBot 插件详情页并重新打开解析调试台。",
                status_code=403,
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._debug_page_error_response(
                code="invalid_request_body",
                title="无法读取输入",
                stage="输入",
                message="调试请求格式无效。",
                action="请刷新页面后重新输入分享文本或链接。",
            )
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._debug_page_error_response(
                code="input_required",
                title="没有可解析的内容",
                stage="输入",
                message="输入框中没有分享文本或链接。",
                action="请粘贴原本要发送到 QQ 的完整分享内容。",
            )
        if len(text) > 20_000:
            return self._debug_page_error_response(
                code="input_too_long",
                title="输入内容过长",
                stage="输入",
                message="调试文本超过 20000 个字符。",
                action="请删减无关文本，保留需要解析的分享链接后重试。",
            )

        matches = self._collect_parser_matches(text)
        if not matches:
            return self._debug_page_error_response(
                code="platform_not_matched",
                title="未识别到可解析链接",
                stage="识别",
                message="输入内容没有匹配到当前已启用的平台。",
                action="请检查链接是否完整，并确认对应平台已在插件配置中开启。",
            )

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
            return self._debug_page_error_response(
                code="page_access_required",
                title="无法连接消息通道",
                stage="访问",
                message="当前请求不是从 Parser X 插件页面发起的。",
                action="请返回 AstrBot 插件详情页并重新打开解析调试台。",
                status_code=403,
            )
        session_id = request.query.get("session_id", "")
        session = self.debug_sessions.get(str(session_id or ""), owner=owner)
        if session is None:
            return self._debug_page_error_response(
                code="session_not_found",
                title="消息通道已失效",
                stage="会话",
                message="当前调试会话不存在、已经结束或已经过期。",
                action="请重新发送分享文本以创建新的调试会话。",
                status_code=404,
            )
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
            return self._debug_page_error_response(
                code="page_access_required",
                title="无法取消任务",
                stage="访问",
                message="当前请求不是从 Parser X 插件页面发起的。",
                action="请返回 AstrBot 插件详情页并重新打开解析调试台。",
                status_code=403,
            )
        payload = await request.json(default={})
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        cancelled = await self.debug_sessions.cancel(
            str(session_id or ""),
            owner=owner,
        )
        if not cancelled:
            return self._debug_page_error_response(
                code="session_not_found",
                title="无法取消任务",
                stage="会话",
                message="当前调试会话不存在或已经结束。",
                action="无需再次取消；可以直接开始新的解析任务。",
                status_code=404,
            )
        return json_response({"cancelled": True})

    async def debug_page_media_preview(self, token: str):
        owner = self._debug_page_owner()
        if owner is None:
            return self._debug_page_error_response(
                code="media_access_denied",
                title="无法预览媒体",
                stage="媒体",
                message="当前页面没有访问该调试媒体的权限。",
                action="请返回原调试页面并重新解析该链接。",
                status_code=403,
            )
        entry = self.debug_media.get(token, owner=owner)
        if entry is None:
            return self._debug_page_error_response(
                code="media_not_found",
                title="无法预览媒体",
                stage="媒体",
                message="该媒体文件不存在或预览令牌已经过期。",
                action="请重新解析链接以生成新的媒体预览。",
                status_code=404,
            )
        if entry.size > 16 * 1024 * 1024:
            return self._debug_page_error_response(
                code="media_preview_too_large",
                title="无法在线预览媒体",
                stage="媒体",
                message="该媒体超过 16 MB 的页面预览上限。",
                action="请使用消息卡片中的“保存”按钮在本地查看。",
                status_code=413,
            )

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
        if owner is None:
            return self._debug_page_error_response(
                code="media_access_denied",
                title="无法保存媒体",
                stage="媒体",
                message="当前页面没有访问该调试媒体的权限。",
                action="请返回原调试页面并重新解析该链接。",
                status_code=403,
            )
        entry = self.debug_media.get(token, owner=owner)
        if entry is None:
            return self._debug_page_error_response(
                code="media_not_found",
                title="无法保存媒体",
                stage="媒体",
                message="该媒体文件不存在或下载令牌已经过期。",
                action="请重新解析链接以生成新的媒体文件。",
                status_code=404,
            )
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
                "exclusive": self._debug_mode_enabled(),
            }
        )

        completed = 0
        issue_count = 0
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
            parse_started_at = asyncio.get_running_loop().time()
            try:
                result = await self._parse_with_cache(parser, keyword, searched, text)
            except SkipParseException:
                await emit(
                    debug_issue_event(
                        event="skipped",
                        code="parser_skipped",
                        title="已跳过该链接",
                        stage="解析",
                        platform=platform_name,
                        message="解析器判断该分享当前不应继续处理。",
                        action="请检查链接内容或平台配置；如确认应当支持，请查看 AstrBot 日志。",
                    )
                )
                issue_count += 1
                continue
            except (SizeLimitException, ParseException) as exc:
                await emit(
                    debug_issue_event(
                        code=(
                            "content_limit_exceeded"
                            if isinstance(exc, SizeLimitException)
                            else "parse_failed"
                        ),
                        title=(
                            "内容超过处理限制"
                            if isinstance(exc, SizeLimitException)
                            else "链接解析失败"
                        ),
                        stage="解析",
                        platform=platform_name,
                        message=str(exc) or "解析器未能读取该分享内容。",
                        action=(
                            "请调整插件的媒体大小限制，或换用体积更小的内容后重试。"
                            if isinstance(exc, SizeLimitException)
                            else "请确认链接仍可访问、Cookie 配置有效，然后重试。"
                        ),
                    )
                )
                issue_count += 1
                continue
            except Exception:
                logger.exception("Canvas 调试页解析发生未知错误")
                await emit(
                    debug_issue_event(
                        code="parse_internal_error",
                        title="链接解析异常",
                        stage="解析",
                        platform=platform_name,
                        message="解析器发生未预期异常，详细信息已写入 AstrBot 日志。",
                        action="请查看 AstrBot 日志定位原因；修复配置或网络问题后重试。",
                    )
                )
                issue_count += 1
                continue

            await emit(
                {
                    "event": "parsed",
                    "platform": platform_name,
                    "parse_ms": round(
                        (asyncio.get_running_loop().time() - parse_started_at) * 1000
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
            except Exception:
                logger.exception("Canvas 调试页模拟投递失败")
                await emit(
                    debug_issue_event(
                        code="delivery_preview_failed",
                        title="消息预览生成失败",
                        stage="投递",
                        platform=platform_name,
                        message="解析已经完成，但无法生成对应的 QQ 消息预览。",
                        action="请查看 AstrBot 日志中的投递错误；确认媒体文件仍存在后重试。",
                    )
                )
                issue_count += 1
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
                "issue_count": issue_count,
                "match_count": len(matches),
                "elapsed_ms": round(
                    (asyncio.get_running_loop().time() - started_at) * 1000
                ),
            }
        )

    # endregion

    @staticmethod
    def _looks_like_live_share(text: str) -> bool:
        return looks_like_live_share(text)

    # Short links hide their target until the redirect resolves; their caption is
    # the only pre-flight signal that the share is a live room.
    LIVE_SHORT_LINK_KEYWORDS = frozenset(
        {
            "v.douyin",
            "jx.douyin",
            "b23.tv",
            "bili2233",
            "xhslink.com",
            "xhslink.cn",
            "v.kuaishou",
            "chenzhongtech",
        }
    )

    def _should_ignore_live_share(
        self,
        keyword: str,
        raw_match: str,
        text: str,
    ) -> bool:
        if keyword in self.LIVE_SHORT_LINK_KEYWORDS and is_live_share_caption(text):
            return True

        low_url = (raw_match or "").lower()
        low_text = (text or "").lower()

        # Every platform: a live room URL is never answered and never parsed.
        # 有的平台正则只截到 "kuaishou.com/u/..." 而丢掉 live./webcast. 前缀，
        # 所以再回原文里按完整写法找一次。
        if is_live_url(raw_match) or (low_url and f"live.{low_url}" in low_text):
            return True

        if keyword in {"v.kuaishou", "kuaishou", "chenzhongtech"}:
            return (
                "live.kuaishou.com" in low_url
                or "/live/" in low_url
                or is_live_share_caption(text)
            )

        if keyword in {"xhslink.com", "xhslink.cn"}:
            return "livestream" in low_text or (
                "小红书" in text and self._looks_like_live_share(text)
            )

        if keyword in {"v.douyin", "jx.douyin"}:
            # 主页分享（"查看TA的更多作品"）不是作品，入口直接丢弃，
            # 免得解析器跳转后拿不到 aweme_id 再报一堆错误。
            if any(marker in text for marker in ("更多作品", "的主页")):
                return True
            return is_live_share_caption(text)

        return False

    @staticmethod
    def _match_has_hostname_boundary(text: str, searched: re.Match[str]) -> bool:
        """Validate the enclosing authority, not just a regex's matched substring.

        Most decorators omit the scheme (some omit www); XHS note decorators
        match only the relative route. Keep those match objects unchanged for
        dispatch while refusing domains embedded in unrelated hosts or paths.
        Bare BV/av identifiers have no enclosing URL and remain supported.
        """
        for url in re.finditer(
            r"(?:https?://)?(?P<host>(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
            r"(?::\d+)?/[^\s<>\"'，。；！？）]*",
            text,
            re.I,
        ):
            if not (url.start() <= searched.start() < url.end()):
                continue
            host = url.group("host").lower()
            if searched.start() < url.end("host"):
                candidate = re.match(
                    r"(?:https?://)?((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?::\d+)?/",
                    searched.group(0),
                    re.I,
                )
                if candidate is None:
                    return False
                matched_host = candidate.group(1).lower()
                return host == matched_host or host.endswith("." + matched_host)
            # XHS decorators deliberately return a relative route, not a URL.
            return (
                (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
                and searched.start() == url.end("host") + 1
                and searched.group(0).startswith(("explore/", "discovery/item/"))
            )
        return True

    def _collect_parser_matches(
        self,
        text: str,
    ) -> list[tuple[BaseParser, str, re.Match[str]]]:
        matches: list[tuple[int, str, re.Match[str]]] = []
        for keyword, pattern in self.key_pattern_list:
            if keyword not in text:
                continue
            for searched in pattern.finditer(text):
                if not self._match_has_hostname_boundary(text, searched):
                    continue
                if is_nonwork_url(searched.group(0)):
                    continue
                if self._should_ignore_live_share(
                    keyword,
                    searched.group(0),
                    text,
                ):
                    logger.info(
                        f"跳过直播链接（{searched.group(0).strip()}）：不响应、不解析"
                    )
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

        return self._prefer_xiaohongshu_note_matches(accepted)

    @staticmethod
    def _xiaohongshu_note_id(raw_match: str) -> str:
        matched = re.search(
            r"(?:explore|discovery/item)/([0-9a-zA-Z]+)",
            raw_match,
            re.I,
        )
        return matched.group(1).lower() if matched else ""

    @classmethod
    def _prefer_xiaohongshu_note_matches(
        cls,
        accepted: list[tuple[BaseParser, str, re.Match[str]]],
    ) -> list[tuple[BaseParser, str, re.Match[str]]]:
        # An opaque short link is not proof of identity with any full note in
        # the message. Only deduplicate routes carrying the same known note ID.
        preferred: dict[str, tuple[int, tuple[BaseParser, str, re.Match[str]]]] = {}
        for item in accepted:
            parser, keyword, searched = item
            if (
                parser.platform.name != "xiaohongshu"
                or keyword in {"xhslink.com", "xhslink.cn"}
            ):
                continue
            note_id = cls._xiaohongshu_note_id(searched.group(0))
            if not note_id:
                continue
            score = 1 if "xsec_token=" in searched.group(0).lower() else 0
            existing = preferred.get(note_id)
            if existing is None or score > existing[0]:
                preferred[note_id] = (score, item)

        if not preferred:
            return accepted

        merged: list[tuple[BaseParser, str, re.Match[str]]] = []
        used_ids: set[str] = set()
        for item in accepted:
            parser, keyword, searched = item
            if (
                parser.platform.name != "xiaohongshu"
                or keyword in {"xhslink.com", "xhslink.cn"}
            ):
                merged.append(item)
                continue
            note_id = cls._xiaohongshu_note_id(searched.group(0))
            if not note_id:
                merged.append(item)
                continue
            if note_id in used_ids:
                continue
            used_ids.add(note_id)
            merged.append(preferred[note_id][1])
        return merged

    @staticmethod
    def _is_share_card_segment(segment: object) -> bool:
        if isinstance(segment, Json):
            return True
        return type(segment).__name__ in {"Json", "Xml"}

    def _text_from_event(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        chain = event.get_messages()
        if not chain:
            return text

        card_urls: list[str] = []
        for segment in chain:
            if not self._is_share_card_segment(segment):
                continue
            try:
                url = extract_json_url(getattr(segment, "data", None)) or ""
            except Exception:
                url = ""
            if url:
                card_urls.append(url)

        if not card_urls:
            return text

        best = pick_supported_share_url(card_urls)
        if not best:
            return text
        if not text:
            return best
        if best in text:
            return text
        return f"{text}\n{best}"

    def _live_card_reason(self, chain: list) -> str:
        """分享卡片本身是直播时给出原因；纯文本消息不在此判定。

        卡片里带着平台自己的标题（如「⚡直播中⚡某某的直播间」），
        这类消息连跳转都不该发生，直接整条丢弃。
        """
        for segment in chain or ():
            if not self._is_share_card_segment(segment):
                continue
            data = getattr(segment, "data", None)
            try:
                url = extract_json_url(data) or ""
            except Exception:
                url = ""
            if url and is_live_url(url):
                return "直播卡片链接"
            raw = data if isinstance(data, str) else json.dumps(
                data, ensure_ascii=False, default=str
            )
            if not looks_like_live_card(raw):
                continue
            # 卡片文案像直播，且卡片给的地址要么是短链（跳转前看不出目标），
            # 要么根本没提取到地址；直接指向普通视频的卡片不受影响。
            if not url or is_short_link(url):
                return "直播分享卡片"
        return ""

    async def _parse_with_cache(
        self, parser: BaseParser, keyword: str, searched: re.Match[str], text: str
    ) -> ParseResult:
        cache = getattr(self, "result_cache", None)
        if cache is None:
            cache = self.result_cache = ParseResultCache()
        return await cache.parse(
            parser, keyword, searched, text, getattr(self, "config", {})
        )


    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听 OneBot 消息并解析其中的支持平台分享链接。"""
        if self._debug_mode_enabled():
            return
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        if not self._should_respond(event):
            return
        umo = event.unified_msg_origin
        text = self._text_from_event(event)

        if self._session_key(event) in self._disabled_sessions():
            return

        chain = event.get_messages() or []
        ignored_reason = ignored_component_reason(chain)
        if ignored_reason:
            logger.info(f"忽略非常规卡片消息（{ignored_reason}）：不响应、不解析")
            return

        live_reason = self._live_card_reason(chain)
        if live_reason:
            logger.info(f"跳过直播分享（{live_reason}）：不响应、不解析")
            return

        if not text:
            if any(is_card_segment(segment) for segment in chain):
                logger.info("忽略非常规卡片消息（分享卡片中没有可解析链接）：不响应、不解析")
            return

        prefixes = self.context.get_config().get("command_prefixes", ["/"])
        is_command = any(text.startswith(p) for p in prefixes)
        if is_command:
            return

        collected_matches = self._collect_parser_matches(text)
        if not collected_matches:
            return

        notified = False
        for parser, keyword, searched in collected_matches:
            try:
                parse_res = await self._parse_with_cache(
                    parser, keyword, searched, text
                )
                # 只在此刻贴表情：解析真的产出了结果。
                # 直播间/被跳过的链接会走 SkipParseException，永远贴不到表情。
                if not notified and isinstance(event, AiocqhttpMessageEvent):
                    if hasattr(event.message_obj, "message_id"):
                        notified = True
                        asyncio.create_task(
                            self.arbiter.notify(event.bot, event.message_obj.message_id)
                        )
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
        umo = self._session_key(event)
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
        umo = self._session_key(event)
        sessions = self._disabled_sessions()
        if umo not in sessions:
            sessions.append(umo)
            self._save_disabled_sessions(sessions)
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")

