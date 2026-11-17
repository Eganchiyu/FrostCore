"""Pure dialogue filtering helpers to keep memories de-peripheralized."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DIALOGUE_ROLES = frozenset({"user", "assistant"})


def is_dialogue_message(message: Mapping[str, Any]) -> bool:
    """Return True if message represents genuine human/assistant dialogue."""
    role = message.get("role")
    if role not in DIALOGUE_ROLES:
        return False
    if message.get("focus_work"):
        return False
    if message.get("is_tool_thought"):
        return False
    if message.get("tool_calls"):
        return False
    if message.get("is_system_injected"):
        return False
    return True


def dialogue_messages(history: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Extract dialogue-only messages while preserving original order."""
    return [dict(m) for m in (history or ()) if is_dialogue_message(m)]


def dialogue_length(history: Sequence[Mapping[str, Any]] | None) -> int:
    """Count genuine dialogue turns (excluding tools, thoughts, and system injections)."""
    return sum(1 for m in (history or ()) if is_dialogue_message(m))


__all__ = ["DIALOGUE_ROLES", "dialogue_length", "dialogue_messages", "is_dialogue_message"]
