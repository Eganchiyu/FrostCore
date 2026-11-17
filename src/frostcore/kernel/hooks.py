"""TurnState and TurnHook lifecycle chain (FocusHook, PruneHook, FocusTraceHook, EventTraceHook)."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from frostcore.contracts.ports import HistoryStore
from frostcore.contracts.probe import EventSink, StructuredEvent
from frostcore.contracts.turn import PersistDecision, TurnOutcome
from frostcore.focus.manager import FocusExit, FocusManager
from frostcore.focus.rewrite import is_domain_work_message, rewrite_in_memory_messages
from frostcore.kernel.pruner import ToolHistoryPruner
from frostcore.kernel.toolchain import FunctionRegistry, ToolCallManager, ToolContext


@dataclass
class TurnState:
    """Mutable per-turn execution state enclosed inside TurnEvaluator."""

    chat_id: str
    context: ToolContext
    tool_messages: list[dict[str, Any]]
    active_registry: FunctionRegistry
    active_tool_manager: ToolCallManager
    now: float
    rng: random.Random
    round_idx: int = 0
    max_rounds: int = 4
    base_max_rounds: int = 4
    tools_executed: bool = False
    entered_domains: list[str] = field(default_factory=list)
    used_domains: set[str] = field(default_factory=set)
    exited_this_turn: set[str] = field(default_factory=set)
    exited_order: list[str] = field(default_factory=list)
    pending_exits: list[FocusExit] = field(default_factory=list)
    completed_exits: list[FocusExit] = field(default_factory=list)
    batch_exited_domains: set[str] = field(default_factory=set)
    persist_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    sent_thoughts: set[str] = field(default_factory=set)
    pending_thought: str = ""
    batch_focus_domain: str | None = None
    outcome: TurnOutcome = "reply"
    final: tuple[str, str] | None = None


class TurnHook(Protocol):
    """Lifecycle hook protocol invoked by TurnEvaluator."""

    def on_turn_start(self, st: TurnState) -> None: ...
    def before_llm(self, st: TurnState) -> None: ...
    def on_model_call(
        self,
        st: TurnState,
        msg: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> None: ...
    def classify_persist(self, st: TurnState, msg: dict[str, Any]) -> PersistDecision: ...
    def after_batch(self, st: TurnState, results: list[dict[str, Any]]) -> None: ...
    def on_turn_end(self, st: TurnState) -> None: ...


class BaseTurnHook:
    """Default no-op TurnHook implementation."""

    def on_turn_start(self, st: TurnState) -> None:
        return None

    def before_llm(self, st: TurnState) -> None:
        return None

    def on_model_call(
        self,
        st: TurnState,
        msg: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> None:
        return None

    def classify_persist(self, st: TurnState, msg: dict[str, Any]) -> PersistDecision:
        return PersistDecision()

    def after_batch(self, st: TurnState, results: list[dict[str, Any]]) -> None:
        return None

    def on_turn_end(self, st: TurnState) -> None:
        return None


class TurnHookChain:
    """Ordered composition of TurnHooks."""

    def __init__(self, hooks: list[TurnHook] | None = None) -> None:
        self.hooks: list[TurnHook] = list(hooks or [])

    def on_turn_start(self, st: TurnState) -> None:
        for hook in self.hooks:
            hook.on_turn_start(st)

    def before_llm(self, st: TurnState) -> None:
        for hook in self.hooks:
            hook.before_llm(st)

    def on_model_call(
        self,
        st: TurnState,
        msg: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> None:
        for hook in self.hooks:
            hook.on_model_call(st, msg, calls)

    def classify_persist(self, st: TurnState, msg: dict[str, Any]) -> PersistDecision:
        decision = PersistDecision()
        for hook in self.hooks:
            candidate = hook.classify_persist(st, msg)
            if candidate.action == "drop":
                return candidate
            if candidate.focus_work and not decision.focus_work:
                decision.focus_work = candidate.focus_work
        return decision

    def after_batch(self, st: TurnState, results: list[dict[str, Any]]) -> None:
        for hook in self.hooks:
            hook.after_batch(st, results)

    def on_turn_end(self, st: TurnState) -> None:
        for hook in self.hooks:
            hook.on_turn_end(st)


class FocusHook(BaseTurnHook):
    """Enforces focus round limits, exit write gating, in-memory rewrite, and idle exit."""

    def __init__(
        self,
        manager: FocusManager,
        history: HistoryStore | None = None,
        *,
        restore_rounds_on_exit: bool = True,
        gate_exited_writes: bool = True,
    ) -> None:
        self.manager = manager
        self.history = history
        self.restore_rounds_on_exit = restore_rounds_on_exit
        self.gate_exited_writes = gate_exited_writes

    def on_turn_start(self, st: TurnState) -> None:
        active = self.manager.active(st.chat_id)
        if active is not None:
            st.max_rounds = self.manager.max_rounds_for(active.domain, st.max_rounds)

    def before_llm(self, st: TurnState) -> None:
        active = self.manager.active(st.chat_id)
        if active is not None and active.domain not in st.exited_this_turn:
            st.max_rounds = self.manager.max_rounds_for(active.domain, st.max_rounds)

    def on_model_call(
        self,
        st: TurnState,
        msg: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> None:
        active = self.manager.active(st.chat_id)
        tool_names = [(c.get("function") or {}).get("name") or "" for c in calls]
        batch_domains: set[str] = set()
        for tn in tool_names:
            d = self.manager.domain_of_tool(str(tn))
            if d is not None:
                batch_domains.add(d.name)
                if tn == d.entry_tool and d.name not in st.entered_domains:
                    st.entered_domains.append(d.name)
        if active is not None:
            batch_domains.add(active.domain)
        for dn in batch_domains:
            st.used_domains.add(dn)
            if dn not in st.exited_this_turn:
                st.max_rounds = self.manager.max_rounds_for(dn, st.max_rounds)
        st.batch_focus_domain = (
            active.domain
            if active is not None
            else (next(iter(batch_domains)) if batch_domains else None)
        )

    def classify_persist(self, st: TurnState, msg: dict[str, Any]) -> PersistDecision:
        role = msg.get("role")
        domain_name: str | None = None
        if role == "assistant":
            domain_name = st.batch_focus_domain
        elif role == "tool":
            tool_name = str(msg.get("name") or "")
            tool_domain = self.manager.domain_of_tool(tool_name)
            active = self.manager.active(st.chat_id)
            domain_name = (
                tool_domain.name
                if tool_domain is not None
                else (active.domain if active is not None else st.batch_focus_domain)
            )
        if self.gate_exited_writes and domain_name and domain_name in st.batch_exited_domains:
            return PersistDecision(action="drop", focus_work=domain_name)
        return PersistDecision(action="write", focus_work=domain_name)

    def collect_batch_exits(self, st: TurnState) -> list[FocusExit]:
        exits: list[FocusExit] = list(st.context.metadata.pop("focus_exits", None) or [])
        if exits:
            st.pending_exits.extend(exits)
            for item in exits:
                st.batch_exited_domains.add(item.domain)
        return exits

    def after_batch(self, st: TurnState, results: list[dict[str, Any]]) -> None:
        self.collect_batch_exits(st)
        while st.pending_exits:
            exit_item = st.pending_exits.pop(0)
            st.exited_this_turn.add(exit_item.domain)
            if exit_item.domain not in st.exited_order:
                st.exited_order.append(exit_item.domain)
            st.completed_exits.append(exit_item)
            d_obj = self.manager.get_domain(exit_item.domain)
            if d_obj is not None:
                rewrite_in_memory_messages(st.tool_messages, d_obj, exit_item.summary)
                st.persist_messages = [
                    m for m in st.persist_messages if not is_domain_work_message(m, d_obj)
                ]
            if self.restore_rounds_on_exit and not self.manager.is_focused(st.chat_id):
                st.max_rounds = max(st.round_idx + 1, st.base_max_rounds)
        st.batch_exited_domains.clear()
        st.batch_focus_domain = None

    def on_turn_end(self, st: TurnState) -> None:
        try:
            # Never mutate HistoryStore directly inside F; defer idle-exit commit to SessionPipeline
            exit_ev = self.manager.after_turn(
                st.chat_id,
                st.used_domains,
                history=None,
            )
            if exit_ev is not None:
                if exit_ev.domain not in st.exited_order:
                    st.exited_order.append(exit_ev.domain)
                st.completed_exits.append(exit_ev)
                d_obj = self.manager.get_domain(exit_ev.domain)
                if d_obj is not None:
                    st.persist_messages = [
                        m for m in st.persist_messages if not is_domain_work_message(m, d_obj)
                    ]
        except Exception:
            pass


class PruneHook(BaseTurnHook):
    """Drives ToolHistoryPruner at turn end."""

    def __init__(
        self,
        pruner: ToolHistoryPruner,
        focus_manager: FocusManager | None = None,
        *,
        skip_active_focus: bool = True,
    ) -> None:
        self.pruner = pruner
        self.focus_manager = focus_manager
        self.skip_active_focus = skip_active_focus

    def on_turn_end(self, st: TurnState) -> None:
        if (
            self.skip_active_focus
            and self.focus_manager is not None
            and self.focus_manager.is_focused(st.chat_id)
        ):
            return
        try:
            self.pruner.after_turn(st.chat_id, st.tools_executed)
        except Exception:
            pass


class FocusTraceHook(BaseTurnHook):
    """Dispatches telemetry callbacks declared on FocusDomains."""

    def __init__(self, manager: FocusManager, incoming_text: str = "") -> None:
        self.manager = manager
        self.incoming_text = incoming_text

    def on_turn_start(self, st: TurnState) -> None:
        if not self.incoming_text:
            return
        for domain in self.manager.domains():
            if domain.on_incoming is not None:
                try:
                    domain.on_incoming(st.chat_id, self.incoming_text)
                except Exception:
                    pass

    def on_model_call(
        self,
        st: TurnState,
        msg: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> None:
        active = self.manager.active(st.chat_id)
        tool_names = [(c.get("function") or {}).get("name") or "" for c in calls]
        target_domains: set[str] = set()
        for tn in tool_names:
            d = self.manager.domain_of_tool(str(tn))
            if d is not None:
                target_domains.add(d.name)
        if active is not None:
            target_domains.add(active.domain)
        for dn in target_domains:
            d_obj = self.manager.get_domain(dn)
            if d_obj is not None and d_obj.on_model_call is not None:
                try:
                    d_obj.on_model_call(st.chat_id, str(msg.get("content") or ""), calls)
                except Exception:
                    pass

    def on_turn_end(self, st: TurnState) -> None:
        if st.final is None or st.outcome not in ("reply", "exhausted", "filtered"):
            return
        raw_ans, clean_ans = st.final
        notify_domains = set(st.used_domains)
        active = self.manager.active(st.chat_id)
        if active is not None:
            notify_domains.add(active.domain)
        for dn in notify_domains:
            d_obj = self.manager.get_domain(dn)
            if d_obj is not None and d_obj.on_final_reply is not None:
                try:
                    d_obj.on_final_reply(st.chat_id, raw_ans, clean_ans)
                except Exception:
                    pass


class EventTraceHook(BaseTurnHook):
    """Emits structured turn lifecycle telemetry to EventSink."""

    def __init__(self, event_sink: EventSink, app_id: str = "frostcore") -> None:
        self.event_sink = event_sink
        self.app_id = app_id

    def on_turn_end(self, st: TurnState) -> None:
        self.event_sink.emit(
            StructuredEvent(
                app_id=self.app_id,
                kind="turn.finished",
                chat_id=st.chat_id,
                payload={
                    "outcome": st.outcome,
                    "llm_rounds": st.round_idx,
                    "tools_used": list(st.tools_used),
                    "tool_trace_count": len(st.tool_trace),
                },
            )
        )


__all__ = [
    "BaseTurnHook",
    "EventTraceHook",
    "FocusHook",
    "FocusTraceHook",
    "PruneHook",
    "TurnHook",
    "TurnHookChain",
    "TurnState",
]
