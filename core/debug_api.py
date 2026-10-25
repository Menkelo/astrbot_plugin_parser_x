"""Plugin-page debug API, using the host plugin's parser/cache/delivery pipeline."""

import asyncio
import base64

from astrbot.api import logger
from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)

from .comment_settings import parse_bool
from .debug_page import (
    DebugCaptureEvent,
    DebugMessageSerializer,
    debug_issue_event,
    serialize_parse_result,
)
from .exception import ParseException, SizeLimitException, SkipParseException

PLUGIN_NAME = "astrbot_plugin_parser_x"


class DebugApiMixin:
    """Debug endpoints sharing the concrete plugin's runtime services."""

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

