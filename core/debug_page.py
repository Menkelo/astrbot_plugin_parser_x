from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .data import DeliveryBatch, MediaContent, ParseResult

DebugEventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
DebugRunner = Callable[[DebugEventEmitter], Awaitable[None]]


def debug_issue_event(
    *,
    event: str = "error",
    code: str,
    title: str,
    stage: str,
    message: str,
    action: str,
    platform: str | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    """Build the shared public issue envelope used by the debug Page."""

    payload: dict[str, Any] = {
        "event": event,
        "level": level
        or {
            "error": "error",
            "skipped": "warning",
            "cancelled": "info",
        }.get(event, "error"),
        "code": str(code or "debug_error").strip(),
        "title": str(title or "调试任务失败").strip(),
        "stage": str(stage or "调试").strip(),
        "message": str(message or "未提供具体原因。").strip(),
        "action": str(action or "请稍后重试。").strip(),
    }
    if platform:
        payload["platform"] = str(platform).strip()
    return payload


@dataclass(slots=True)
class DebugMediaEntry:
    token: str
    owner: str
    path: Path
    name: str
    mime: str
    size: int
    created_at: float = field(default_factory=time.monotonic)


class DebugMediaRegistry:
    """Short-lived, opaque references for files shown on the debug Page."""

    def __init__(self, *, max_entries: int = 256, ttl: float = 1800) -> None:
        self.max_entries = max(16, int(max_entries))
        self.ttl = max(60.0, float(ttl))
        self._entries: dict[str, DebugMediaEntry] = {}
        self._path_tokens: dict[tuple[str, str], str] = {}

    @staticmethod
    def _local_path(source: object) -> Path | None:
        text = str(source or "").strip()
        if not text or text.startswith(("http://", "https://", "base64://", "data:")):
            return None
        if text.startswith("file://"):
            parsed = urlparse(text)
            text = url2pathname(unquote(parsed.path))
            if parsed.netloc:
                text = f"//{parsed.netloc}{text}"
            if len(text) >= 3 and text[0] == "/" and text[2] == ":":
                text = text[1:]
        try:
            path = Path(text).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_file() else None

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, entry in self._entries.items()
            if now - entry.created_at > self.ttl
        ]
        for token in expired:
            self._remove(token)

        overflow = len(self._entries) - self.max_entries
        if overflow <= 0:
            return
        oldest = sorted(self._entries.values(), key=lambda item: item.created_at)
        for entry in oldest[:overflow]:
            self._remove(entry.token)

    def _remove(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry is not None:
            self._path_tokens.pop((entry.owner, str(entry.path)), None)

    def describe(self, source: object, *, owner: str) -> dict[str, Any]:
        text = str(source or "").strip()
        if not text:
            return {}
        if text.startswith(("http://", "https://")):
            return {"url": text, "external": True}
        if text.startswith("data:"):
            return {"data_url": text, "inline": True}
        if text.startswith("base64://"):
            return {
                "data_url": f"data:application/octet-stream;base64,{text[9:]}",
                "inline": True,
            }

        path = self._local_path(text)
        if path is None:
            return {"name": Path(text).name or "media", "missing": True}

        self._prune()
        key = (owner, str(path))
        token = self._path_tokens.get(key)
        entry = self._entries.get(token or "")
        if entry is None:
            token = secrets.token_urlsafe(18)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entry = DebugMediaEntry(
                token=token,
                owner=owner,
                path=path,
                name=path.name,
                mime=mime,
                size=size,
            )
            self._entries[token] = entry
            self._path_tokens[key] = token

        return {
            "token": entry.token,
            "name": entry.name,
            "mime": entry.mime,
            "size": entry.size,
            "previewable": entry.mime.startswith("image/")
            or (
                entry.size <= 12 * 1024 * 1024
                and entry.mime.startswith(("video/", "audio/"))
            ),
        }

    def get(self, token: str, *, owner: str) -> DebugMediaEntry | None:
        self._prune()
        entry = self._entries.get(str(token or ""))
        if entry is None or entry.owner != owner or not entry.path.is_file():
            return None
        return entry


class DebugMessageSerializer:
    def __init__(self, registry: DebugMediaRegistry, *, owner: str) -> None:
        self.registry = registry
        self.owner = owner

    @staticmethod
    def _component_source(component: BaseMessageComponent) -> object:
        if isinstance(component, File):
            return component.file_ or component.url
        return (
            getattr(component, "file", None)
            or getattr(component, "url", None)
            or getattr(component, "path", None)
        )

    async def serialize_component(
        self,
        component: BaseMessageComponent,
    ) -> dict[str, Any]:
        if isinstance(component, Plain):
            return {"type": "text", "text": component.text}
        if isinstance(component, Reply):
            return {"type": "reply", "id": str(component.id)}
        if isinstance(component, Nodes):
            return {
                "type": "forward",
                "nodes": [await self.serialize_node(node) for node in component.nodes],
            }
        if isinstance(component, Image):
            return {
                "type": "image",
                "media": self.registry.describe(
                    self._component_source(component),
                    owner=self.owner,
                ),
            }
        if isinstance(component, Video):
            return {
                "type": "video",
                "media": self.registry.describe(
                    self._component_source(component),
                    owner=self.owner,
                ),
            }
        if isinstance(component, Record):
            return {
                "type": "audio",
                "media": self.registry.describe(
                    self._component_source(component),
                    owner=self.owner,
                ),
            }
        if isinstance(component, File):
            media = self.registry.describe(
                self._component_source(component),
                owner=self.owner,
            )
            media.setdefault("name", component.name or "file")
            return {"type": "file", "media": media}
        if isinstance(component, Node):
            return await self.serialize_node(component)

        component_type = getattr(getattr(component, "type", None), "value", None)
        return {
            "type": str(component_type or type(component).__name__).lower(),
            "label": type(component).__name__,
        }

    async def serialize_node(self, node: Node) -> dict[str, Any]:
        return {
            "type": "node",
            "name": str(node.name or "Parser X"),
            "uin": str(node.uin or "0"),
            "content": [
                await self.serialize_component(component)
                for component in node.content
            ],
        }

    async def serialize_chain(
        self,
        chain: list[BaseMessageComponent],
    ) -> list[dict[str, Any]]:
        return [await self.serialize_component(component) for component in chain]


class DebugCaptureEvent:
    """AstrMessageEvent-shaped sink that records the exact delivery chain."""

    def __init__(
        self,
        *,
        emit: DebugEventEmitter,
        serializer: DebugMessageSerializer,
        started_at: float,
        platform: str,
        initial_message_index: int = 0,
    ) -> None:
        self._emit = emit
        self._serializer = serializer
        self._started_at = started_at
        self._platform = platform
        self._send_lock = asyncio.Lock()
        self._message_index = max(0, int(initial_message_index))
        self.message_obj = SimpleNamespace(message_id="parser-x-debug-source")

    @staticmethod
    def get_sender_id() -> str:
        return "0"

    @staticmethod
    def get_sender_name() -> str:
        return "Parser X 调试台"

    @staticmethod
    def chain_result(chain):
        return chain

    @staticmethod
    def plain_result(text: str):
        return [Plain(text)]

    async def send(self, result) -> None:
        chain = list(result) if isinstance(result, (list, tuple)) else [result]
        async with self._send_lock:
            self._message_index += 1
            await self._emit(
                {
                    "event": "message",
                    "platform": self._platform,
                    "message": {
                        "index": self._message_index,
                        "elapsed_ms": round(
                            (asyncio.get_running_loop().time() - self._started_at)
                            * 1000
                        ),
                        "components": await self._serializer.serialize_chain(chain),
                    },
                }
            )

    @property
    def message_count(self) -> int:
        return self._message_index


@dataclass(slots=True)
class DebugSession:
    session_id: str
    owner: str
    queue: asyncio.Queue[dict[str, Any] | None]
    task: asyncio.Task[None] | None = None
    created_at: float = field(default_factory=time.monotonic)
    ended: bool = False


class DebugSessionManager:
    def __init__(self, *, ttl: float = 1200) -> None:
        self.ttl = max(60.0, float(ttl))
        self._sessions: dict[str, DebugSession] = {}

    @staticmethod
    def _finish(session: DebugSession) -> None:
        if session.ended:
            return
        session.ended = True
        session.queue.put_nowait({"event": "session_end"})
        session.queue.put_nowait(None)

    async def _cleanup(self) -> None:
        now = time.monotonic()
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.created_at > self.ttl
        ]
        for session_id in stale:
            await self.cancel(session_id, owner=None)
            self._sessions.pop(session_id, None)

    async def create(self, *, owner: str, runner: DebugRunner) -> DebugSession:
        await self._cleanup()
        session = DebugSession(
            session_id=secrets.token_urlsafe(18),
            owner=owner,
            queue=asyncio.Queue(),
        )
        self._sessions[session.session_id] = session

        async def emit(payload: dict[str, Any]) -> None:
            await session.queue.put(payload)

        async def run() -> None:
            try:
                await runner(emit)
            except asyncio.CancelledError:
                await emit(
                    debug_issue_event(
                        event="cancelled",
                        code="session_cancelled",
                        title="解析已取消",
                        stage="任务",
                        message="调试任务已停止，不会继续生成消息。",
                        action="可以修改输入内容后重新发送。",
                    )
                )
                raise
            except Exception:
                await emit(
                    debug_issue_event(
                        code="session_internal_error",
                        title="调试任务异常",
                        stage="任务",
                        message="任务发生未预期异常，详细信息已写入 AstrBot 日志。",
                        action="请查看 AstrBot 日志定位原因，然后重新发送。",
                    )
                )
            finally:
                self._finish(session)

        session.task = asyncio.create_task(
            run(),
            name=f"parser_x_debug_{session.session_id}",
        )
        return session

    def get(self, session_id: str, *, owner: str) -> DebugSession | None:
        session = self._sessions.get(str(session_id or ""))
        if session is None or session.owner != owner:
            return None
        return session

    async def cancel(self, session_id: str, *, owner: str | None) -> bool:
        session = self._sessions.get(str(session_id or ""))
        if session is None or (owner is not None and session.owner != owner):
            return False
        if session.task is not None and not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
            if not session.ended:
                session.queue.put_nowait(
                    debug_issue_event(
                        event="cancelled",
                        code="session_cancelled",
                        title="解析已取消",
                        stage="任务",
                        message="调试任务已停止，不会继续生成消息。",
                        action="可以修改输入内容后重新发送。",
                    )
                )
                self._finish(session)
        return True

    async def stream(self, session: DebugSession):
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(session.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if payload is None:
                    break
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            if session.task is not None and not session.task.done():
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            self._sessions.pop(session.session_id, None)

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            if session.task is not None and not session.task.done():
                session.task.cancel()
        await asyncio.gather(
            *(session.task for session in sessions if session.task is not None),
            return_exceptions=True,
        )
        self._sessions.clear()

    @property
    def active_count(self) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if session.task is not None and not session.task.done()
        )


