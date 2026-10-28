"""Media delivery, conversion and retry helpers shared by the plugin pipeline."""

import asyncio
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Reply,
    Video,
)

from .comment_settings import parse_bool
from .data import (
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
from .exception import (
    DownloadException,
    DownloadLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import exec_ffmpeg_cmd, generate_file_name


def _wechat_inline_static(result, content, path, card_segment):
    """Only a complete WX flow image can replace its decoded static original."""
    if (card_segment is None or result.platform.name != 'wechat_article'
            or result.extra.get('delivery_text_card_consume_non_video') is not True
            or result.extra.get('text_card_detach_images')
            or type(content) is not ImageContent):
        return False
    blocks = result.extra.get('text_card_flow')
    if not isinstance(blocks, (list, tuple)):
        return False
    if not any(isinstance(block, dict) and block.get('type') == 'image'
               and isinstance(block.get('url'), str)
               and path.stem == Path(generate_file_name(block['url'], '.jpg')).stem
               for block in blocks):
        return False
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as image:
            if getattr(image, 'is_animated', False):
                return False
            image.verify()
        return True
    except Exception:
        return False



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


class DeliveryMixin:
    """Deliver media using the concrete plugin's configuration and card builder."""

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
                return File(name=cont.name or path.name, file=str(path))
            case FileContent():
                return File(name=path.name, file=str(path))
        return None

    @staticmethod
    def _comment_forward_segments(result: ParseResult, card_segment) -> list:
        """Only video works get a separate comment-forward preview heading."""
        if result.has_video and not result.extra.get('mixed_post_body_card') and (callable(result.extra.get("text_card_comment_factory"))
                                 or result.extra.get("_disk_has_comments")):
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
                if result.platform.name == 'wechat_article' and await asyncio.to_thread(_wechat_inline_static, result, content, path, lead_card_segment):
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

    async def _deliver_parse_result(self, event: AstrMessageEvent, result: ParseResult):
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

        # 混合媒体作品（如微博九宫格配视频、抖音图文里的视频幻灯片）里的图片是
        # 作品自身的原图，不是视频封面。整条作品按原顺序装进一条合并转发。
        mixed_media_forward = (
            result.has_video
            and result.extra.get('mixed_post_body_card') is True
            and any(isinstance(content, (ImageContent, GraphicsContent))
                    for content in media_contents)
        )
        if result.has_video and not mixed_media_forward:
            # Video previews already carry a cover. Never resend source images
            # beside a comment card, including images from native delivery plans.
            media_contents = [content for content in media_contents
                              if not isinstance(content, (ImageContent, GraphicsContent))]

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
            contents: list[MediaContent], card_task=None,
        ) -> tuple[list[BaseMessageComponent], list[str]]:
            download_results = await asyncio.gather(
                *(self._download_content(content) for content in contents)
            )
            card_segment = await card_task if card_task is not None else None
            segments: list[BaseMessageComponent] = []
            errors: list[str] = []
            for content, path, error in download_results:
                if error:
                    errors.append(error.strip())
                    continue
                if path and result.platform.name == 'wechat_article' and await asyncio.to_thread(_wechat_inline_static, result, content, path, card_segment):
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
        if mixed_media_forward:
            # Keep the card and every original image/video in one ordered forward.
            # Do not let the global pure-video branch discard the image gallery.
            card_task = asyncio.create_task(prepare_body_card_segment()) if cards_enabled else None
            try:
                segments, errors = await prepare_media_segments(media_contents, card_task)
                card_segment = await card_task if card_task is not None else None
                leading = [card_segment] if card_segment is not None else []
                if not leading and (body := str(result.text or '').strip()):
                    leading = [Plain(body.replace('@', '@\u200b'))]
                tips = [Plain(error) for error in errors] if show_download_fail_tip else []
                await send_forward_segments(
                    [*leading, *segments, *tips],
                    failure_label=f'{result.platform.display_name}混合媒体合并转发失败',
                )
            finally:
                if card_task is not None and not card_task.done():
                    card_task.cancel()
                    await asyncio.gather(card_task, return_exceptions=True)
            return

        if not cards_enabled and media_contents and not result.has_video:
            # Preserve image/audio source bodies, never add text to pure videos.
            # Mixed Weibo galleries already retain their body in the branch above.
            has_native_text = result.delivery is not None and any(
                isinstance(part, str) and part.strip()
                for batch in result.delivery.batches for part in batch.parts
            )
            if not has_native_text and (body := str(result.text or "").strip()):
                try:
                    await event.send(event.plain_result(body.replace("@", "@\u200b")))
                except Exception as exc:
                    logger.warning(f"原始正文发送失败，继续原始媒体: {exc}")
        if result.has_video:
            # One shared video path for all platforms, including native plans:
            # source video(s) + an optional card containing captured comments.
            # Missing comments are intentional suppression, never a render
            # failure that should resurrect a summary or source-image forward.
            card_task = asyncio.create_task(prepare_body_card_segment()) if cards_enabled else None
            try:
                # Preserve source order for multi-video posts instead of racing
                # the transfers and making separate clips look like duplicate sends.
                for content in media_contents:
                    await send_media_content(content)
                card_segment = await card_task if card_task is not None else None
                if card_segment is not None:
                    try:
                        await event.send(event.chain_result([Nodes([
                            Node(uin=node_uin, name=node_name, content=[part])
                            for part in self._comment_forward_segments(result, card_segment)
                        ])]))
                    except Exception as exc:
                        logger.warning(f'视频评论卡合并转发失败，降级引用发送: {exc}')
                        reply = original_message_reply()
                        await event.send(event.chain_result(
                            ([reply] if reply is not None else []) + [card_segment]))
            finally:
                if card_task is not None and not card_task.done():
                    card_task.cancel()
                    await asyncio.gather(card_task, return_exceptions=True)
            return

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
                    [content for content in media_contents if content not in video_contents], card_task
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
                media_segments, errors = await prepare_media_segments(image_contents, card_task)
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
