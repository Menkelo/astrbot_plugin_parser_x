"""Preserve source topic tags across platforms.

The historical name remains for parser compatibility; topics are now retained
and highlighted by the unified renderer rather than stripped from source text.
"""
from __future__ import annotations


def strip_topic_tags(text: str) -> str:
    """Compatibility shim: topic removal was explicitly withdrawn by the user."""
    return str(text or '')


__all__ = ['strip_topic_tags']
