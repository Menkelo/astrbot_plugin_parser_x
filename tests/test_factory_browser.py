import sys
from types import SimpleNamespace
import pytest
from core.browser_backend import LocalRenderBackend

@pytest.mark.asyncio
async def test_missing_playwright_has_actionable_install_message(monkeypatch):
    monkeypatch.setitem(sys.modules, 'playwright.async_api', None)
    backend = LocalRenderBackend()
    with pytest.raises(RuntimeError, match='python -m pip install playwright'):
        await backend.ensure_ready()
    assert backend._browser is None

@pytest.mark.asyncio
async def test_missing_chromium_has_install_instruction_and_stops_driver(monkeypatch):
    stopped = []
    async def launch(**kwargs):
        raise RuntimeError("Executable doesn't exist at /missing/chromium")
    async def stop(): stopped.append(True)
    driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=stop)
    async def start(): return driver
    monkeypatch.setitem(sys.modules, 'playwright.async_api', SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=start)))
    backend = LocalRenderBackend()
    with pytest.raises(RuntimeError, match='python -m playwright install chromium'):
        await backend.ensure_ready()
    assert stopped == [True]
    assert backend._browser is None
