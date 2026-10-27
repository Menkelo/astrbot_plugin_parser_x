"""Zhihu cards must carry the author avatar into the card signature row.

The unified renderer reads ``result.extra['text_card_avatar']`` only
(core/card_service.py). Zhihu built its Author without ever calling
apply_author_block, so every Zhihu card fell back to an initial-letter avatar.
"""
import asyncio

import pytest

from astrbot_plugin_parser_x.core.parsers.zhihu import ZhihuParser


class StubTask:
    """Loop-free stand-in for a downloader task (suites close the global loop)."""

    def done(self):
        return True

    def result(self, *args, **kwargs):
        return None

    def cancel(self):
        return True

    def add_done_callback(self, callback):
        callback(self)


class FakeDownloader:
    def download_img(self, _url, **_kwargs):
        return StubTask()

    def download_video(self, _url, **_kwargs):
        return StubTask()


def make_parser():
    return ZhihuParser({"cookies": {"zhihu_cookie": ""}}, FakeDownloader())


AVATAR = "https://pic1.zhimg.com/9d07465233f2f8c5df073d6a8f0c098a_l.jpg?source=2c26e567"


def test_zhihu_assemble_sets_card_avatar():
    parser = make_parser()
    result = asyncio.run(parser._assemble(
        "<p>正文</p>", "标题",
        {"name": "思考回路", "avatar_url": AVATAR},
        "https://www.zhihu.com/question/1/answer/2",
    ))
    assert result.extra.get("text_card_avatar") == AVATAR


def test_zhihu_assemble_tolerates_missing_avatar():
    parser = make_parser()
    result = asyncio.run(parser._assemble(
        "<p>正文</p>", "标题", {"name": "匿名用户"},
        "https://www.zhihu.com/question/1/answer/2",
    ))
    assert not result.extra.get("text_card_avatar")
