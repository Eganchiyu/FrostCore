"""Deterministic turn evaluation boundary contracts: F(TurnInput) -> TurnOutput."""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from frostcore.contracts.events import InboundEvent
from frostcore.contracts.policy import RecallItem

TurnOutcome = Literal["reply", "skip", "filtered", "exhausted", "error"]


class PersistDecision(BaseModel):
    """Persistence gating decision for an intermediate turn message."""

    action: Literal["write", "drop"] = "write"
    focus_work: str | None = None


class FocusDelta(BaseModel):
    """Summary of focus domain transitions that occurred during a single turn."""

    model_config = ConfigDict(frozen=True)

    entered: str | None = None
    exited: tuple[str, ...] = ()
    active_after: str | None = None


class FocusExitRecord(BaseModel):
    """Structured record of a focus exit produced by pure turn evaluation F."""

    model_config = ConfigDict(frozen=True)

    chat_id: str
    domain: str
    summary: str
    reason: str = "finish"


class TurnInput(BaseModel):
    """Explicit input boundary for deterministic turn evaluation F."""

    chat_id: str
    events: tuple[InboundEvent, ...] = ()
    memories: tuple[RecallItem, ...] = ()
    messages: list[dict[str, Any]] = Field(default_factory=list)
    system_prompt: str = ""
    now: float = Field(default_factory=time.time)
    rng_seed: int = 0
    max_rounds: int = 4
    extra: dict[str, Any] = Field(default_factory=dict)


class TurnOutput(BaseModel):
    """Explicit output produced by TurnEvaluator F(TurnInput)."""

    reply: str
    raw_reply: str
    turn_messages: list[dict[str, Any]] = Field(default_factory=list)
    persist_messages: list[dict[str, Any]] = Field(default_factory=list)
    focus_exits: tuple[FocusExitRecord, ...] = ()
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    focus_delta: FocusDelta = Field(default_factory=FocusDelta)
    used_tools: tuple[str, ...] = ()
    outcome: TurnOutcome = "reply"
    llm_rounds: int = 0


__all__ = [
    "FocusDelta",
    "FocusExitRecord",
    "PersistDecision",
    "TurnInput",
    "TurnOutcome",
    "TurnOutput",
]
