"""Instantiable ModeRouter enforcing single non-chat mode exclusivity and catchups."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from frostcore.contracts.events import InboundEvent, SystemInjectedEvent
from frostcore.contracts.probe import EventSink, StructuredEvent
from frostcore.modes.base import CHAT_CAPABILITIES, Mode, ModeCapabilities

CHAT_MODE = "chat"

ROUTE_CHAT = "chat"
ROUTE_MODE = "mode"
ROUTE_FROZEN = "frozen"


class ModeBusyError(RuntimeError):
    """Raised when attempting to enter a non-chat mode while another is globally active."""


@dataclass
class ModeState:
    chat_id: str
    mode: str
    run_id: str
    started_at: float
    instance: Mode | None = None
    task: Any = None

    @property
    def is_chat(self) -> bool:
        return self.mode == CHAT_MODE

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "mode": self.mode,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "elapsed": max(0.0, time.time() - self.started_at),
        }


@dataclass
class FrozenChat:
    """Tracks missed genuine human events for a frozen chat during a non-chat mode."""

    chat_id: str
    since: float = field(default_factory=time.time)
    messages: int = 0
    last_at: float = 0.0
    missed_events: list[InboundEvent] = field(default_factory=list)


def _counts_as_missed(event: InboundEvent) -> bool:
    if getattr(event, "is_system_injected", False):
        return False
    if getattr(event, "is_bot", False):
        return False
    return True


class ModeRouter:
    """Per-app mode router guarding single-identity non-chat exclusivity."""

    def __init__(
        self,
        *,
        event_sink: EventSink | None = None,
        app_id: str = "frostcore",
    ) -> None:
        self._modes: dict[str, Callable[[], Mode]] = {}
        self._states: dict[str, ModeState] = {}
        self._global_run: ModeState | None = None
        self._frozen: dict[str, FrozenChat] = {}
        self._catchup_handler: Callable[[FrozenChat], Awaitable[None] | None] | None = None
        self._event_sink = event_sink
        self._app_id = app_id

    def _emit(self, kind: str, chat_id: str, payload: dict[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink.emit(
                StructuredEvent(app_id=self._app_id, kind=kind, chat_id=chat_id, payload=payload)
            )

    def register_mode(self, name: str, factory: Callable[[], Mode]) -> None:
        self._modes[str(name)] = factory

    def has_mode(self, name: str) -> bool:
        return str(name) in self._modes

    def create_mode(self, name: str) -> Mode | None:
        factory = self._modes.get(str(name))
        return factory() if factory is not None else None

    def set_catchup_handler(
        self,
        handler: Callable[[FrozenChat], Awaitable[None] | None] | None,
    ) -> None:
        self._catchup_handler = handler

    @property
    def global_run(self) -> ModeState | None:
        return self._global_run

    def is_idle(self) -> bool:
        return self._global_run is None

    def current_mode(self, chat_id: str) -> str:
        state = self._states.get(str(chat_id))
        return state.mode if state else CHAT_MODE

    def active_state(self, chat_id: str) -> ModeState | None:
        state = self._states.get(str(chat_id))
        if state is None or state.is_chat:
            return None
        return state

    def capabilities(self, chat_id: str) -> ModeCapabilities:
        state = self.active_state(chat_id)
        if state is None or state.instance is None:
            return CHAT_CAPABILITIES
        return state.instance.capabilities

    def route(self, chat_id: str) -> tuple[str, ModeState | None]:
        cid = str(chat_id)
        state = self._states.get(cid)
        if state is not None and not state.is_chat:
            return ROUTE_MODE, state
        if self._global_run is not None:
            return ROUTE_FROZEN, self._global_run
        return ROUTE_CHAT, None

    def enter(
        self,
        chat_id: str,
        mode: str,
        *,
        run_id: str | None = None,
        instance: Mode | None = None,
    ) -> ModeState:
        cid = str(chat_id)
        mode_name = str(mode)
        if mode_name == CHAT_MODE:
            raise ValueError(
                "chat mode is the default state and cannot be entered via router.enter"
            )

        current = self._states.get(cid)
        if current is not None and not current.is_chat:
            if current.mode == mode_name:
                return current
            raise ModeBusyError(f"chat {cid} is already running mode {current.mode}")
        if self._global_run is not None and self._global_run.chat_id != cid:
            raise ModeBusyError(
                f"global non-chat mode {self._global_run.mode} active on chat "
                f"{self._global_run.chat_id}"
            )

        state = ModeState(
            chat_id=cid,
            mode=mode_name,
            run_id=str(run_id or f"{mode_name}-{int(time.time() * 1000)}"),
            started_at=time.time(),
            instance=instance,
        )
        self._states[cid] = state
        self._global_run = state
        self._frozen.setdefault(cid, FrozenChat(chat_id=cid))
        self._emit("mode.entered", cid, {"mode": mode_name, "run_id": state.run_id})
        return state

    def exit(self, chat_id: str | None = None, *, run_id: str | None = None) -> ModeState | None:
        cid = (
            str(chat_id)
            if chat_id is not None
            else (self._global_run.chat_id if self._global_run else None)
        )
        if cid is None:
            return None
        state = self._states.get(cid)
        if state is None or state.is_chat:
            return None
        if run_id and state.run_id != str(run_id):
            return None

        self._states.pop(cid, None)
        if self._global_run is state:
            self._global_run = None
        self._emit("mode.exited", cid, {"mode": state.mode, "run_id": state.run_id})
        return state

    def force_release(self, reason: str = "force_release") -> ModeState | None:
        state = self._global_run
        if state is None:
            return None
        if state.instance is not None:
            try:
                state.instance.request_stop(reason)
            except Exception:
                pass
        self._emit(
            "mode.force_release",
            state.chat_id,
            {"mode": state.mode, "run_id": state.run_id, "reason": reason},
        )
        return state

    def mark_frozen(self, chat_id: str, event: InboundEvent | None = None) -> FrozenChat:
        cid = str(chat_id)
        frozen = self._frozen.get(cid)
        if frozen is None:
            frozen = FrozenChat(chat_id=cid)
            self._frozen[cid] = frozen
        if event is not None and _counts_as_missed(event):
            frozen.messages += 1
            frozen.last_at = time.time()
            frozen.missed_events.append(event)
        return frozen

    def pending_catchups(self) -> dict[str, FrozenChat]:
        return {cid: f for cid, f in self._frozen.items() if f.messages > 0}

    def clear_all_catchups(self) -> None:
        self._frozen.clear()

    async def dispatch_catchups(self) -> list[str]:
        pending = self.pending_catchups()
        self.clear_all_catchups()
        if not pending or self._catchup_handler is None:
            return []
        done: list[str] = []
        for cid, frozen in pending.items():
            try:
                res = self._catchup_handler(frozen)
                if res is not None:
                    await res
                done.append(cid)
            except Exception:
                continue
        return done

    def build_catchup_event(self, frozen: FrozenChat) -> SystemInjectedEvent:
        """Convert a FrozenChat record into a standard catchup SystemInjectedEvent."""
        preview = "; ".join(
            f"{getattr(ev.sender, 'display_name', 'user')}: {ev.content}"
            for ev in frozen.missed_events[-5:]
            if hasattr(ev, "sender")
        )
        content = (
            f"While away, {frozen.messages} message(s) arrived in chat {frozen.chat_id}."
            + (f" Recent: {preview}" if preview else "")
        )
        return SystemInjectedEvent(
            chat_id=frozen.chat_id,
            system_kind="catchup",
            content=content,
            metadata={"missed_count": frozen.messages, "since": frozen.since},
        )

    def deliver_inner_event(self, chat_id: str, event: InboundEvent) -> bool:
        state = self.active_state(chat_id)
        if state is None or state.instance is None:
            return False
        if not state.instance.capabilities.accepts_inner_event:
            return False
        try:
            return bool(state.instance.on_inner_event(event))
        except Exception:
            return False

    def deliver_input(self, chat_id: str, event: InboundEvent) -> None:
        state = self.active_state(chat_id)
        if state is None or state.instance is None:
            return
        try:
            state.instance.on_input(event)
        except Exception:
            pass


__all__ = [
    "CHAT_MODE",
    "FrozenChat",
    "ModeBusyError",
    "ModeRouter",
    "ModeState",
    "ROUTE_CHAT",
    "ROUTE_FROZEN",
    "ROUTE_MODE",
]
