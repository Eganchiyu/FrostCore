"""Stable event and entity contracts for FrostCore."""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    return uuid.uuid4().hex


class EntityRef(BaseModel):
    """Platform-agnostic logical identity reference for a conversational participant."""

    model_config = ConfigDict(frozen=True)

    entity_id: str
    display_name: str = ""
    platform: str = ""
    platform_user_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaAttachment(BaseModel):
    """Normalized media attachment attached to an event or outbound message."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str = Field(default_factory=_new_id)
    kind: Literal["image", "audio", "file"] = "image"
    url: str = ""
    local_path: str = ""
    mime_type: str = ""
    description: str = ""


class IncomingEvent(BaseModel):
    """Normalized inbound message/event delivered by a ChannelAdapter."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=_new_id)
    chat_id: str
    sender: EntityRef
    content: str
    raw_text: str = ""
    timestamp: float = Field(default_factory=time.time)
    is_bot: bool = False
    source: str = "adapter"
    tags: frozenset[str] = Field(default_factory=frozenset)
    attachments: tuple[MediaAttachment, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_system_injected(self) -> bool:
        return False


class SystemInjectedEvent(BaseModel):
    """Internal system event (catchup, subagent_report, timer).

    Per FrostCore conventions, system-injected events do not increment diary
    dialogue turn counts and do not prematurely clear human reply watches.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=_new_id)
    chat_id: str
    system_kind: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_system_injected(self) -> bool:
        return True


InboundEvent = IncomingEvent | SystemInjectedEvent


class OutboundMessage(BaseModel):
    """Normalized outbound message emitted by SessionPipeline."""

    model_config = ConfigDict(frozen=True)

    chat_id: str
    text: str
    reply_to_event_id: str | None = None
    attachments: tuple[MediaAttachment, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryReceipt(BaseModel):
    """Receipt returned by ChannelAdapter.send()."""

    model_config = ConfigDict(frozen=True)

    message_id: str | None = None
    ok: bool = True
    error: str = ""


__all__ = [
    "DeliveryReceipt",
    "EntityRef",
    "InboundEvent",
    "IncomingEvent",
    "MediaAttachment",
    "OutboundMessage",
    "SystemInjectedEvent",
]
