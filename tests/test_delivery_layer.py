"""Second-stage delivery ownership without wrappers or duplicated pipelines."""

import ast
from pathlib import Path


DELIVERY_METHODS = (
    "_download_content",
    "_convert_to_seg",
    "_comment_forward_segments",
    "_format_text_fallback",
    "_format_media_summary",
    "_transcode_to_h264",
    "_send_card_reply",
    "_render_and_send_text_card",
    "_remux_fresh_copy",
    "_resend_with_fresh_media",
    "_send_video_segment",
    "_send_delivery_plan",
    "_deliver_parse_result",
)
MAIN_METHODS = {
    "_send_parse_result", "_build_text_card_content",
    "_build_text_card_content_uncached", "_text_card_body", "_bounded_timeout",
    "_collect_parser_matches", "_parse_with_cache", "on_message",
}


def test_delivery_methods_live_in_a_coherent_mixin_not_the_plugin():
    root = Path(__file__).resolve().parents[1]
    layer_path = root / "core" / "delivery.py"
    assert layer_path.is_file(), "media delivery must have its own coherent layer"
    main_tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8"))
    plugin = next(node for node in main_tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "ParserXPlugin")
    mixin = next(node for node in layer_tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "DeliveryMixin")
    assert [ast.unparse(base) for base in plugin.bases] == [
        "DeliveryMixin", "DebugApiMixin", "Star",
    ]
    assert not mixin.bases
    plugin_methods = {node.name for node in plugin.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    mixin_methods = {node.name for node in mixin.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert mixin_methods == set(DELIVERY_METHODS)
    assert plugin_methods.isdisjoint(DELIVERY_METHODS)
    assert MAIN_METHODS <= plugin_methods
    assert "__getattr__" not in ast.unparse(layer_tree)
    assert "__getattribute__" not in ast.unparse(layer_tree)
    wrapper = next(node for node in plugin.body
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_parse_result")
    assert ast.unparse(wrapper.body[0]) == "await self._deliver_parse_result(event, result)"
    assert "DiskResultCache" in ast.unparse(wrapper)
    assert "DiskResultCache" not in ast.unparse(layer_tree)


def test_plugin_inherits_delivery_methods_directly_without_wrappers():
    from astrbot_plugin_parser_x.core.delivery import DeliveryMixin
    from astrbot_plugin_parser_x.main import ParserXPlugin

    assert issubclass(ParserXPlugin, DeliveryMixin)
    for name in DELIVERY_METHODS:
        assert name not in ParserXPlugin.__dict__
        assert getattr(ParserXPlugin, name) is getattr(DeliveryMixin, name)


def test_shared_card_marker_and_file_guard_are_direct_reexports():
    from astrbot_plugin_parser_x import main
    from astrbot_plugin_parser_x.core import delivery
    from astrbot_plugin_parser_x.core.debug_api import PLUGIN_NAME

    assert main._GeneratedCardContent is delivery._GeneratedCardContent
    assert main._prepare_media_file is delivery._prepare_media_file
    assert main.PLUGIN_NAME == PLUGIN_NAME == "astrbot_plugin_parser_x"
