"""Instantiable multi-domain FocusManager (zero global singletons)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from frostcore.contracts.ports import HistoryStore
from frostcore.focus.domain import FocusDomain
from frostcore.focus.rewrite import (
    build_summary_message,
    message_focus_domain,
    rewrite_in_memory_messages,
    rewrite_persistent_session,
)

DEFAULT_MAX_IDLE_TURNS = 5


@dataclass
class FocusState:
    """Active focus state for one chat_id."""

    chat_id: str
    domain: str
    goal: str = ""
    started_at: float = field(default_factory=time.time)
    idle_turns: int = 0
    trace: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FocusExit:
    """Result record emitted when a chat exits a focus domain."""

    chat_id: str
    domain: str
    summary: str
    reason: str = "finish"


class FocusManager:
    """Per-app focus state machine: at most 1 active focus domain per chat_id."""

    def __init__(self, default_idle_turns: int = DEFAULT_MAX_IDLE_TURNS) -> None:
        self.default_idle_turns = max(1, int(default_idle_turns or DEFAULT_MAX_IDLE_TURNS))
        self._domains: dict[str, FocusDomain] = {}
        self._tool_to_domain: dict[str, FocusDomain] = {}
        self._states: dict[str, FocusState] = {}

    def register(self, domain: FocusDomain) -> FocusDomain:
        self._domains[domain.name] = domain
        for tool_name in domain.work_tools:
            self._tool_to_domain[tool_name] = domain
        return domain

    def domain(self, name: str) -> FocusDomain:
        if name not in self._domains:
            raise KeyError(f"Unregistered focus domain: {name}")
        return self._domains[name]

    def get_domain(self, name: str) -> FocusDomain | None:
        return self._domains.get(name)

    def domains(self) -> list[FocusDomain]:
        return list(self._domains.values())

    def domain_of_tool(self, tool_name: str) -> FocusDomain | None:
        if not tool_name:
            return None
        return self._tool_to_domain.get(tool_name)

    def active(self, chat_id: str) -> FocusState | None:
        return self._states.get(str(chat_id))

    def is_focused(self, chat_id: str, domain: str | None = None) -> bool:
        state = self._states.get(str(chat_id))
        if state is None:
            return False
        if domain is None:
            return True
        return state.domain == domain

    def enter(
        self,
        chat_id: str,
        domain: str,
        goal: str = "",
        *,
        history: HistoryStore | None = None,
    ) -> FocusState:
        cid = str(chat_id)
        d = self.domain(domain)
        state = self._states.get(cid)

        if state is not None and state.domain != d.name:
            self.exit(cid, history=history, reason="switch")
            state = None

        if state is None:
            state = FocusState(chat_id=cid, domain=d.name, goal=goal.strip())
            self._states[cid] = state
            if d.on_enter is not None:
                try:
                    d.on_enter(cid, state.goal)
                except Exception:
                    pass
        else:
            if goal and not state.goal:
                state.goal = goal.strip()
            state.idle_turns = 0
        return state

    def abort(self, chat_id: str, domain: str | None = None) -> None:
        """Roll back focus state on entry failure without writing a summary."""
        cid = str(chat_id)
        state = self._states.get(cid)
        if state is None:
            return
        if domain is None or state.domain == domain:
            self._states.pop(cid, None)

    def record_step(
        self,
        chat_id: str,
        step_desc: str,
        note: str = "",
        *,
        domain: str | None = None,
    ) -> None:
        cid = str(chat_id)
        state = self._states.get(cid)
        if state is None:
            target = domain or next(iter(self._domains), "")
            if not target:
                return
            state = self.enter(cid, target)
        state.idle_turns = 0
        d = self._domains.get(state.domain)
        max_trace = d.max_trace if d else 20

        desc = step_desc.strip()
        note_str = note.strip()
        entry = f"{desc} ({note_str})" if (desc and note_str) else (desc or note_str)
        if entry:
            state.trace.append(entry[:80])
            if len(state.trace) > max_trace:
                del state.trace[:-max_trace]

    def build_fallback_summary(
        self,
        state: FocusState | None,
        custom_summary: str = "",
        *,
        domain: str | None = None,
    ) -> str:
        text = custom_summary.strip()
        if text:
            return text
        d_name = (state.domain if state else None) or domain or ""
        d = self._domains.get(d_name)
        title = d.title if d else d_name
        default_fb = (
            d.default_fallback if d and d.default_fallback else f"Completed {title} work."
        )
        empty_fb = (
            d.empty_trace_fallback
            if d and d.empty_trace_fallback
            else f"Entered and finished {title}."
        )
        if state is None:
            return default_fb

        parts: list[str] = []
        if state.goal:
            parts.append(f"Goal: {state.goal}")
        if state.trace:
            parts.append("Steps: " + " -> ".join(state.trace[-8:]))
        return "; ".join(parts) if parts else empty_fb

    def exit(
        self,
        chat_id: str,
        *,
        history: HistoryStore | None = None,
        summary: str = "",
        reason: str = "finish",
        domain: str | None = None,
        tool_messages: list[dict[str, Any]] | None = None,
    ) -> FocusExit | None:
        cid = str(chat_id)
        state = self._states.pop(cid, None)
        d_name = (state.domain if state else None) or domain
        if not d_name:
            return None
        d = self._domains.get(d_name)
        if d is None:
            return None

        final_summary = self.build_fallback_summary(state, summary, domain=d.name)

        if history is not None:
            session = history.get_session(cid)
            filtered = rewrite_persistent_session(session, d, final_summary)
            history.replace_session(cid, filtered)

        if tool_messages is not None:
            rewrite_in_memory_messages(tool_messages, d, final_summary)

        if d.on_exit is not None:
            try:
                d.on_exit(cid, final_summary, reason)
            except Exception:
                pass

        return FocusExit(chat_id=cid, domain=d.name, summary=final_summary, reason=reason)

    def max_rounds_for(self, domain_name: str, default_rounds: int = 4) -> int:
        d = self._domains.get(domain_name)
        if d is None:
            return default_rounds
        return max(default_rounds, int(d.max_rounds))

    def after_turn(
        self,
        chat_id: str,
        used_domains: set[str] | bool,
        *,
        history: HistoryStore | None = None,
    ) -> FocusExit | None:
        cid = str(chat_id)
        state = self._states.get(cid)
        if state is None:
            return None

        domain_used = (
            used_domains if isinstance(used_domains, bool) else (state.domain in used_domains)
        )
        if domain_used:
            state.idle_turns = 0
            return None

        state.idle_turns += 1
        d = self._domains.get(state.domain)
        limit = max(1, int(d.idle_turns)) if d else self.default_idle_turns
        if state.idle_turns > limit:
            auto_note = self.build_fallback_summary(state)
            return self.exit(cid, history=history, summary=auto_note, reason="idle")
        return None

    def _sweep_session(
        self,
        session: list[dict[str, Any]],
        active_domain: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        new_session: list[dict[str, Any]] = []
        segment_domain: str | None = None
        segment_thoughts: list[str] = []
        segment_tools: list[str] = []
        segment_interleaved: list[dict[str, Any]] = []
        swept_in_chat = 0

        def _flush_segment() -> None:
            nonlocal segment_domain, segment_thoughts, segment_tools, segment_interleaved
            if not segment_domain:
                return
            d = self._domains.get(segment_domain)
            new_session.extend(segment_interleaved)
            parts: list[str] = []
            if segment_thoughts:
                parts.append("; ".join(segment_thoughts[:3]))
            if segment_tools:
                parts.append("Steps: " + " -> ".join(segment_tools[:6]))
            fallback = (
                "; ".join(parts)
                if parts
                else (d.default_fallback if d and d.default_fallback else "Interrupted")
            )
            summary_text = f"(Interrupted before exit) {fallback}"
            if d is not None:
                new_session.append(build_summary_message(d, summary_text))
            else:
                new_session.append(
                    {
                        "role": "user",
                        "content": f"[{segment_domain} Summary] {summary_text}",
                        "is_focus_summary": True,
                        "focus_domain": segment_domain,
                    }
                )
            segment_domain = None
            segment_thoughts = []
            segment_tools = []
            segment_interleaved = []

        for msg in session:
            msg_domain = message_focus_domain(msg, self.domain_of_tool)
            if msg_domain and msg_domain != active_domain:
                if segment_domain and segment_domain != msg_domain:
                    _flush_segment()
                segment_domain = msg_domain
                swept_in_chat += 1
                if msg.get("role") == "assistant" and msg.get("content"):
                    segment_thoughts.append(str(msg["content"]).strip()[:60])
                elif msg.get("role") == "tool" and msg.get("name"):
                    segment_tools.append(str(msg["name"]))
            else:
                if segment_domain is not None:
                    if msg.get("role") == "user" and not msg.get("is_focus_summary"):
                        segment_interleaved.append(msg)
                    else:
                        _flush_segment()
                        new_session.append(msg)
                else:
                    new_session.append(msg)

        if segment_domain is not None:
            _flush_segment()

        return new_session, swept_in_chat

    def sweep_orphans(self, history: HistoryStore | None) -> int:
        """Collapse orphaned focus segments left over from unexpected restarts."""
        if history is None:
            return 0
        total_swept = 0
        for cid in history.list_chat_ids():
            session = history.get_session(cid)
            if not session:
                continue
            active_state = self.active(cid)
            active_domain = active_state.domain if active_state else None
            new_session, swept_in_chat = self._sweep_session(session, active_domain)
            if swept_in_chat > 0:
                total_swept += swept_in_chat
                history.replace_session(cid, new_session)

        return total_swept


__all__ = [
    "DEFAULT_MAX_IDLE_TURNS",
    "FocusExit",
    "FocusManager",
    "FocusState",
]
