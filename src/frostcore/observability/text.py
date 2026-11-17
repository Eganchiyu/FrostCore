"""Grapheme-aware text truncation helpers preventing split emoji/ZWJ sequences."""
from __future__ import annotations

import unicodedata

_ZWJ = "\u200d"
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})


def _is_emoji_modifier(ch: str) -> bool:
    code = ord(ch)
    return 0x1F3FB <= code <= 0x1F3FF


def _is_cluster_continuation(ch: str) -> bool:
    return (
        unicodedata.combining(ch) != 0
        or ch == _ZWJ
        or ch in _VARIATION_SELECTORS
        or _is_emoji_modifier(ch)
    )


def safe_truncate(text: str, max_chars: int, suffix: str = "") -> str:
    """Truncate text without splitting combining marks, variation selectors, or ZWJ clusters."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    budget = max(0, max_chars - len(suffix))
    if budget == 0:
        return suffix[:max_chars]

    end = budget
    # Walk back if we would cut inside a cluster or right after a ZWJ
    while end > 0 and (
        _is_cluster_continuation(text[end])
        or text[end - 1] == _ZWJ
    ):
        if text[end - 1] == _ZWJ and end >= 2:
            end -= 2
        else:
            end -= 1

    # Step back past any remaining base char attached to a stripped ZWJ chain
    while end > 0 and text[end] == _ZWJ:
        end -= 1

    return text[:end] + suffix


__all__ = ["safe_truncate"]
