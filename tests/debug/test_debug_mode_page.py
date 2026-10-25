"""Exercise debug-mode page controls against the real JavaScript with a tiny DOM."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def test_debug_mode_controls_preserve_existing_dom_references():
    import re

    app = (ROOT / "pages/debug/app.js").read_text(encoding="utf-8")
    html = (ROOT / "pages/debug/index.html").read_text(encoding="utf-8")
    references = re.findall(r'document.getElementById\("([^"]+)"\)', app)
    for reference in references:
        assert f'id="{reference}"' in html
    assert 'role="switch"' in html
    # The mode controls share the title bar with the contact block.
    header = html.split("</header>", 1)[0]
    for element in ("mode-status", "mode-toggle", "mode-notice", "mode-retry"):
        assert f'id="{element}"' in header
    assert "群聊与普通消息已暂停" in app


def test_debug_mode_page_confirmed_updates_and_failure_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise page JavaScript")
    app = (ROOT / "pages/debug/app.js").read_text(encoding="utf-8")
    harness = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    value: "", textContent: "", className: "", disabled: false, hidden: false,
    childElementCount: 0, attrs: {}, handlers: {}, scrollTop: 0,
    addEventListener(name, handler) { this.handlers[name] = handler; },
    setAttribute(name, value) { this.attrs[name] = value; },
    removeAttribute(name) { delete this.attrs[name]; },
    replaceChildren() {},
  });
  return nodes.get(id);
}
let status = {debug_only: true, exclusive: false};
let getError = null;
let postError = null;
let response = {debug_only: false};
let postGate = null;
const calls = [];
const bridge = {
  ready: async () => {},
  apiGet: async (path) => {
    assert.equal(path, "debug/status");
    if (getError) throw getError;
    return status;
  },
  apiPost: async (path, body) => {
    calls.push({path, body});
    if (postGate) await postGate;
    if (postError) throw postError;
    return response;
  },
};
const context = vm.createContext({
  window: {AstrBotPluginPage: bridge, addEventListener() {}},
  document: {getElementById: node},
  setTimeout: () => 1, clearTimeout() {},
});
const source = APP_SOURCE;
(async () => {
  await vm.runInContext(`(async () => {${source}\n})()`, context);
  const toggle = node("mode-toggle");
  const notice = node("mode-notice");
  const retry = node("mode-retry");
  const tick = () => new Promise(resolve => setImmediate(resolve));
  assert.equal(toggle.attrs["aria-checked"], "true");
  assert.match(notice.textContent, /已暂停/);
  assert.equal(toggle.disabled, false);
  let release;
  postGate = new Promise(resolve => { release = resolve; });
  toggle.handlers.click();
  assert.equal(toggle.disabled, true);
  assert.equal(toggle.attrs["aria-checked"], "true", "no optimistic state change");
  toggle.handlers.click();
  assert.equal(calls.length, 1, "double clicks cannot race");
  release();
  await tick();
  assert.equal(calls[0].path, "debug/mode");
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({enabled: false}));
  assert.equal(toggle.attrs["aria-checked"], "false");
  assert.match(notice.textContent, /开启后暂停/);
  postGate = null;
  postError = new Error("save failed");
  toggle.handlers.click();
  await tick();
  assert.equal(calls[1].body.enabled, true);
  assert.equal(toggle.attrs["aria-checked"], undefined);
  assert.equal(toggle.disabled, true);
  assert.equal(retry.hidden, false);
  assert.match(notice.textContent, /save failed/);
  assert.match(notice.textContent, /当前模式未知/);
  status = {debug_only: true};
  retry.handlers.click();
  await tick();
  assert.equal(toggle.attrs["aria-checked"], "true");
  assert.equal(toggle.disabled, false);
  assert.equal(retry.hidden, true);
  getError = new Error("offline");
  retry.handlers.click();
  await tick();
  assert.equal(toggle.disabled, true);
  assert.equal(toggle.attrs["aria-checked"], undefined);
  assert.match(notice.textContent, /offline/);
  getError = null;
  status = {exclusive: false};
  retry.handlers.click();
  await tick();
  assert.equal(toggle.disabled, true, "missing debug_only must not become false");
  assert.match(notice.textContent, /debug_only/);
  status = {debug_only: false};
  retry.handlers.click();
  await tick();
  assert.equal(toggle.attrs["aria-checked"], "false");
  console.log("mode page controls passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-e", harness.replace("APP_SOURCE", json.dumps(app))],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
