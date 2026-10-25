"""小黑盒长文帖子（``type == "html"``）的正文转换。"""

import pytest
from astrbot_plugin_parser_x.core.parsers.xiaoheihe import (
    _html_to_body,
    _image_key,
    _is_empty_game_entry,
    _HtmlBodyParser,
)


def test_html_paragraphs_become_lines():
    html = "<p>第一段</p><p>第二段</p>"
    text, images = _html_to_body(html)
    assert text == "第一段\n第二段"
    assert images == []


def test_inline_tags_are_stripped_but_text_kept():
    html = "<p>这里有 <b>粗体</b> 和 <i>斜体</i></p>"
    text, _ = _html_to_body(html)
    assert text == "这里有 粗体 和 斜体"


def test_plain_angle_bracket_is_preserved():
    """作者手写的 <10.21> 不是合法标签，必须原样保留。"""
    html = "<p><10.21>等等党大胜利</p>"
    text, _ = _html_to_body(html)
    assert text == "<10.21>等等党大胜利"


def test_image_data_original_is_collected():
    html = (
        '<p>看图</p><p><img class="lazy" '
        'data-original="https://imgheybox.max-c.com/web/bbs/2026/10/15/abc/thumb.jpeg" '
        'data-height="177" data-width="886"/></p>'
    )
    text, images = _html_to_body(html)
    assert text == "看图"
    assert images == ["https://imgheybox.max-c.com/web/bbs/2026/10/15/abc/thumb.jpeg"]


@pytest.mark.parametrize("class_name", ["emoji", "emoticon", "icon", "ui-icon", "logo", "avatar", "emoji-face", "icon-share"])
def test_explicit_inline_asset_is_not_a_body_picture(class_name):
    from astrbot_plugin_parser_x.core.parsers.xiaoheihe import _html_to_flow

    html = f'<p>前<img class="lazy {class_name}" src="https://cdn.example/asset.png" alt="[笑]">后</p>'
    expected = "前[笑]后" if class_name in {"emoji", "emoticon", "emoji-face"} else "前后"
    assert _html_to_body(html) == (expected, [])
    assert _html_to_flow(html) == [("text", expected)]


@pytest.mark.parametrize("attributes", [
    "hidden", 'aria-hidden="true"', 'style="display: none"',
    'style="visibility: hidden !important"', 'width="1" height="1"',
    'style="width:1px; height:1px"',
])
def test_hidden_or_tracking_image_is_not_a_body_picture(attributes):
    from astrbot_plugin_parser_x.core.parsers.xiaoheihe import _html_to_flow

    html = f'<p>前<img {attributes} src="https://cdn.example/pixel.png">后</p>'
    assert _html_to_body(html) == ("前后", [])
    assert _html_to_flow(html) == [("text", "前后")]


@pytest.mark.parametrize("attributes", [
    "", 'width="24" height="24"', 'width="1" height="200"',
    'class="iconic-photo emojiish-photo"',
    'class="lazy" src="https://cdn.example/placeholder.png"',
])
def test_genuine_pictures_remain_eligible(attributes):
    url = "https://arbitrary.example/actual-meme.png"
    html = f'<p>正文</p><img {attributes} data-original="{url}">'
    assert _html_to_body(html) == ("正文", [url])


def test_relative_image_src_is_ignored():
    html = '<p><img src="data:image/png;base64,AAA"/></p>'
    _, images = _html_to_body(html)
    assert images == []


def test_unordered_list_becomes_bullet_lines():
    html = "<blockquote><ul><li><p>新品节:10月20日</p></li><li><p>尖叫游戏节:10月27日</p></li></ul></blockquote>"
    text, _ = _html_to_body(html)
    assert text.splitlines() == ["· 新品节:10月20日", "· 尖叫游戏节:10月27日"]


def test_list_item_with_inline_text():
    html = "<ul><li>纯文字条目</li></ul>"
    text, _ = _html_to_body(html)
    assert text == "· 纯文字条目"


def test_special_characters_are_unescaped():
    html = "<p>A &amp; B &lt; C</p>"
    text, _ = _html_to_body(html)
    assert text == "A & B < C"


def test_non_breaking_space_becomes_plain_space():
    html = "<p>前&nbsp;后</p>"
    text, _ = _html_to_body(html)
    assert text == "前 后"


def test_blank_lines_are_collapsed():
    html = "<p>甲</p><p> </p><p>乙</p>"
    text, _ = _html_to_body(html)
    assert text == "甲\n乙"


