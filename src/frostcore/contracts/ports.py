"""Core extension ports (Protocols) and request/response models."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from frostcore.contracts.events import (
    DeliveryReceipt,
    InboundEvent,
    OutboundMessage,
)
from frostcore.contracts.policy import ChatPolicy, RecallItem
from frostcore.contracts.turn import TurnOutput

InboundHandler = Callable[[InboundEvent], Awaitable[None]]


class LLMRequest(BaseModel):
    """Normalized LLM completion request."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 1024
    extra: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """Normalized LLM completion response."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """Asynchronous LLM completion client."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """External messaging platform adapter."""

    name: str

    async def start(self, on_event: InboundHandler) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        ...


@runtime_checkable
class HistoryStore(Protocol):
    """Per-chat sequential message history storage."""

    def get_session(
        self,
        chat_id: str,
        system_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def append_message(
        self,
        chat_id: str,
        role: str,
        content: Any,
        **extra: Any,
    ) -> int:
        ...

    def replace_session(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ...

    def truncate_before_seq(
        self,
        chat_id: str,
        max_seq: int,
        keep_last_dialogue: int = 10,
    ) -> int:
        ...

    def list_chat_ids(self) -> list[str]:
        ...


@runtime_checkable
class RecallPlugin(Protocol):
    """Memory retrieval plugin."""

    async def search(
        self,
        query: str,
        *,
        chat_id: str,
        entity_ids: Sequence[str] = (),
        top_k: int = 5,
    ) -> list[RecallItem]:
        ...


class ComposeContext(BaseModel):
    """Input provided to PromptComposer before running turn evaluation F."""

    chat_id: str
    events: tuple[InboundEvent, ...] = ()
    memories: tuple[RecallItem, ...] = ()
    session: list[dict[str, Any]] = Field(default_factory=list)
    policy: ChatPolicy = Field(default_factory=ChatPolicy)
    energy: float = 100.0
    activity: float = 0.0
    clock_weight: float = 1.0
    extra: dict[str, Any] = Field(default_factory=dict)


class ComposedPrompt(BaseModel):
    """Final prompt structure produced by PromptComposer."""

    system_prompt: str
    persona_block: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class PromptComposer(Protocol):
    """Host-controlled prompt and context slot builder."""

    async def compose(self, ctx: ComposeContext) -> ComposedPrompt:
        ...


@runtime_checkable
class InboundMiddleware(Protocol):
    """Middleware intercepting inbound events before ModeRouter."""

    async def process_inbound(
        self,
        event: InboundEvent,
        next_call: Callable[[InboundEvent], Awaitable[None]],
    ) -> None:
        ...


@runtime_checkable
class OutboundMiddleware(Protocol):
    """Middleware intercepting outbound messages before ChannelAdapter.send()."""

    async def process_outbound(
        self,
        message: OutboundMessage,
        turn_output: TurnOutput,
        next_call: Callable[[OutboundMessage], Awaitable[DeliveryReceipt]],
    ) -> DeliveryReceipt:
        ...


@runtime_checkable
class DiarySummarizer(Protocol):
    """Generates first-person diary entries from filtered dialogue turns."""

    async def summarize(
        self,
        chat_id: str,
        dialogue: Sequence[dict[str, Any]],
    ) -> str:
        ...


__all__ = [
    "ChannelAdapter",
    "ComposeContext",
    "ComposedPrompt",
    "DiarySummarizer",
    "HistoryStore",
    "InboundHandler",
    "InboundMiddleware",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "OutboundMiddleware",
    "PromptComposer",
    "RecallPlugin",
]
