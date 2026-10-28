import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_parser_x.core.comment_canvas import (
    CommentAuthor,
    CommentDocument,
    CommentEntry,
    CommentRichPart,
    CommentTheme,
)
from astrbot_plugin_parser_x.core.unified_renderer import UnifiedCardRenderer


class CaptureService:
    def __init__(self):
        self.html = ""
        self.htmls = []

    async def render(self, out_path, html, **kwargs):
        self.html = html
        self.htmls.append(html)
        return Path(out_path)


def document(*parts):
    return CommentDocument(
        theme=CommentTheme("测试"), work_title="", cover="", total_text="",
        entries=[CommentEntry(CommentAuthor("作者"), list(parts))],
    )


class Resolver:
    def __init__(self, known=None, resolved=None, delay=0):
        self.known = set(known or ())
        self.resolved = dict(resolved or {})
        self.delay = delay
        self.calls = []

    def known_tokens(self, platform):
        return self.known

    async def resolve(self, platform, tokens, sources=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(("resolve", platform, set(tokens)))
        self.last_sources = dict(sources or {})
        return {token: self.resolved[token] for token in tokens if token in self.resolved}

    async def resolve_urls(self, platform, sources):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(("resolve_urls", platform, dict(sources)))
        return {token: self.resolved[token] for token in sources if token in self.resolved}


def test_warm_uses_explicit_platform_and_resolves_emoji_text_and_directory_url(tmp_path):
    service = CaptureService()
    resolver = Resolver(
        known={"[笑R]", "[目录]"},
        resolved={"[笑R]": "data:image/png;base64,known", "[目录]": "data:image/png;base64,dir"},
    )
    renderer = UnifiedCardRenderer(service)
    renderer.emote_resolver = resolver
    doc = document(
        CommentRichPart("emoji-text", text="[笑R]"),
        CommentRichPart("emote", text="[目录]", url="https://cdn.example/dir.png"),
    )
    asyncio.run(renderer.render_card(tmp_path / "x.png", platform_name="小红书", platform_key="xiaohongshu", author_name="a", text="", comment_document=doc))
    assert ("resolve", "xiaohongshu", {"[笑R]", "[目录]"}) in resolver.calls
    assert resolver.last_sources == {"[目录]": "https://cdn.example/dir.png"}
    assert not any(call[0] == "resolve_urls" for call in resolver.calls)
    assert 'src="data:image/png;base64,known"' in service.html
    assert 'src="data:image/png;base64,dir"' in service.html
    assert "cdn.example" not in service.html


def test_unmanaged_comment_images_are_not_downloaded_or_conflated(tmp_path):
    service = CaptureService()
    resolver = Resolver(resolved={"[图片]": "data:image/png;base64,last"})
    renderer = UnifiedCardRenderer(service)
    renderer.emote_resolver = resolver
    doc = document(
        CommentRichPart("emote", text="[图片]", url="http://127.0.0.1/first.png"),
        CommentRichPart("emote", text="[图片]", url="https://cdn.example/second.png"),
    )
    asyncio.run(renderer.render_card(tmp_path / "x.png", platform_name="微博", platform_key="weibo", author_name="a", text="", comment_document=doc))
    assert not resolver.calls
    assert "http://127.0.0.1/first.png" in service.html
    assert "https://cdn.example/second.png" in service.html


def test_managed_warmup_has_aggregate_deadline(tmp_path, monkeypatch):
    import astrbot_plugin_parser_x.core.unified_renderer as module
    monkeypatch.setattr(module, "EMOTE_WARMUP_SECONDS", 0.01, raising=False)
    service = CaptureService()
    renderer = UnifiedCardRenderer(service)
    renderer.emote_resolver = Resolver(known={"[慢]"}, delay=0.1)
    doc = document(CommentRichPart("emoji-text", text="[慢]"))
    async def run():
        await asyncio.wait_for(renderer.render_card(tmp_path / "x.png", platform_name="酷安", platform_key="coolapk", author_name="a", text="", comment_document=doc), timeout=0.05)
    asyncio.run(run())
    assert "[慢]" in service.html


def test_managed_warmup_caps_requested_tokens(tmp_path):
    service = CaptureService()
    tokens = {f"[表情{i}]" for i in range(50)}
    resolver = Resolver(known=tokens)
    renderer = UnifiedCardRenderer(service)
    renderer.emote_resolver = resolver
    doc = document(*(CommentRichPart("emoji-text", text=token) for token in sorted(tokens)))
    asyncio.run(renderer.render_card(tmp_path / "x.png", platform_name="酷安", platform_key="coolapk", author_name="a", text="", comment_document=doc))
    assert len(resolver.calls) == 1
    assert len(resolver.calls[0][2]) == 32
    assert all(token in service.html for token in tokens)


def test_build_html_emote_without_warmup_uses_safe_default_context():
    renderer = UnifiedCardRenderer(CaptureService())
    doc = document(CommentRichPart("emote", text="[直接]", url="https://cdn.example/direct.png"))
    html = renderer.build_html(
        platform_name="微博", platform_key="weibo", author_name="a", text="",
        comment_document=doc,
    )
    assert "https://cdn.example/direct.png" in html
    assert "[直接]" in html


def test_failed_managed_emote_is_text_but_unknown_remote_emote_is_preserved(tmp_path):
    service = CaptureService()
    renderer = UnifiedCardRenderer(service)
    renderer.emote_resolver = Resolver(known={"[坏R]"})
    doc = document(
        CommentRichPart("emote", text="[坏R]", url="https://cdn.example/managed.png"),
        CommentRichPart("emote", text="[远程]", url="https://cdn.example/remote.png"),
    )
    asyncio.run(renderer.render_card(tmp_path / "x.png", platform_name="微博", platform_key="weibo", author_name="a", text="", comment_document=doc))
    assert "cdn.example/managed.png" not in service.html
    assert "https://cdn.example/remote.png" in service.html
    assert "[坏R]" in service.html


@pytest.mark.asyncio
async def test_emote_state_does_not_cross_concurrent_renders(tmp_path):
    first, second, shared = CaptureService(), CaptureService(), CaptureService()
    r1 = UnifiedCardRenderer(shared)
    r2 = r1
    r1.emote_resolver = Resolver(
        known={"[一]", "[二]"},
        resolved={"[一]": "data:image/png;base64,one", "[二]": "data:image/png;base64,two"},
        delay=.01,
    )
    d1, d2 = document(CommentRichPart("emoji-text", text="[一]")), document(CommentRichPart("emoji-text", text="[二]"))
    await asyncio.gather(
        r1.render_card(tmp_path / "1.png", platform_name="A", platform_key="xiaohongshu", author_name="a", text="", comment_document=d1),
        r2.render_card(tmp_path / "2.png", platform_name="B", platform_key="bilibili", author_name="b", text="", comment_document=d2),
    )
    html_one, html_two = shared.htmls
    assert "data:image/png;base64,one" in html_one and "[二]" not in html_one
    assert "data:image/png;base64,two" in html_two and "[一]" not in html_two
