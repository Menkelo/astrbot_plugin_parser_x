from __future__ import annotations

import asyncio
import io

import zxingcpp
from PIL import Image

from core.comment_canvas import CommentAuthor, CommentEntry, CommentRichPart
from core.comment_filter import CommentFilter
from core.comment_settings import CommentFilterSettings
from core.parsers.bilibili.comment_canvas import (
    BiliAuthorBadge,
    BiliCommentEntry,
    BiliRichPart,
)


def _settings(**overrides) -> CommentFilterSettings:
    raw = {
        "enabled": True,
        "mention_mode": "balanced",
        "qrcode": True,
        "ads": True,
        "duplicates": True,
        "low_information": False,
        "ad_threshold": 4,
        **overrides,
    }
    return CommentFilterSettings.from_config({"comments": {"filter": raw}})


def _entry(
    text: str,
    *,
    images: list[str] | None = None,
    reply: CommentEntry | None = None,
) -> CommentEntry:
    return CommentEntry(
        author=CommentAuthor("用户"),
        content=[CommentRichPart("text", text)],
        images=list(images or []),
        first_reply=reply,
    )


def _text(entry) -> str:
    return "".join(part.text for part in entry.content if part.kind != "line-break")


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class _FakeParser:
    def __init__(self, images: dict[str, bytes] | None = None):
        self.images = images or {}
        self.calls: list[str] = []

    async def http_get(self, url: str, **_kwargs):
        self.calls.append(url)
        content = self.images.get(url)
        if content is None:
            return _FakeResponse(b"", 404)
        return _FakeResponse(content)


def _filter(settings: CommentFilterSettings | None = None, parser=None):
    return CommentFilter(
        parser or _FakeParser(),
        settings or _settings(qrcode=False),
        platform="测试",
    )


def _qr_png(value: str = "https://spam.example") -> bytes:
    barcode = zxingcpp.create_barcode(value, zxingcpp.BarcodeFormat.QRCode)
    raw = zxingcpp.write_barcode_to_image(barcode, scale=4)
    image = Image.frombytes(
        "L",
        (raw.shape[1], raw.shape[0]),
        bytes(memoryview(raw)),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _plain_png() -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGB", (160, 120), "white") as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_balanced_mentions_keep_real_text_and_drop_summons():
    entries = [
        _entry("@作者 这个方法确实有效"),
        _entry("@作者"),
        _entry("@甲 @乙 快来看"),
        _entry("回复 @某人：这个方法也有效"),
    ]

    result = asyncio.run(_filter().apply(entries, limit=10))

    assert [_text(entry) for entry in result] == [
        "这个方法确实有效",
        "这个方法也有效",
    ]


def test_strict_mentions_ignore_structural_reply_prefix_only():
    result = asyncio.run(
        _filter(_settings(mention_mode="strict", qrcode=False)).apply(
            [
                _entry("回复 @某人：正常回复"),
                _entry("正文里 @某人 提醒一下"),
            ],
            limit=10,
        )
    )

    assert [_text(entry) for entry in result] == ["正常回复"]


def test_ad_filter_uses_combinations_instead_of_single_keywords():
    result = asyncio.run(
        _filter().apply(
            [
                _entry("微信这个功能不好用"),
                _entry("微信: abcde"),
                _entry("加我微信: abcde"),
                _entry("免费领取 https://spam.example.com"),
                _entry("资料在 https://docs.python.org"),
                _entry("兼职日赚，私聊QQ:123456"),
            ],
            limit=10,
        )
    )

    assert [_text(entry) for entry in result] == [
        "微信这个功能不好用",
        "资料在 https://docs.python.org",
    ]


def test_control_characters_are_cleaned_and_normalized_duplicates_are_removed():
    result = asyncio.run(
        _filter().apply(
            [
                _entry("好\u200b评"),
                _entry("好评！！！"),
                _entry("正常\x00评论"),
            ],
            limit=10,
        )
    )

    assert [_text(entry) for entry in result] == ["好评", "正常评论"]


def test_low_information_filter_is_opt_in():
    default_result = asyncio.run(
        _filter().apply([_entry("..."), _entry("666")], limit=10)
    )
    enabled_result = asyncio.run(
        _filter(_settings(qrcode=False, low_information=True)).apply(
            [_entry("..."), _entry("666"), _entry("真好看")],
            limit=10,
        )
    )

    assert [_text(entry) for entry in default_result] == ["...", "666"]
    assert [_text(entry) for entry in enabled_result] == ["真好看"]


def test_qrcode_comments_are_removed_and_later_candidates_fill_the_limit():
    qr_url = "https://img.example/qr.png"
    normal_url = "https://img.example/normal.png"
    parser = _FakeParser({qr_url: _qr_png(), normal_url: _plain_png()})
    comment_filter = _filter(_settings(), parser)

    result = asyncio.run(
        comment_filter.apply(
            [
                _entry("二维码广告", images=[qr_url]),
                _entry("正常图片评论", images=[normal_url]),
                _entry("补位评论"),
            ],
            limit=2,
        )
    )

    assert [_text(entry) for entry in result] == ["正常图片评论", "补位评论"]
    assert parser.calls == [qr_url, normal_url]


def test_qrcode_detection_failure_is_fail_open_and_cached():
    broken_url = "https://img.example/broken.png"
    parser = _FakeParser({broken_url: b"not-an-image"})
    comment_filter = _filter(_settings(), parser)

    result = asyncio.run(
        comment_filter.apply(
            [
                _entry("第一条", images=[broken_url]),
                _entry("第二条", images=[broken_url]),
            ],
            limit=2,
        )
    )

    assert [_text(entry) for entry in result] == ["第一条", "第二条"]
    assert parser.calls == [broken_url]


def test_filtered_nested_reply_does_not_remove_the_main_comment():
    main = _entry("主评论正常", reply=_entry("加我微信: abcde"))

    result = asyncio.run(_filter().apply([main], limit=1))

    assert len(result) == 1
    assert result[0].first_reply is None


def test_shared_filter_accepts_bilibili_comment_model():
    entry = BiliCommentEntry(
        author=BiliAuthorBadge("用户"),
        content=[BiliRichPart("text", "@作者 B站正常回复")],
    )

    result = asyncio.run(_filter().apply([entry], limit=1))

    assert len(result) == 1
    assert _text(result[0]) == "B站正常回复"
