"""NGA 表情库回归：token 不能丢，也不能变成裸文本。"""
from core.parsers.nga import bbcode_flow
from core.parsers.nga_comment import _rich_parts
from core.platform_emotes import contains_platform_emotes, fallback_emote_map, resolve_emote_url


def test_nga_emote_tokens_survive_bbcode_cleanup():
    flow = bbcode_flow("你好[s:ac:汗]再见 [b]粗体[/b] [s:a2:doge]", "https://img.nga.cn/attachments/")
    assert flow == [("text", "你好[s:ac:汗]再见 粗体 [s:a2:doge]")]


def test_nga_bbcode_flow_still_drops_other_markup():
    flow = bbcode_flow("[quote]引用[/quote][img]abc.png[/img]尾巴", "https://img.nga.cn/attachments/")
    # [quote] 这类纯 BBCode 标签照旧剔除，[img] 照旧拆成独立图片段。
    assert ("text", "引用") in flow and ("text", "尾巴") in flow
    assert ("image", "https://img.nga.cn/attachments/abc.png") in flow
    assert not any("[/quote]" in value or "[img]" in value for kind, value in flow if kind == "text")


def test_nga_catalog_resolves_known_tokens_only():
    catalog = fallback_emote_map("nga")
    assert catalog["[s:ac:汗]"].endswith("/ac34.png")
    assert catalog["[s:a2:doge]"].endswith("/a2_27.png")
    assert resolve_emote_url("nga", "[s:ac:不存在]", catalog) == ""
    assert contains_platform_emotes("[s:ac:汗]", "nga") is True


def test_nga_comment_keeps_emotes_as_images():
    parts = _rich_parts("这样[s:ac:汗]的图")
    assert [part.kind for part in parts] == ["text", "emote", "text"]
    assert parts[1].text == "[s:ac:汗]"
    assert parts[1].url.endswith("/ac34.png")
