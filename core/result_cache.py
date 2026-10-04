"""Bounded, process-local reuse of successful parses and their media tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from re import Match
from typing import Any

from .comment_settings import parse_bool
from .data import Author, MediaContent, ParseResult


def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass(frozen=True)
class CacheSettings:
    enabled: bool = True
    ttl: int = 300
    max_entries: int = 128

    @classmethod
    def from_config(cls, config: Mapping) -> CacheSettings:
        options = config.get("result_cache", {})
        if not isinstance(options, Mapping):
            options = {}
        return cls(
            enabled=parse_bool(options.get("enabled", True), True),
            ttl=_bounded_int(options.get("ttl", 300), 300, 15, 3600),
            max_entries=_bounded_int(options.get("max_entries", 128), 128, 1, 512),
        )


def _walk(value: Any, seen: set[int] | None = None) -> Iterator[Any]:
    """Visit data, not closures (comment factories run afresh for every send)."""
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if is_dataclass(value) and not isinstance(value, type):
        children = (getattr(value, field.name) for field in fields(value))
    elif isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple, set)):
        children = value
    else:
        return
    for child in children:
        yield from _walk(child, seen)


def _valid(result: Any) -> bool:
    for value in _walk(result):
        if isinstance(value, asyncio.Future):
            if value.cancelled():
                return False
            if value.done():
                try:
                    if not _valid(value.result()):
                        return False
                except Exception:
                    return False
        elif isinstance(value, Path):
            try:
                if not value.is_file() or value.stat().st_size == 0:
                    return False
            except OSError:
                return False
    return True


def _clone(result: ParseResult) -> ParseResult:
    # Tasks cannot be deep-copied. Only the download is shared; mutable result
    # data and delivery batches belong to each caller. Media getters shield the
    # shared task so cancelling one recipient cannot cancel another's download.
    memo = {
        id(value): value for value in _walk(result) if isinstance(value, asyncio.Future)
    }
    cloned = deepcopy(result, memo)
    for value in _walk(cloned):
        if isinstance(value, (MediaContent, Author)):
            value._cache_shared = True
    return cloned


def _config_signature(config: Mapping) -> str:
    # Hash credentials instead of exposing them in cache keys or logs. Include
    # the uploaded yt-dlp cookie file's identity, since its path can stay fixed.
    cookies = config.get("cookies", {})
    cookie_files = (
        cookies.get("ytdlp_cookie_file", []) if isinstance(cookies, Mapping) else []
    )
    cookie_files = cookie_files if isinstance(cookie_files, list) else [cookie_files]
    stamps = []
    for item in cookie_files:
        if isinstance(item, dict):
            item = item.get("path") or item.get("file") or item.get("name")
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            stat = Path(item).expanduser().stat()
            stamps.append((item, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append((item, None, None))
    raw = json.dumps([dict(config), stamps], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class _Entry:
    result: ParseResult
    expires_at: float


@dataclass
class _Pending:
    task: asyncio.Task[ParseResult]
    waiters: int = 0


class ParseResultCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._settings = CacheSettings()
        self._signature = ""
        self._entries: OrderedDict[tuple, _Entry] = OrderedDict()
        self._pending: dict[tuple, _Pending] = {}
        self._media_tasks: set[asyncio.Future] = set()
        self._closed = False

    def _track_media(self, result: ParseResult) -> None:
        def finished(task):
            self._media_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        for value in _walk(result):
            if isinstance(value, asyncio.Future) and value not in self._media_tasks:
                self._media_tasks.add(value)
                value.add_done_callback(finished)

    def _discard(self, key: tuple, entry: _Entry) -> None:
        if self._entries.get(key) is entry:
            self._entries.pop(key)

    def _remember(self, key: tuple, result: ParseResult) -> None:
        if self._closed or key[0] != self._signature or not _valid(result):
            return
        # Live status and snapshots should always be refreshed.
        if (
            result.extra.get("cache_result") is False
            or "live.bilibili.com" in (result.url or "").lower()
        ):
            return
        entry = _Entry(_clone(result), self._clock() + self._settings.ttl)
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self._settings.max_entries:
            self._entries.popitem(last=False)

        def media_finished(_future):
            if not _valid(entry.result):
                self._discard(key, entry)

        for value in _walk(entry.result):
            if isinstance(value, asyncio.Future) and not value.done():
                value.add_done_callback(media_finished)

    async def parse(
        self,
        parser: Any,
        keyword: str,
        searched: Match[str],
        source_text: str,
        config: Mapping,
    ) -> ParseResult:
        if self._closed:
            raise RuntimeError("Parse cache is closed")
        self._settings = CacheSettings.from_config(config)
        signature = _config_signature(config)
        if signature != self._signature or not self._settings.enabled:
            self._entries.clear()
            self._signature = signature

        async def parse_once():
            # BaseParser.source_text is a ContextVar, inherited by download
            # tasks. Set it inside this request's task, never a shared global.
            parser.source_text = source_text
            result = await parser.parse(keyword, searched)
            self._track_media(result)
            return result

        if not self._settings.enabled:
            return await parse_once()

        # Keep query parameters, capture groups and share text intact. They can
        # carry page numbers, access tokens, or live/daily-share hints.
        payload = [searched.group(0), searched.groups(), source_text.strip()]
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode()
        ).hexdigest()
        key = (signature, id(parser), keyword, digest)
        now = self._clock()
        for old_key, old_entry in list(self._entries.items()):
            if old_entry.expires_at <= now:
                self._discard(old_key, old_entry)
        if entry := self._entries.get(key):
            if _valid(entry.result):
                self._entries.move_to_end(key)
                return _clone(entry.result)
            self._discard(key, entry)

        pending = self._pending.get(key)
        if pending is None:

            async def produce():
                result = await parse_once()
                self._remember(key, result)
                return result

            task = asyncio.create_task(produce(), name="parser_x_parse")
            pending = _Pending(task)
            self._pending[key] = pending

            def finished(done):
                if self._pending.get(key) is pending:
                    self._pending.pop(key)
                if not done.cancelled():
                    done.exception()  # Retrieve errors even if all waiters left.

            task.add_done_callback(finished)
        pending.waiters += 1
        try:
            return _clone(await asyncio.shield(pending.task))
        finally:
            pending.waiters -= 1
            if pending.waiters == 0 and not pending.task.done():
                self._pending.pop(key, None)
                pending.task.cancel()

    async def close(self) -> None:
        self._closed = True
        tasks = {pending.task for pending in self._pending.values()} | self._media_tasks
        self._entries.clear()
        self._pending.clear()
        self._media_tasks = set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
