import asyncio
import sys
import pytest
from core import utils

@pytest.mark.asyncio
async def test_cancellation_reaps_real_subprocess(monkeypatch):
    original=asyncio.create_subprocess_exec
    children=[]
    ready=asyncio.Event()
    async def spawn(*args,**kwargs):
        child=await original(*args,**kwargs)
        children.append(child);ready.set();return child
    monkeypatch.setattr(utils.asyncio,'create_subprocess_exec',spawn)
    task=asyncio.create_task(utils.exec_ffmpeg_cmd([sys.executable,'-c','import time; time.sleep(60)']))
    try:
        await ready.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert children[0].returncode is not None
    finally:
        for child in children:
            if child.returncode is None:child.kill();await child.wait()

@pytest.mark.asyncio
async def test_cancel_escalates_when_child_ignores_terminate(monkeypatch):
    class Child:
        returncode=None
        killed=False
        def terminate(self): pass
        def kill(self): self.killed=True;self.returncode=-9
        async def communicate(self):
            if self.killed:return b'',b''
            await asyncio.Event().wait()
    child=Child();ready=asyncio.Event()
    async def spawn(*args,**kwargs):ready.set();return child
    original=asyncio.wait_for
    async def short_wait(awaitable,timeout):return await original(awaitable,timeout=0.01)
    monkeypatch.setattr(utils.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(utils.asyncio,'wait_for',short_wait)
    task=asyncio.create_task(utils.exec_ffmpeg_cmd(['fake']))
    await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert child.killed and child.returncode==-9
