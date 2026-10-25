"""Resource limits for debug sessions, including clients that never attach SSE."""

import asyncio
import json
from unittest.mock import patch

import pytest

from astrbot_plugin_parser_x.core import debug_api
from astrbot_plugin_parser_x.core.debug_page import DebugSessionManager


def test_start_returns_useful_429_for_admission_limit():
    class Plugin(debug_api.DebugApiMixin):
        def _collect_parser_matches(self, _text):
            return [object()]

        async def _run_debug_page_parse(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    class Request:
        username = "alice"
        plugin_name = debug_api.PLUGIN_NAME

        async def json(self, default=None):
            return {"text": "https://example.com/post"}

    async def run():
        plugin = Plugin()
        plugin.debug_sessions = DebugSessionManager()
        try:
            with patch.object(debug_api, "request", Request()):
                assert (await plugin.debug_page_start()).status_code == 200
                assert (await plugin.debug_page_start()).status_code == 200
                response = await plugin.debug_page_start()
            assert response.status_code == 429
            data = json.loads(response.body)["data"]
            assert data["code"] == "session_limit_reached"
            assert "2" in data["message"]
            assert data["action"]
        finally:
            await plugin.debug_sessions.close()

    asyncio.run(run())


def test_full_queue_cancellation_does_not_block_and_sse_drains_end():
    async def run():
        manager = DebugSessionManager()
        flooded = asyncio.Event()

        async def runner(emit):
            for index in range(1000):
                await emit({"event": "working", "index": index})
            flooded.set()
            await asyncio.Event().wait()

        session = await manager.create(owner="alice", runner=runner)
        try:
            await asyncio.wait_for(flooded.wait(), 1)
            assert 0 < session.queue.maxsize <= 256
            assert session.queue.qsize() <= session.queue.maxsize
            await asyncio.wait_for(manager.cancel(session.session_id, owner="alice"), 0.3)
            chunks = await asyncio.wait_for(
                _drain(manager, session), 0.3
            )
            events = [json.loads(chunk[6:]) for chunk in chunks]
            assert [event["event"] for event in events[-2:]] == ["cancelled", "session_end"]
            assert events[-3]["index"] == 999
            assert len(events) <= 255
        finally:
            await manager.close()

    asyncio.run(run())


async def _drain(manager, session):
    return [chunk async for chunk in manager.stream(session)]


def test_finished_sessions_are_bounded_without_sse_attachment():
    async def run():
        manager = DebugSessionManager()
        sessions = []

        async def runner(emit):
            await emit({"event": "done"})

        try:
            for _ in range(40):
                session = await manager.create(owner="alice", runner=runner)
                await session.task
                sessions.append(session)
            assert len(manager._sessions) <= 16
            assert manager.get(sessions[0].session_id, owner="alice") is None
            assert sessions[0].expiry_handle.cancelled()
            assert manager.get(sessions[-1].session_id, owner="alice") is sessions[-1]
            assert manager.active_count == 0
            chunks = await _drain(manager, sessions[-1])
            assert [json.loads(chunk[6:])["event"] for chunk in chunks] == ["done", "session_end"]
        finally:
            await manager.close()

    asyncio.run(run())


def test_expired_get_preserves_owner_auth_and_cancellation_cleanup():
    async def run():
        manager = DebugSessionManager()
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def runner(_emit):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        session = await manager.create(owner="alice", runner=runner)
        try:
            await started.wait()
            session.created_at -= manager.ttl + 1
            assert manager.get(session.session_id, owner="bob") is None
            assert not session.expired
            assert manager.get(session.session_id, owner="alice") is None
            await cleaning.wait()
            assert manager.get(session.session_id, owner="alice") is None
            await asyncio.sleep(0)
            assert not session.task.done(), "Repeated lookup must not interrupt runner cleanup"
            release.set()
            await asyncio.gather(session.task, return_exceptions=True)
            assert session.ended
        finally:
            release.set()
            await manager.close()

    asyncio.run(run())


def test_create_does_not_wait_for_expired_runner_cleanup():
    async def run():
        manager = DebugSessionManager()
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def runner(_emit):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        session = await manager.create(owner="alice", runner=runner)
        try:
            await started.wait()
            session.created_at -= manager.ttl + 1
            other = await asyncio.wait_for(manager.create(owner="bob", runner=runner), 0.1)
            await cleaning.wait()
            assert session.expired
            assert manager.active_count == 2
            assert manager.get(other.session_id, owner="bob") is other
        finally:
            release.set()
            await manager.close()

    asyncio.run(run())


def test_finished_session_expires_without_another_request():
    async def run():
        manager = DebugSessionManager(ttl=0.02)

        async def runner(_emit):
            return

        try:
            session = await manager.create(owner="alice", runner=runner)
            await session.task
            await asyncio.sleep(0.05)
            assert session.session_id not in manager._sessions
        finally:
            await manager.close()

    asyncio.run(run())


def test_close_cancels_full_queue_and_prestart_sessions_without_consumers():
    async def run():
        manager = DebugSessionManager()
        flooded = asyncio.Event()

        async def runner(emit):
            for _ in range(1000):
                await emit({"event": "working"})
            flooded.set()
            await asyncio.Event().wait()

        first = await manager.create(owner="alice", runner=runner)
        await flooded.wait()
        second = await manager.create(owner="alice", runner=runner)
        await asyncio.wait_for(manager.close(), 0.3)
        assert first.task.done() and second.task.done()
        assert first.expiry_handle.cancelled() and second.expiry_handle.cancelled()
        assert not manager._sessions
        for session in (first, second):
            chunks = await asyncio.wait_for(_drain(manager, session), 0.3)
            events = [json.loads(chunk[6:])["event"] for chunk in chunks]
            assert events[-2:] == ["cancelled", "session_end"]

    asyncio.run(run())


def test_deadline_cancels_runner_without_sse_or_later_create():
    async def run():
        manager = DebugSessionManager(ttl=0.03)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def runner(emit):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        session = await manager.create(owner="alice", runner=runner)
        try:
            await started.wait()
            await asyncio.wait_for(stopped.wait(), 0.3)
            await asyncio.sleep(0)
            assert session.task.done()
            assert manager.get(session.session_id, owner="alice") is None
            assert manager.active_count == 0
            chunks = [chunk async for chunk in manager.stream(session)]
            events = [json.loads(chunk[6:]) for chunk in chunks]
            assert events[-1]["event"] == "session_end"
            assert events[-2]["code"] == "session_expired"
        finally:
            await manager.close()

    asyncio.run(run())


def test_admission_limits_are_atomic_and_owner_scoped():
    async def run():
        manager = DebugSessionManager()

        async def runner(_emit):
            await asyncio.Event().wait()

        try:
            alice = [await manager.create(owner="alice", runner=runner) for _ in range(2)]
            with pytest.raises(Exception, match="owner") as failure:
                await manager.create(owner="alice", runner=runner)
            assert type(failure.value).__name__ == "DebugSessionLimitError"
            await manager.create(owner="bob", runner=runner)
            await manager.create(owner="bob", runner=runner)
            with pytest.raises(type(failure.value), match="global"):
                await manager.create(owner="carol", runner=runner)
            assert manager.active_count == 4
            assert manager.get(alice[0].session_id, owner="bob") is None
            assert not await manager.cancel(alice[0].session_id, owner="bob")
            assert not alice[0].task.cancelled()
            assert await manager.cancel(alice[0].session_id, owner="alice")
            await manager.create(owner="alice", runner=runner)
            assert manager.active_count == 4
        finally:
            await manager.close()

    asyncio.run(run())
