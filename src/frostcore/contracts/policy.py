"""Policy, recall item, and decision factor contracts."""
from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from frostcore.contracts.events import InboundEvent


class ChatPolicy(BaseModel):
    """Per-chat behavioral policy resolved by the host application."""

    model_config = ConfigDict(frozen=True)

    dormant: bool = False
    debounce_seconds: float = 0.0
    force_reply: bool = False
    allow_skip: bool = True
    speaker_weight: float = 1.0
    extra: dict[str, Any] = Field(default_factory=dict)


class RecallItem(BaseModel):
    """A single recalled memory item returned by RecallPipeline."""

    model_config = ConfigDict(frozen=True)

    diary_id: str
    content: str
    chat_id: str = ""
    created_at: float = 0.0
    score: float = 0.0
    source: str = "fts5"
    entity_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionContext(BaseModel):
    """Read-only context passed to DecisionFactor plugins."""

    model_config = ConfigDict(frozen=True)

    chat_id: str
    policy: ChatPolicy
    energy: float
    activity: float
    desire: float
    clock_weight: float
    recall_items: tuple[RecallItem, ...] = ()
    events: tuple[InboundEvent, ...] = ()
    now: float = 0.0


@runtime_checkable
class PolicyResolver(Protocol):
    """Resolves how the kernel should treat incoming events for a given chat."""

    def resolve(
        self,
        chat_id: str,
        events: Sequence[InboundEvent],
    ) -> ChatPolicy | Awaitable[ChatPolicy]:
        ...


@runtime_checkable
class DecisionFactor(Protocol):
    """Additive score factor contributed to reply willingness evaluation."""

    def score(self, ctx: DecisionContext) -> float:
        ...


__all__ = [
    "ChatPolicy",
    "DecisionContext",
    "DecisionFactor",
    "PolicyResolver",
    "RecallItem",
]