def _delivery_batch_summary(batch: DeliveryBatch) -> dict[str, Any]:
    parts = []
    for part in batch.parts:
        if isinstance(part, str):
            text = part.strip()
            parts.append(
                {
                    "type": "text",
                    "text": text[:240],
                    "truncated": len(text) > 240,
                }
            )
        elif isinstance(part, MediaContent):
            parts.append({"type": type(part).__name__})
    return {
        "mode": batch.mode,
        "reply_original": batch.reply_original,
        "parts": parts,
    }


def serialize_parse_result(result: ParseResult) -> dict[str, Any]:
    return {
        "platform": {
            "name": result.platform.name,
            "display_name": result.platform.display_name,
        },
        "title": result.title,
        "text": result.text,
        "author": result.author.name if result.author else None,
        "timestamp": result.timestamp,
        "url": result.url,
        "contents": [
            {
                "type": type(content).__name__,
                "duration": getattr(content, "duration", None),
            }
            for content in result.contents
        ],
        "delivery": [
            _delivery_batch_summary(batch)
            for batch in (result.delivery.batches if result.delivery else [])
        ],
        "has_comments": callable(result.extra.get("comment_image_task_factory")),
        "native_delivery": bool(result.extra.get("native_delivery")),
        "repost": (
            {
                "platform": result.repost.platform.display_name,
                "title": result.repost.title,
                "text": result.repost.text,
                "url": result.repost.url,
            }
            if result.repost
            else None
        ),
    }


__all__ = [
    "DebugCaptureEvent",
    "DebugMediaRegistry",
    "DebugMessageSerializer",
    "DebugSessionManager",
    "serialize_parse_result",
]
