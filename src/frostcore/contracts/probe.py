"""Observability, health probe, and read-only guard contracts."""
from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class StructuredEvent(BaseModel):
    """Structured telemetry event emitted by kernel components."""

    model_config = ConfigDict(frozen=True)

    ts: float = Field(default_factory=time.time)
    app_id: str
    kind: str
    chat_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EventSink(Protocol):
    """Receives structured kernel telemetry events."""

    def emit(self, event: StructuredEvent) -> None:
        ...


class AppHealthSnapshot(BaseModel):
    """Health and readiness snapshot reported by a single FrostApp."""

    model_config = ConfigDict(frozen=True)

    app_id: str
    ready: bool
    seed_path: str = ""
    epoch: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class HealthReporter(Protocol):
    """Provides synchronous health snapshot for out-of-loop probe serving."""

    def health_snapshot(self) -> AppHealthSnapshot:
        ...


__all__ = [
    "AppHealthSnapshot",
    "EventSink",
    "HealthReporter",
    "StructuredEvent",
]
