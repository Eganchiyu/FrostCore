"""Deterministic turn evaluation function F(TurnInput) -> TurnOutput."""
from __future__ import annotations

import copy
import json
import random
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any

from frostcore.contracts.ports import HistoryStore, LLMClient, LLMRequest, LLMResponse
from frostcore.contracts.probe import EventSink, StructuredEvent
from frostcore.contracts.turn import FocusDelta, TurnInput, TurnOutput
from frostcore.focus.manager import FocusManager
from frostcore.focus.view import ToolView
from frostcore.kernel.hooks import (
    EventTraceHook,
    FocusHook,
    FocusTraceHook,
    PruneHook,
    TurnHook,
    TurnHookChain,
    TurnState,
)
from frostcore.kernel.pruner import ToolHistoryPruner
from frostcore.kernel.toolchain import (
    SKIP_REPLY_SENTINEL,
    FunctionRegistry,
    ToolCallManager,
    ToolContext,
)

_TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


def extract_text_tool_calls(
    content: str | None,
    valid_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Optional fallback parser for <tool_call>{...}</tool_call> text markup."""
    if not content or "<tool_call>" not in content:
        return [], content or ""

    parsed_calls: list[dict[str, Any]] = []
    for match in _TEXT_TOOL_CALL_RE.finditer(content):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("name") or "")
        if not name or (valid_names is not None and name not in valid_names):
            continue
        args = obj.get("arguments", {})
        args_str = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        parsed_calls.append(
            {
                "id": f"textcall_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": name, "arguments": args_str},
            }
        )

    if not parsed_calls:
        return [], content
    cleaned = _TEXT_TOOL_CALL_RE.sub("", content).strip()
    return parsed_calls, cleaned


class TurnEvaluator:
    """Implements F(TurnInput) -> TurnOutput with deterministic seed/time isolation."""

    def __init__(
        self,
        llm: LLMClient,
        registry: FunctionRegistry | None = None,
        focus_manager: FocusManager | None = None,
        history: HistoryStore | None = None,
        *,
        seed: Any = None,
        pruner: ToolHistoryPruner | None = None,
        extra_hooks: Sequence[TurnHook] = (),
        event_sink: EventSink | None = None,
        app_id: str = "frostcore",
        tool_timeout_seconds: float = 60.0,
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 1024,
        enable_text_tool_fallback: bool = False,
        gate_exited_writes: bool = True,
        restore_rounds_on_exit: bool = True,
    ) -> None:
        self.llm = llm
        self.registry = registry or FunctionRegistry()
        self.focus_manager = focus_manager or FocusManager()
        self.history = history
        self.seed = seed
        self.pruner = pruner
        self.extra_hooks = list(extra_hooks)
        self.event_sink = event_sink
        self.app_id = app_id
        self.tool_timeout_seconds = tool_timeout_seconds
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.enable_text_tool_fallback = enable_text_tool_fallback
        self.gate_exited_writes = gate_exited_writes
        self.restore_rounds_on_exit = restore_rounds_on_exit

    def _build_hooks(self, turn_input: TurnInput) -> tuple[TurnHookChain, FocusHook]:
        focus_hook = FocusHook(
            self.focus_manager,
            self.history,
            restore_rounds_on_exit=self.restore_rounds_on_exit,
            gate_exited_writes=self.gate_exited_writes,
        )
        hooks: list[TurnHook] = [focus_hook]
        if self.pruner is not None:
            hooks.append(PruneHook(self.pruner, self.focus_manager))
        incoming_text = "\n".join(getattr(ev, "content", "") for ev in turn_input.events).strip()
        hooks.append(FocusTraceHook(self.focus_manager, incoming_text=incoming_text))
        if self.event_sink is not None:
            hooks.append(EventTraceHook(self.event_sink, app_id=self.app_id))
        hooks.extend(self.extra_hooks)
        return TurnHookChain(hooks), focus_hook

    @staticmethod
    def _build_initial_messages(turn_input: TurnInput) -> list[dict[str, Any]]:
        msgs = [copy.deepcopy(m) for m in turn_input.messages]
        if turn_input.system_prompt:
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = turn_input.system_prompt
            else:
                msgs.insert(0, {"role": "system", "content": turn_input.system_prompt})
        return msgs

    @staticmethod
    def _tool_payload(tool_message: dict[str, Any]) -> dict[str, Any]:
        content = tool_message.get("content")
        if not isinstance(content, str):
            return {}
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def evaluate(self, turn_input: TurnInput) -> TurnOutput:
        """Linear deterministic turn evaluation F(TurnInput) -> TurnOutput."""
        tool_messages = self._build_initial_messages(turn_input)
        initial_len = len(tool_messages)
        tool_manager = ToolCallManager(
            self.registry,
            max_rounds=turn_input.max_rounds,
            timeout_seconds=self.tool_timeout_seconds,
        )
        context = ToolContext(
            chat_id=str(turn_input.chat_id),
            history=self.history,
            focus_manager=self.focus_manager,
            seed=self.seed,
            extra=dict(turn_input.extra),
        )
        st = TurnState(
            chat_id=str(turn_input.chat_id),
            context=context,
            tool_messages=tool_messages,
            active_registry=self.registry,
            active_tool_manager=tool_manager,
            now=float(turn_input.now),
            rng=random.Random(turn_input.rng_seed),
            max_rounds=tool_manager.max_rounds,
            base_max_rounds=tool_manager.max_rounds,
        )
        hooks, focus_hook = self._build_hooks(turn_input)
        valid_names = set(self.registry.list_functions()) or None

        hooks.on_turn_start(st)
        try:
            while st.round_idx < st.max_rounds:
                st.round_idx += 1
                hooks.before_llm(st)
                view = ToolView.resolve(
                    st.active_registry,
                    self.focus_manager,
                    st.chat_id,
                    st.exited_this_turn,
                )
                tools_schema = st.active_registry.get_tools(view=view)
                t0 = time.monotonic()
                resp = await self.llm.complete(
                    LLMRequest(
                        messages=copy.deepcopy(st.tool_messages),
                        tools=tools_schema,
                        tool_choice="auto" if tools_schema else None,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_tokens=self.max_tokens,
                    )
                )
                llm_ms = int((time.monotonic() - t0) * 1000)

                calls = list(resp.tool_calls or [])
                content = resp.content or ""
                if not calls and self.enable_text_tool_fallback:
                    parsed, cleaned = extract_text_tool_calls(content, valid_names)
                    if parsed:
                        calls = parsed
                        content = cleaned

                if not calls:
                    if resp.finish_reason == "content_filter":
                        st.outcome = "filtered"
                        st.final = ("Filtered", "Filtered")
                    else:
                        st.outcome = "reply"
                        st.final = (content, content)
                    st.tool_trace.append(
                        {
                            "round": st.round_idx,
                            "llm_ms": llm_ms,
                            "thought": content[:240],
                            "calls": [],
                        }
                    )
                    return self._build_output(st, initial_len)

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                }
                hooks.on_model_call(st, assistant_msg, calls)
                st.tools_executed = True
                tool_names = [
                    str((c.get("function") or {}).get("name") or "")
                    for c in calls
                ]
                for tn in tool_names:
                    if tn:
                        st.tools_used.append(tn)

                if content.strip() and "skip" not in tool_names:
                    if content.strip() not in st.sent_thoughts:
                        st.sent_thoughts.add(content.strip())
                        st.pending_thought = content.strip()

                if st.pending_thought and not focus_hook.gate_exited_writes:
                    self._persist_thought(st, hooks)

                st.tool_messages.append(assistant_msg)
                t_exec = time.monotonic()
                results = await st.active_tool_manager.execute_tool_calls(
                    calls,
                    st.context,
                    finish_reason=resp.finish_reason,
                    view=view,
                )
                exec_ms = int((time.monotonic() - t_exec) * 1000)
                st.tool_trace.append(
                    {
                        "round": st.round_idx,
                        "llm_ms": llm_ms,
                        "exec_ms": exec_ms,
                        "thought": content[:240],
                        "calls": [
                            {
                                "name": str((c.get("function") or {}).get("name") or ""),
                                "args": str((c.get("function") or {}).get("arguments") or "")[:200],
                            }
                            for c in calls
                        ],
                    }
                )

                if st.context.metadata.get("skip_reply"):
                    st.outcome = "skip"
                    st.final = (SKIP_REPLY_SENTINEL, SKIP_REPLY_SENTINEL)
                    return self._build_output(st, initial_len)

                focus_hook.collect_batch_exits(st)
                if st.pending_thought:
                    self._persist_thought(st, hooks)

                self._persist_tool_results(st, hooks, results)
                st.tool_messages.extend(results)
                hooks.after_batch(st, results)

            # Max rounds exhausted -> force plain text closing turn
            closing_messages = copy.deepcopy(st.tool_messages) + [
                {
                    "role": "user",
                    "content": (
                        "Tool call budget exhausted for this turn. "
                        "Provide a direct text reply without calling any more tools."
                    ),
                }
            ]
            closing_resp: LLMResponse = await self.llm.complete(
                LLMRequest(
                    messages=closing_messages,
                    tools=[],
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                )
            )
            st.outcome = "exhausted"
            st.final = (closing_resp.content, closing_resp.content)
            return self._build_output(st, initial_len)
        except Exception as exc:
            st.outcome = "error"
            st.final = ("", "")
            st.tool_trace.append({"round": st.round_idx, "error": str(exc)})
            if self.event_sink is not None:
                self.event_sink.emit(
                    StructuredEvent(
                        app_id=self.app_id,
                        kind="turn.error",
                        chat_id=st.chat_id,
                        payload={"error": str(exc), "round": st.round_idx},
                    )
                )
            return self._build_output(st, initial_len)
        finally:
            hooks.on_turn_end(st)

    def _persist_thought(self, st: TurnState, hooks: TurnHookChain) -> None:
        thought = st.pending_thought
        st.pending_thought = ""
        if self.history is None or not thought:
            return
        decision = hooks.classify_persist(
            st, {"role": "assistant", "content": thought, "is_tool_thought": True}
        )
        if decision.action == "drop":
            return
        extra: dict[str, Any] = {"is_tool_thought": True}
        if decision.focus_work:
            extra["focus_work"] = decision.focus_work
        self.history.append_message(st.chat_id, "assistant", thought, **extra)

    def _persist_tool_results(
        self,
        st: TurnState,
        hooks: TurnHookChain,
        results: list[dict[str, Any]],
    ) -> None:
        if self.history is None:
            return
        for msg in results:
            tool_name = str(msg.get("name") or "")
            spec = st.active_registry.get_spec(tool_name)
            if spec is not None and not spec.persist_result:
                continue
            data = self._tool_payload(msg).get("data")
            if isinstance(data, dict) and data.get("no_persist"):
                continue
            decision = hooks.classify_persist(st, msg)
            if decision.action == "drop":
                continue
            extra: dict[str, Any] = {
                "name": tool_name,
                "tool_call_id": msg.get("tool_call_id"),
            }
            if decision.focus_work:
                extra["focus_work"] = decision.focus_work
            self.history.append_message(st.chat_id, "tool", msg.get("content"), **extra)

    def _build_output(self, st: TurnState, initial_len: int) -> TurnOutput:
        raw_reply, reply = st.final if st.final is not None else ("", "")
        active_after = self.focus_manager.active(st.chat_id)
        return TurnOutput(
            reply=reply,
            raw_reply=raw_reply,
            turn_messages=copy.deepcopy(st.tool_messages[initial_len:]),
            tool_trace=copy.deepcopy(st.tool_trace),
            focus_delta=FocusDelta(
                entered=st.entered_domains[-1] if st.entered_domains else None,
                exited=tuple(st.exited_order),
                active_after=active_after.domain if active_after else None,
            ),
            used_tools=tuple(st.tools_used),
            outcome=st.outcome,
            llm_rounds=st.round_idx,
        )


__all__ = ["TurnEvaluator", "extract_text_tool_calls"]