class TestEmptyGameEntries:
    """长文里的 ``第N款`` 游戏条目：没内容的必须整条丢掉。"""

    def _parse(self, html: str):
        parser = _HtmlBodyParser()
        parser.feed(html)
        parser.close()
        return parser

    def test_game_card_without_anything_is_dropped(self):
        html = (
            "<p>第4款</p>"
            '<p><img class="mention-game" data-gameid="473950" data-type="game_card"/></p>'
            "<p>第5款</p>"
            '<p><img class="mention-game" data-gameid="2421540" data-type="game_card"/></p>'
        )
        text, _ = _html_to_body(html)
        assert text == ""

    def test_game_card_with_description_text_is_kept(self):
        html = (
            "<p>第一款</p>"
            '<p><img class="mention-game" data-gameid="1" data-type="game_card"/></p>'
            "<blockquote><p>一款民国谍战影游，多分支多结局。</p></blockquote>"
        )
        text, _ = _html_to_body(html)
        assert "第一款" in text
        assert "一款民国谍战影游" in text

    def test_game_card_with_screenshot_is_kept(self):
        html = (
            "<p>第1款</p>"
            '<p><img class="mention-game" data-gameid="701160" data-type="game_card"/></p>'
            '<p><img data-description="Sker Ritual 界面截图" '
            'data-original="https://imgheybox.max-c.com/web/bbs/2026/10/20/y/thumb.png"/></p>'
        )
        text, images = _html_to_body(html)
        assert text == "第1款"
        assert len(images) == 1

    def test_game_card_followed_only_by_text_is_kept(self):
        html = (
            "<p>第2款</p>"
            '<p><img class="mention-game" data-gameid="2" data-type="game_card"/></p>'
            "<p>原价 62，现在免费。</p>"
        )
        text, _ = _html_to_body(html)
        assert "第2款" in text
        assert "原价 62" in text

    def test_counter_without_media_kept(self):
        """普通编号段落（没有紧跟游戏卡）不应被误删。"""
        html = "<p>第1款</p><p>这里是正文说明</p>"
        text, _ = _html_to_body(html)
        assert "第1款" in text
        assert "这里是正文说明" in text

    def test_is_empty_game_entry_handles_tail(self):
        parser = self._parse(
            "<p>第9款</p>"
            '<p><img class="mention-game" data-gameid="1" data-type="game_card"/></p>'
        )
        assert _is_empty_game_entry(parser.chunks, 0) is True


def test_image_key_normalises_path():
    assert _image_key(
        "https://imgheybox.max-c.com/web/bbs/2026/10/15/abc/thumb.jpeg"
    ) == _image_key("https://imgheybox.max-c.com/bbs/2026/10/15/abc/thumb.jpeg")
    assert _image_key("https://a.com/x/y/z.png?v=1") == "y/z.png"


def test_malformed_html_falls_back_to_text():
    text, images = _html_to_body("<p>未闭合 <b>标签")
    assert "未闭合" in text
    assert images == []


class TestLongArticleInterception:
    """长文帖只回提示，不做完整解析。

    判据是「没有头图 且 正文够长」，不再看 link_tag —— 平台的 link_tag
    会把正常图帖也标成 11，只看它会把带图的帖子误拦。
    """

    @staticmethod
    def _link(text_chars: int, thumb: str | None) -> dict:
        import json

        return {
            "thumb": thumb,
            "text": json.dumps([{"type": "text", "text": "字" * text_chars}]),
        }

    @pytest.mark.parametrize(
        "text_chars, thumb, expected",
        [
            # 长正文 + 无头图 → 文章型长文
            (1058, None, True),
            (1585, None, True),
            # 正文很短 → 正常帖，哪怕没有头图
            (262, None, False),
            (134, None, False),
            # 有头图 → 正常帖，哪怕正文很长
            (1200, "https://img.example.com/a.jpg", False),
        ],
    )
    def test_is_long_article(self, text_chars, thumb, expected):
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser

        assert XiaoHeiHeParser._is_long_article(self._link(text_chars, thumb)) is expected

    @pytest.mark.parametrize("value", [None, "", "abc", [], {}])
    def test_is_long_article_tolerates_junk(self, value):
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser

        assert XiaoHeiHeParser._is_long_article({"text": value, "thumb": value}) is False

    def test_missing_fields_is_not_article(self):
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser

        assert XiaoHeiHeParser._is_long_article({}) is False


