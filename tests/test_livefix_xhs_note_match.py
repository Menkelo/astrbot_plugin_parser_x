"""XHS note matching must not demand an exact set equality of identifiers.

Live pages happen to key ``noteDetailMap`` by note id, but the map key is not a
guaranteed identity: other shapes key it by ``other``/``currentNote``. The old
``ids == {xhs_id}`` comparison then treated a real note as a foreign one and
dropped it, so parsing failed even though the note was right there.
"""
from astrbot_plugin_parser_x.core.parsers.xiaohongshu import XiaoHongShuParser

NOTE = {
    "type": "normal",
    "title": "标题",
    "desc": "正文",
    "time": 1700000000,
    "user": {"nickname": "作者", "avatar": "https://img.example.com/a.jpg"},
    "imageList": [{"urlDefault": "https://img.example.com/c.jpg"}],
}


def make_parser():
    return XiaoHongShuParser({"cache_dir": "."}, object())


def test_unknown_map_key_still_yields_the_note():
    parser = make_parser()
    state = {"value": {"note": {"noteDetailMap": {
        "value": {"other": {"note": {"value": NOTE}}},
    }}}}
    assert parser._extract_note_from_state(state, "note123") == NOTE


def test_note_id_inside_note_matches_even_with_foreign_map_key():
    parser = make_parser()
    identified = {**NOTE, "noteId": "note123"}
    state = {"note": {"noteDetailMap": {"other": {"note": identified}}}}
    assert parser._extract_note_from_state(state, "note123") == identified


def test_conflicting_note_id_is_rejected():
    parser = make_parser()
    foreign = {**NOTE, "noteId": "someoneelse"}
    state = {"note": {"noteDetailMap": {"other": {"note": foreign}}}}
    assert parser._extract_note_from_state(state, "note123") == {}
