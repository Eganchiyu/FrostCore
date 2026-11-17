"""Structured event sinks with built-in secret and base64 redaction."""
from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from frostcore.contracts.probe import EventSink, StructuredEvent

_SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
    "password",
    "passwd",
)


def redact_value(value: Any, *, key_hint: str = "") -> Any:
    """Recursively redact secrets and large inline data URLs."""
    key_lower = key_hint.lower()
    if any(marker in key_lower for marker in _SENSITIVE_KEYS):
        if isinstance(value, str) and value:
            return "***REDACTED***"
        if isinstance(value, (int, float)):
            return "***REDACTED***"

    if isinstance(value, str):
        if value.startswith("data:image/"):
            return "<image_omitted>"
        if len(value) > 4096:
            return value[:4096] + "...<truncated>"
        return value

    if isinstance(value, dict):
        return {
            str(k): redact_value(v, key_hint=str(k))
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [redact_value(item) for item in value]

    if isinstance(value, tuple):
        return [redact_value(item) for item in value]

    return value


def redact_event(event: StructuredEvent) -> StructuredEvent:
    """Return a copy of StructuredEvent with payload sanitized."""
    clean_payload = redact_value(event.payload)
    if not isinstance(clean_payload, dict):
        clean_payload = {"value": clean_payload}
    return StructuredEvent(
        ts=event.ts,
        app_id=event.app_id,
        kind=event.kind,
        chat_id=event.chat_id,
        payload=clean_payload,
    )


class NullEventSink:
    """No-op EventSink."""

    def emit(self, event: StructuredEvent) -> None:
        return None


class MemoryEventSink:
    """Thread-safe in-memory EventSink for tests and diagnostics."""

    def __init__(self, *, redact: bool = True, max_events: int = 5000) -> None:
        self._redact = redact
        self._max_events = max(1, max_events)
        self._lock = threading.Lock()
        self._events: list[StructuredEvent] = []

    def emit(self, event: StructuredEvent) -> None:
        stored = redact_event(event) if self._redact else event
        with self._lock:
            self._events.append(stored)
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]

    def events(
        self,
        *,
        kind: str | None = None,
        app_id: str | None = None,
    ) -> list[StructuredEvent]:
        with self._lock:
            items = list(self._events)
        if kind is not None:
            items = [e for e in items if e.kind == kind]
        if app_id is not None:
            items = [e for e in items if e.app_id == app_id]
        return items

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


class JsonlEventSink:
    """Append-only JSONL structured event sink with secret redaction."""

    def __init__(self, path: Path | str, *, redact: bool = True) -> None:
        self.path = Path(path)
        self._redact = redact
        self._lock = threading.Lock()

    def emit(self, event: StructuredEvent) -> None:
        stored = redact_event(event) if self._redact else event
        line = json.dumps(stored.model_dump(mode="json"), ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()


class CompositeEventSink:
    """Broadcasts structured events to multiple sinks without raising."""

    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def emit(self, event: StructuredEvent) -> None:
        sanitized = redact_event(event)
        for sink in self._sinks:
            try:
                sink.emit(sanitized)
            except Exception:
                continue


__all__ = [
    "CompositeEventSink",
    "JsonlEventSink",
    "MemoryEventSink",
    "NullEventSink",
    "redact_event",
    "redact_value",
]
