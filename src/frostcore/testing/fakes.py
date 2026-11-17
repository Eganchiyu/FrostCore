"""Offline fakes for testing FrostCore kernels and extensions without API keys."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from frostcore.contracts.events import (
    DeliveryReceipt,
    InboundEvent,
    OutboundMessage,
)
from frostcore.contracts.policy import ChatPolicy
from frostcore.contracts.ports import InboundHandler, LLMRequest, LLMResponse


class FakeLLMClient:
    """Deterministic scripted LLMClient recording every LLMRequest."""

    def __init__(
        self,
        responses: Sequence[LLMResponse | Callable[[LLMRequest], LLMResponse]] = (),
        *,
        default_reply: str = "ok",
    ) -> None:
        self._script: list[LLMResponse | Callable[[LLMRequest], LLMResponse]] = list(responses)
        self._cursor = 0
        self.default_reply = default_reply
        self.requests: list[LLMRequest] = []

    def reset(self) -> None:
        self._cursor = 0
        self.requests.clear()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))
        if self._cursor < len(self._script):
            item = self._script[self._cursor]
            self._cursor += 1
            return item(request) if callable(item) else item
        return LLMResponse(content=self.default_reply, finish_reason="stop")


class MemoryChannelAdapter:
    """In-memory ChannelAdapter for deterministic end-to-end tests."""

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self.started = False
        self.stopped = False
        self.sent_messages: list[OutboundMessage] = []
        self._on_event: InboundHandler | None = None
        self._seq = 0

    async def start(self, on_event: InboundHandler) -> None:
        self._on_event = on_event
        self.started = True
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.started = False

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self._seq += 1
        self.sent_messages.append(message)
        return DeliveryReceipt(message_id=f"{self.name}-{self._seq}", ok=True)

    async def inject(self, event: InboundEvent) -> None:
        if self._on_event is None:
            raise RuntimeError("MemoryChannelAdapter has not been started")
        await self._on_event(event)


class FakeClock:
    """Controllable wall-clock timestamp and TimeCurve provider."""

    def __init__(self, now: float = 1_700_000_000.0, weight: float = 1.0) -> None:
        self.now = now
        self.weight = weight

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now

    def curve(self, _ts: float) -> float:
        return self.weight


class FakeDiarySummarizer:
    """Deterministic DiarySummarizer with optional async barrier for concurrency tests."""

    def __init__(
        self,
        prefix: str = "Diary summary:",
        *,
        started_event: asyncio.Event | None = None,
        proceed_event: asyncio.Event | None = None,
    ) -> None:
        self.prefix = prefix
        self.started_event = started_event
        self.proceed_event = proceed_event
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def summarize(self, chat_id: str, dialogue: Sequence[dict[str, Any]]) -> str:
        snap = [dict(m) for m in dialogue]
        self.calls.append((chat_id, snap))
        if self.started_event is not None:
            self.started_event.set()
        if self.proceed_event is not None:
            await self.proceed_event.wait()
        snippets = [str(m.get("content") or "")[:30] for m in snap[-3:]]
        return f"{self.prefix} {' | '.join(snippets)}".strip()


class StaticPolicyResolver:
    """Simple PolicyResolver mapping chat_ids to fixed ChatPolicy objects."""

    def __init__(
        self,
        default_policy: ChatPolicy | None = None,
        overrides: dict[str, ChatPolicy] | None = None,
    ) -> None:
        self.default_policy = default_policy or ChatPolicy()
        self.overrides: dict[str, ChatPolicy] = dict(overrides or {})

    def resolve(self, chat_id: str, events: Sequence[InboundEvent]) -> ChatPolicy:
        return self.overrides.get(str(chat_id), self.default_policy)


__all__ = [
    "FakeClock",
    "FakeDiarySummarizer",
    "FakeLLMClient",
    "MemoryChannelAdapter",
    "StaticPolicyResolver",
]
