"""Third-stage card preparation ownership without changing the card pipeline."""

import ast
from pathlib import Path


CARD_METHODS = (
    "_text_card_body",
    "_bounded_timeout",
    "_build_text_card_content",
    "_build_text_card_content_uncached",
)


def test_card_methods_live_in_a_coherent_mixin_not_the_plugin():
    root = Path(__file__).resolve().parents[2]
    layer_path = root / "core" / "card_service.py"
    assert layer_path.is_file(), "card preparation must have its own coherent layer"
    main_tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8"))
    plugin = next(node for node in main_tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "ParserXPlugin")
    mixin = next(node for node in layer_tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "CardServiceMixin")
    assert [ast.unparse(base) for base in plugin.bases] == [
        "CardServiceMixin", "DeliveryMixin", "DebugApiMixin", "Star",
    ]
    assert not mixin.bases
    plugin_methods = {node.name for node in plugin.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    mixin_methods = {node.name for node in mixin.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert mixin_methods == set(CARD_METHODS)
    assert plugin_methods.isdisjoint(CARD_METHODS)
    assert {
        "__init__", "initialize", "terminate", "_register_parser",
        "_send_parse_result", "_parse_with_cache", "on_message",
    } <= plugin_methods
    assert not {"__getattr__", "__getattribute__", "__init__"} & mixin_methods
    assert "DiskResultCache" not in ast.unparse(layer_tree)
    wrapper = next(node for node in plugin.body
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_parse_result")
    assert ast.unparse(wrapper.body[0]) == "await self._deliver_parse_result(event, result)"
    assert "DiskResultCache" in ast.unparse(wrapper)


def test_plugin_inherits_card_methods_directly_without_wrappers():
    from astrbot_plugin_parser_x.core.card_service import CardServiceMixin
    from astrbot_plugin_parser_x.main import ParserXPlugin

    assert issubclass(ParserXPlugin, CardServiceMixin)
    for name in CARD_METHODS:
        assert name not in ParserXPlugin.__dict__
        assert getattr(ParserXPlugin, name) is getattr(CardServiceMixin, name)
    for name in ("_text_card_body", "_bounded_timeout"):
        assert isinstance(CardServiceMixin.__dict__[name], staticmethod)


def test_card_types_and_delivery_marker_keep_direct_identity():
    from astrbot_plugin_parser_x import main
    from astrbot_plugin_parser_x.core import card_service, delivery

    assert card_service._GeneratedCardContent is delivery._GeneratedCardContent
    assert main._GeneratedCardContent is card_service._GeneratedCardContent
    assert main.ImageContent is card_service.ImageContent
    assert main.CommentDocument is card_service.CommentDocument
    assert main.BiliCommentDocument is card_service.BiliCommentDocument
