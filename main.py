import nest_asyncio
nest_asyncio.apply()

import asyncio
import hashlib
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import EmojiLikeArbiter
from .core.clean import CacheCleaner
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
    ZeroSizeException,
)
from .core.parsers import BaseParser
from .core.text_renderer import TextCardRenderer
from .core.utils import extract_json_url, exec_ffmpeg_cmd


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_r_parser")
        config["data_dir"] = str(self.data_dir)
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)
        self.config.save_config()

        self.parser_map: dict[str, BaseParser] = {}
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.downloader = Downloader(config)
        self.arbiter = EmojiLikeArbiter()
        self.cleaner = CacheCleaner(self.context, self.config)
        self.text_renderer = TextCardRenderer()

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
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(f"已加载平台: {'、'.join(platform_names)}")

        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in all_subclass
            for kw, pt in cls._key_patterns
        ]
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

    # endregion

    # region 核心逻辑 (Download / Convert / Send)

    async def _download_content(self, cont: MediaContent) -> tuple[MediaContent, Path | None, str | None]:
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

    def _convert_to_seg(self, cont: MediaContent, path: Path) -> BaseMessageComponent | None:
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
        return "\n\n".join(parts).replace("@", "@\u200b")

    async def _ensure_text_only_content(self, result: ParseResult) -> bool:
        if result.contents:
            return False

        text = (result.text or "").strip()
        if not text:
            return False

        author_name = result.author.name if result.author else None
        author_avatar = result.extra.get("text_card_avatar")
        if not isinstance(author_avatar, str) or not author_avatar.strip():
            author_avatar = None
        platform_name = result.platform.display_name or result.platform.name
        title = (result.title or "").strip() or None
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
                    "text_card_v4",
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
                timestamp_text=timestamp_text,
            )

        result.contents = [ImageContent(out_path)]
        return True

    async def _transcode_to_h264(self, input_path: Path) -> Path:
        output_path = input_path.with_name(f"{input_path.stem}_h264.mp4")
        logger.info(f"正在转码视频为 H.264 (极速模式): {input_path.name} -> {output_path.name}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "superfast",
            "-tune", "zerolatency",
            "-crf", "28",
            "-vf", "scale='min(1280,iw)':-2",
            "-maxrate", "1.5M",
            "-bufsize", "3M",
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path),
        ]
        await exec_ffmpeg_cmd(cmd)
        return output_path

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)

        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        async def process_main_content():
            if not result.contents:
                try:
                    await self._ensure_text_only_content(result)
                except Exception as e:
                    logger.warning(f"text-only render failed: {e}")
                    if result.text and result.text.strip():
                        await event.send(event.plain_result(self._format_text_fallback(result)))
                    return

                if not result.contents:
                    return

            tasks = [self._download_content(c) for c in result.contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}

            segs = []
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

            has_video = any(isinstance(c, (VideoContent, DynamicContent)) for c in result.contents)

            if has_video:
                media_count = sum(1 for s in segs if isinstance(s, (Video, Image, File, Record)))
                if media_count >= 2:
                    try:
                        nodes = Nodes([])
                        for seg in segs:
                            nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
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
                            "rich media" in err_msg or "1200" in err_msg or "Timeout" in err_msg
                        ):
                            logger.warning("视频发送失败(编码不兼容/超时)，尝试转码 H.264 重试...")
                            path_str = getattr(seg, "file", None)
                            if path_str:
                                try:
                                    input_path = Path(path_str)
                                    new_path = await self._transcode_to_h264(input_path)
                                    await event.send(event.chain_result([Video(str(new_path))]))
                                    try:
                                        if input_path.exists():
                                            await asyncio.to_thread(input_path.unlink)
                                    except Exception:
                                        pass
                                    continue
                                except Exception:
                                    pass

                        await event.send(
                            event.plain_result(f"⚠️ 媒体发送失败\n🔗 原链接: {result.url or '未知'}")
                        )
            else:
                if len(segs) == 1:
                    await event.send(event.chain_result([segs[0]]))
                    return

                nodes = Nodes([])
                for seg in segs:
                    nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
                if nodes.nodes:
                    await event.send(event.chain_result([nodes]))

        async def process_comment_content():
            # 评论区在解析阶段被放到后台任务(避免抓取/二维码识别阻塞主视频)，
            # 这里与主视频发送并行地等待它完成。
            comment_task = result.extra.get("comment_task")
            if comment_task is not None:
                try:
                    result.comment_contents = await comment_task
                except Exception as e:
                    logger.warning(f"评论区生成失败: {e}")
                    return

            if not result.comment_contents:
                return

            tasks = [self._download_content(c) for c in result.comment_contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}

            segs = []
            for cont in result.comment_contents:
                path, _ = path_map.get(id(cont), (None, None))
                if path:
                    if seg := self._convert_to_seg(cont, path):
                        segs.append(seg)

            if not segs:
                return

            nodes = Nodes([])
            nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[Plain("评论区↓")]))
            for seg in segs:
                nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
            await event.send(event.chain_result([nodes]))

        await asyncio.gather(process_main_content(), process_comment_content())

    # endregion

    # region 事件监听

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        text = event.message_str.strip()

        if umo in self.config["disabled_sessions"]:
            return

        if not text:
            chain = event.get_messages()
            if chain:
                seg1 = chain[0]
                if isinstance(seg1, Json):
                    try:
                        text = extract_json_url(seg1.data)
                    except Exception:
                        pass

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

        if not matches:
            return

        if isinstance(event, AiocqhttpMessageEvent):
            if hasattr(event.message_obj, "message_id"):
                asyncio.create_task(
                    self.arbiter.notify(event.bot, event.message_obj.message_id)
                )

        matches.sort(key=lambda x: (x[0], -len(x[1])))
        processed_parsers: set[int] = set()

        for _, keyword, searched in matches:
            parser = self.parser_map.get(keyword)
            if parser is None:
                continue

            pid = id(parser)
            if pid in processed_parsers:
                continue
            processed_parsers.add(pid)

            try:
                parse_res = await parser.parse(keyword, searched)
                await self._send_parse_result(event, parse_res)
            except SizeLimitException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except ParseException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except Exception:
                logger.exception("解析过程中发生未知错误")

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")
