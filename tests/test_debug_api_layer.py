"""First-stage debug API ownership without duplicating the plugin pipeline."""

import ast
from pathlib import Path


DEBUG_API_METHODS = (
    "_register_debug_page_apis",
    "_debug_page_owner",
    "_debug_page_error_response",
    "debug_page_status",
    "debug_page_mode",
    "debug_page_start",
    "debug_page_events",
    "debug_page_cancel",
    "debug_page_media_preview",
    "debug_page_media",
    "_run_debug_page_parse",
)


def test_debug_api_methods_live_in_a_small_mixin_not_the_plugin():
    root = Path(__file__).resolve().parents[1]
    layer_path = root / "core" / "debug_api.py"
    assert layer_path.is_file(), "debug API must have its own coherent layer"
    main_tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    layer_tree = ast.parse(layer_path.read_text(encoding="utf-8"))
    plugin = next(
        node for node in main_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ParserXPlugin"
    )
    mixin = next(
        node for node in layer_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DebugApiMixin"
    )
    assert [ast.unparse(base) for base in plugin.bases] == ["DebugApiMixin", "Star"]
    assert not mixin.bases
    plugin_methods = {
        node.name for node in plugin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    mixin_methods = {
        node.name for node in mixin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert mixin_methods == set(DEBUG_API_METHODS)
    assert plugin_methods.isdisjoint(DEBUG_API_METHODS)
    assert "__getattr__" not in ast.unparse(layer_tree)
    # Recognition, parser/cache execution, and delivery remain plugin-owned.
    assert {
        "_collect_parser_matches", "_parse_with_cache",
        "_send_parse_result", "_debug_mode_enabled",
    } <= plugin_methods


def test_plugin_inherits_debug_handlers_directly_without_wrappers():
    from astrbot_plugin_parser_x.core.debug_api import DebugApiMixin
    from astrbot_plugin_parser_x.main import ParserXPlugin

    assert issubclass(ParserXPlugin, DebugApiMixin)
    for name in DEBUG_API_METHODS:
        assert name not in ParserXPlugin.__dict__
        assert getattr(ParserXPlugin, name) is getattr(DebugApiMixin, name)


def test_debug_route_registration_keeps_paths_methods_and_bound_handlers():
    from types import SimpleNamespace

    from astrbot_plugin_parser_x.main import PLUGIN_NAME, ParserXPlugin

    plugin = object.__new__(ParserXPlugin)
    registered = []
    plugin.context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
    plugin._register_debug_page_apis()

    routes = (
        ("debug/status", "debug_page_status", ["GET"], "Debug Page status"),
        ("debug/start", "debug_page_start", ["POST"], "Start debug parse"),
        ("debug/mode", "debug_page_mode", ["POST"], "Set debug-only mode"),
        ("debug/events", "debug_page_events", ["GET"], "Debug parse events"),
        ("debug/cancel", "debug_page_cancel", ["POST"], "Cancel debug parse"),
        ("debug/media/<token>/preview", "debug_page_media_preview", ["GET"], "Preview debug media"),
        ("debug/media/<token>", "debug_page_media", ["GET"], "Download debug media"),
    )
    assert registered == [
        (f"/{PLUGIN_NAME}/{route}", getattr(plugin, handler), methods, description)
        for route, handler, methods, description in routes
    ]