class TestCoverFlowInvariant:
    """头图不计入正文流，所以 contents 比 flow 图数最多多 1，多出的那张就是头图。

    这个等量关系是解析链路的一个隐含约定：``_parse_body`` 里 head 图用
    ``contents.insert(0, ...)`` 插在前面，不会写进 ``flow``。一旦哪天两侧
    的构建逻辑不同步（图丢了、图重复进了 contents），这里就会失败。
    """

    @staticmethod
    def _link(segments: list[dict], thumb: str | None) -> dict:
        import json

        return {"text": json.dumps(segments), "thumb": thumb}

    @staticmethod
    def _parse(link: dict, downloader) -> tuple[int, int]:
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe import XiaoHeiHeParser

        parser = XiaoHeiHeParser(downloader=downloader, config={"cookies": {}})
        _text, contents, flow, _cover = parser._parse_body(link)
        flow_images = sum(1 for kind, _value in flow if kind == "image")
        return len(contents), flow_images

    def test_gap_is_one_when_cover_present(self, monkeypatch):
        # 有头图：2 张正文图 + 1 张头图 = 3 个 contents，flow 里 2 张图
        link = self._link(
            [
                {"type": "text", "text": "<p>正文</p>"},
                {"type": "img", "url": "https://img.example.com/a.jpg"},
                {"type": "img", "url": "https://img.example.com/b.jpg"},
            ],
            "https://img.example.com/cover.jpg",
        )
        contents, flow_images = self._parse(link, _StubDownloader())
        assert contents - flow_images == 1

    def test_gap_is_zero_without_cover(self):
        # 无头图：contents 与 flow 图数完全相等
        link = self._link(
            [
                {"type": "text", "text": "<p>正文</p>"},
                {"type": "img", "url": "https://img.example.com/a.jpg"},
                {"type": "img", "url": "https://img.example.com/b.jpg"},
            ],
            None,
        )
        contents, flow_images = self._parse(link, _StubDownloader())
        assert contents == flow_images

    def test_gap_matches_cover_flag(self):
        # 两种形态互斥：要么差 1（有头图），要么差 0（无头图），不会出现别的值
        for thumb, expected_gap in (
            ("https://img.example.com/cover.jpg", 1),
            (None, 0),
            ("", 0),
        ):
            link = self._link(
                [{"type": "img", "url": "https://img.example.com/a.jpg"}],
                thumb,
            )
            contents, flow_images = self._parse(link, _StubDownloader())
            assert contents - flow_images == expected_gap


class _StubDownloader:
    """``_parse_body`` 只用到 downloader 来构造待下载任务，不真正发起请求。"""

    def download_img(self, url: str, **_kwargs) -> str:
        return url

    def download_video(self, url: str, **_kwargs) -> str:
        return url


class TestPinnedCommentDetection:
    """置顶评论判定。

    小黑盒的置顶信号有两套且各有失效场景，这里用真实帖子的字段组合锁住行为：
    ``is_top`` 是热门排序标记（非置顶帖里也能有十几条），绝不能拿来判断。
    """

    @staticmethod
    def _floors(rows: list[dict]) -> list[dict]:
        return [{"comment": [row]} for row in rows]

    def _detect(self, rows: list[dict]) -> bool:
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe_comment import (
            XiaoHeiHeCommentFeed,
        )

        floors = self._floors(rows)
        return XiaoHeiHeCommentFeed._is_pinned_head(rows[0], floors)

    def test_top_comment_flag_marks_pinned(self):
        # 194561740：楼层恰好正序，只能靠 top_comment 认出来
        rows = [
            {"floor_num": 2, "top_comment": 1},
            {"floor_num": 4},
        ]
        assert self._detect(rows) is True

    def test_out_of_order_floor_marks_pinned(self):
        # 193973610：top_comment 整个字段缺失，靠首条楼层不是最小值认出来
        rows = [
            {"floor_num": 7},
            {"floor_num": 22},
            {"floor_num": 3},
        ]
        assert self._detect(rows) is True

    def test_is_top_alone_never_means_pinned(self):
        # is_top 是热门排序标记：置顶帖里也有十几条同时为 1，不能据此判断
        rows = [
            {"floor_num": 3, "is_top": 1},
            {"floor_num": 55, "is_top": 1},
            {"floor_num": 49, "is_top": 1},
        ]
        assert self._detect(rows) is False

    def test_regular_first_floor_is_not_pinned(self):
        rows = [{"floor_num": 2}, {"floor_num": 4}, {"floor_num": 3}]
        assert self._detect(rows) is False

    def test_empty_rows_are_not_pinned(self):
        assert self._detect([{}]) is False


class TestCommentHtmlCleanup:
    """评论正文里的 @人 是 HTML，必须只留文字。

    小黑盒把 ``@某人`` 下发成
    ``<a data-user-id=".." href="https://api.xiaoheihe.cn/open_inapp/#..">@某人</a>``，
    早期版本原样拼进正文，用户看到的是整串 href。
    """

    def _clean(self, text: str) -> str:
        from astrbot_plugin_parser_x.core.parsers.xiaoheihe_comment import (
            _clean_comment_html,
        )

        return _clean_comment_html(text)

    def test_at_mention_keeps_only_text(self):
        raw = (
            '<a data-user-id="75853867" '
            'href="https://api.xiaoheihe.cn/open_inapp/#heybox://%7B%22protocol_type%22" '
            'target="_blank">@异世回忆</a>'
        )
        assert self._clean(raw) == "@异世回忆"

    def test_at_mention_mixed_with_text(self):
        raw = (
            '可以看这位大佬的教程<a data-user-id="75853867" '
            'href="https://api.xiaoheihe.cn/open_inapp/#x" target="_blank">@异世回忆</a>'
        )
        assert self._clean(raw) == "可以看这位大佬的教程@异世回忆"

    def test_plain_text_untouched(self):
        assert self._clean("没有标签的普通评论") == "没有标签的普通评论"

    def test_other_tags_are_dropped(self):
        assert self._clean("第一行<br>第二行") == "第一行第二行"
