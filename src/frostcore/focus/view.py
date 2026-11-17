"""ToolView: immutable visible tool set derived purely from FocusManager + registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frostcore.focus.manager import FocusManager


@dataclass(frozen=True)
class ToolView:
    """Immutable snapshot of tools visible to the LLM for the current step."""

    visible: frozenset[str]

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self.visible

    @classmethod
    def resolve(
        cls,
        registry: Any,
        manager: FocusManager | None,
        chat_id: str,
        exited_this_turn: set[str] | None = None,
    ) -> ToolView:
        names = set(registry.list_functions()) if hasattr(registry, "list_functions") else set()
        if manager is None or not manager.domains():
            return cls(frozenset(names))

        exited = exited_this_turn or set()
        state = manager.active(chat_id)
        if state is not None:
            domain = manager.get_domain(state.domain)
            if domain is not None and domain.entry_tool in names:
                return cls(frozenset(names & domain.focus_allowed))

        for domain in manager.domains():
            if domain.entry_tool in names:
                names -= domain.hidden_in_default
                if domain.name in exited:
                    names.discard(domain.entry_tool)
        return cls(frozenset(names))


__all__ = ["ToolView"]
