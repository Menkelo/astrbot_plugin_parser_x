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
from .core.clean import CacheCleaner
from .core.comment_settings import parse_bool
from .core.data import (
    AudioContent,
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
        self._background_tasks: set[asyncio.Task] = set()

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()
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
        parts: list[str] = []
        text = (result.text or "").strip()
        if text:
            parts.append(text)
        extra_info = (result.extra_info or "").strip()
        if extra_info and extra_info not in text:
            parts.append(extra_info)
        return "\n\n".join(parts)

    async def _build_text_card_content(
        self,
        result: ParseResult,
    ) -> ImageContent | None:
        text = self._text_card_body(result)
        title = (result.title or "").strip() or None
        if not text and not title:
            text = (result.url or "").strip()
        if not text and not title:
            return None

        author_name = result.author.name if result.author else None
        author_avatar = result.extra.get("text_card_avatar")
        if not isinstance(author_avatar, str) or not author_avatar.strip():
            author_avatar = None
        platform_name = result.platform.display_name or result.platform.name
        timestamp_text = result.formatted_datetime

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
                    "text_card_v7_shared_media",
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
            )

        return ImageContent(out_path)

    async def _ensure_text_only_content(self, result: ParseResult) -> bool:
        if result.contents:
            return False

        card = await self._build_text_card_content(result)
        if card is None:
            return False
        result.contents = [card]
        return True

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

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        show_download_fail_tip = self._show_download_fail_tip()

        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        def original_message_reply() -> Reply | None:
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id in (None, ""):
                return None
            return Reply(id=message_id)

        async def process_main_content():
            parsed_contents = tuple(result.contents)
            text_card_failed = False
            if result.contents and result.extra.get("render_text_card"):
                try:
                    if card := await self._build_text_card_content(result):
                        result.contents.insert(0, card)
                except Exception as e:
                    text_card_failed = True
                    logger.warning(f"media text-card render failed: {e}")

            if not result.contents:
                if result.extra.get("plain_text_only"):
                    text = (result.text or "").strip()
                    if text:
                        await event.send(
                            event.plain_result(text.replace("@", "@\u200b"))
                        )
                    return

                try:
                    await self._ensure_text_only_content(result)
                except Exception as e:
                    logger.warning(f"text-only render failed: {e}")
                    fallback = self._format_text_fallback(result)
                    if fallback:
                        await event.send(event.plain_result(fallback))
                    return

                if not result.contents:
                    return

            tasks = [self._download_content(c) for c in result.contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}

            segs = []
            if result.extra.get("send_text") or text_card_failed:
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
                if (
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
                        len(parsed_contents) == 1
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
                try:
                    nodes = Nodes([])
                    for seg in segs:
                        nodes.nodes.append(
                            Node(uin=node_uin, name=node_name, content=[seg])
                        )
                    if nodes.nodes:
                        await event.send(event.chain_result([nodes]))
                    return
                except Exception as e:
                    logger.warning(f"合并转发发送失败，降级逐条发送: {e}")

                for seg in segs:
                    try:
                        await event.send(event.chain_result([seg]))
                    except Exception as e:
                        logger.warning(f"图片逐条发送失败: {e}")

        async def process_comment_content():
            # 评论抓取和 Canvas 渲染延迟到发送阶段，避免阻塞主视频取流。
            timeout = max(1.0, float(result.extra.get("comment_timeout", 90)))
            started_at = asyncio.get_running_loop().time()
            comment_task = result.extra.get("comment_task")
            comment_task_factory = result.extra.get("comment_task_factory")
            if comment_task is None and callable(comment_task_factory):
                comment_task = asyncio.create_task(
                    comment_task_factory(),
                    name="parser_x_comment_build",
                )
            if comment_task is not None:
                try:
                    result.comment_contents = await asyncio.wait_for(
                        comment_task, timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning("评论区生成超时，已跳过发送")
                    return
                except Exception as e:
                    logger.warning(f"评论区生成失败: {e}")
                    return

            if not result.comment_contents:
                return

            remaining = max(
                0.001,
                timeout - (asyncio.get_running_loop().time() - started_at),
            )
            tasks = [self._download_content(c) for c in result.comment_contents]
            try:
                download_results = await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.warning("评论区渲染超时，已取消并跳过发送")
                return
            path_map = {id(c): (p, err) for c, p, err in download_results}

            segs = []
            for cont in result.comment_contents:
                path, _ = path_map.get(id(cont), (None, None))
                if path:
                    if seg := self._convert_to_seg(cont, path):
                        segs.append(seg)

            if not segs:
                return

            comment_label = f"{result.platform.display_name} 评论区↓"
            nodes = Nodes([])
            nodes.nodes.append(
                Node(uin=node_uin, name=node_name, content=[Plain(comment_label)])
            )
            for seg in segs:
                nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
            try:
                await event.send(event.chain_result([nodes]))
                return
            except Exception as e:
                logger.warning(f"评论区合并转发失败，降级逐张发送: {e}")

            try:
                await event.send(event.chain_result([Plain(comment_label)]))
            except Exception as e:
                logger.debug(f"评论区标题发送失败: {e}")
            for seg in segs:
                try:
                    await event.send(event.chain_result([seg]))
                except Exception as e:
                    logger.warning(f"评论区图片逐张发送失败: {e}")

        # 主内容优先。评论抓取和 Canvas 渲染在主内容完成后才启动，
        # 避免与视频下载/上传争抢连接和 CPU，也确保消息顺序稳定。
        await process_main_content()

        if (
            result.extra.get("comment_task") is not None
            or result.extra.get("comment_task_factory") is not None
        ):

            async def run_comment_content():
                try:
                    await process_comment_content()
                except Exception as e:
                    logger.warning(f"评论区后台发送失败: {e}")

            task = asyncio.create_task(
                run_comment_content(),
                name="parser_x_comment_send",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

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
