"""Retired implementations must not return to the runtime surface."""
import ast
from pathlib import Path


def test_no_retired_delivery_or_temporary_diagnostic_entrypoints():
    root = Path(__file__).resolve().parents[1]
    trees = [ast.parse((root / name).read_text(encoding='utf-8'))
             for name in ('main.py', 'core/delivery.py')]
    names = {node.name for tree in trees for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    retired = {'_send_delivery_plan_legacy', '_send_parse_result_legacy', '_card_embeds_single_image', 'send_direct_segments', '_diag_log_outgoing', '_log_comment_event'}
    assert not names & retired
    assert {'on_message', '_send_parse_result', '_send_delivery_plan', '_send_video_segment', '_resend_with_fresh_media', '_prepare_media_file'} <= names
