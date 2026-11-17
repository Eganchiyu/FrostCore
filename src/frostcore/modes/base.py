"""Mode base classes and capability declarations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frostcore.contracts.events import InboundEvent


@dataclass(frozen=True)
class ModeCapabilities:
    """Capabilities and isolation constraints declared by a Mode."""

    can_send: bool = True
    writes_chat_history: bool = True
    accepts_human_input: bool = True
    allow_sub_focus: bool = False
    triggers_diary: bool = True
    accepts_inner_event: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "can_send": self.can_send,
            "writes_chat_history": self.writes_chat_history,
            "accepts_human_input": self.accepts_human_input,
            "allow_sub_focus": self.allow_sub_focus,
            "triggers_diary": self.triggers_diary,
            "accepts_inner_event": self.accepts_inner_event,
        }


CHAT_CAPABILITIES = ModeCapabilities(allow_sub_focus=True)


class Mode:
    """Base class for pluggable non-chat modes (e.g., autonomous thinking/work)."""

    name: str = "mode"
    title: str = ""
    capabilities: ModeCapabilities = ModeCapabilities(
        can_send=False,
        writes_chat_history=False,
        accepts_human_input=False,
        allow_sub_focus=False,
        triggers_diary=False,
        accepts_inner_event=True,
    )

    def __init__(
        self,
        *,
        chat_id: str,
        router: Any = None,
        run_id: str = "",
        goal: str = "",
    ) -> None:
        self.chat_id = str(chat_id)
        self.router = router
        self.run_id = run_id
        self.goal = goal
        self._stop_reason = ""
        self.result: dict[str, Any] = {}

    async def run(self) -> dict[str, Any]:
        raise NotImplementedError

    def on_input(self, event: InboundEvent) -> None:
        return None

    def on_inner_event(self, event: InboundEvent) -> bool:
        return False

    async def on_exit(self, result: dict[str, Any]) -> None:
        return None

    def request_stop(self, reason: str = "force_release") -> None:
        self._stop_reason = str(reason or "force_release")

    @property
    def stop_requested(self) -> bool:
        return bool(self._stop_reason)

    @property
    def stop_reason(self) -> str:
        return self._stop_reason


__all__ = ["CHAT_CAPABILITIES", "Mode", "ModeCapabilities"]
