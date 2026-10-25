"""Concurrent work merging only; no completed values are retained."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class AsyncSingleFlight(Generic[T]):
    def __init__(self):
        self._pending: dict[str, asyncio.Task[T]] = {}
        self._waiters: dict[asyncio.Task[T], int] = {}
        self._closed = False

    async def get(
        self,
        key: str,
        produce: Callable[[], Awaitable[T]],
    ) -> T:
        if self._closed:
            raise RuntimeError("Async cache is closed")
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(produce(), name="parser_x_comment_card")
            self._pending[key] = task

            def finished(done):
                if self._pending.get(key) is done:
                    self._pending.pop(key)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        # Cancellation belongs to the waiter, unless no recipients remain.
        self._waiters[task] = self._waiters.get(task, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            remaining = self._waiters[task] - 1
            if remaining:
                self._waiters[task] = remaining
            else:
                self._waiters.pop(task)
                if not task.done():
                    if self._pending.get(key) is task:
                        self._pending.pop(key)
                    task.cancel()
                    # Drain nested fetch/render cancellation before the last
                    # waiter returns, so aborted deliveries leave no orphan.
                    await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._pending.values())
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
