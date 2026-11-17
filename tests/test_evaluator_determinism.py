"""Tests for deterministic F(TurnInput) -> TurnOutput evaluation and tool loop."""
from __future__ import annotations

from pathlib import Path

from frostcore.contracts import LLMResponse, TurnInput
from frostcore.focus import FocusManager
from frostcore.kernel import (
    SKIP_REPLY_SENTINEL,
    BaseTurnHook,
    FunctionRegistry,
    ToolContext,
    ToolResult,
    ToolSpec,
    TurnEvaluator,
    TurnState,
)
from frostcore.memory import JsonlHistoryStore
from frostcore.observability import MemoryEventSink
from frostcore.testing import FakeLLMClient, verify_turn_determinism_contract


async def _echo_tool(ctx: ToolContext, text: str) -> ToolResult:
    return ToolResult(success=True, content=f"echo:{text}", data={"ok": True})


async def _skip_tool(ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, content="skipped", data={"skip_reply": True})


async def test_turn_evaluator_determinism_contract() -> None:
    def _factory() -> TurnEvaluator:
        reg = FunctionRegistry(
            [
                ToolSpec(
                    name="echo",
                    description="Echo text",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                    handler=_echo_tool,
                )
            ]
        )
        llm = FakeLLMClient(
            [
                LLMResponse(
                    content="let me check",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                        }
                    ],
                ),
                LLMResponse(content="final deterministic answer"),
            ]
        )
        return TurnEvaluator(llm=llm, registry=reg, focus_manager=FocusManager())

    t_in = TurnInput(
        chat_id="c1",
        system_prompt="You are a calm bot.",
        messages=[{"role": "user", "content": "ping"}],
        now=1_700_000_000.0,
        rng_seed=99,
        max_rounds=4,
    )
    await verify_turn_determinism_contract(_factory, t_in)


async def test_turn_evaluator_skip_and_single_exit_hook(tmp_path: Path) -> None:
    ended: list[str] = []

    class _RecordingHook(BaseTurnHook):
        def on_turn_end(self, st: TurnState) -> None:
            ended.append(st.outcome)

    reg = FunctionRegistry(
        [
            ToolSpec(
                name="skip",
                description="Stay quiet",
                parameters={"type": "object", "properties": {}},
                handler=_skip_tool,
                persist_result=False,
            )
        ]
    )
    llm = FakeLLMClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "skip", "arguments": "{}"},
                    }
                ],
            )
        ]
    )
    sink = MemoryEventSink()
    ev = TurnEvaluator(
        llm=llm,
        registry=reg,
        history=JsonlHistoryStore(tmp_path / "h"),
        extra_hooks=[_RecordingHook()],
        event_sink=sink,
        app_id="test-app",
    )
    out = await ev.evaluate(
        TurnInput(chat_id="c1", messages=[{"role": "user", "content": "ignored"}])
    )
    assert out.outcome == "skip"
    assert out.reply == SKIP_REPLY_SENTINEL
    assert ended == ["skip"]
    assert len(sink.events(kind="turn.finished")) == 1


async def test_turn_evaluator_exhausted_and_text_tool_fallback() -> None:
    reg = FunctionRegistry(
        [
            ToolSpec(
                name="echo",
                description="Echo",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                handler=_echo_tool,
            )
        ]
    )
    llm = FakeLLMClient(
        [
            LLMResponse(
                content='<tool_call>{"name": "echo", "arguments": {"text": "abc"}}</tool_call>'
            ),
            LLMResponse(content="fallback summary after exhaustion"),
        ]
    )
    ev = TurnEvaluator(
        llm=llm,
        registry=reg,
        enable_text_tool_fallback=True,
    )
    out = await ev.evaluate(
        TurnInput(chat_id="c1", messages=[{"role": "user", "content": "go"}], max_rounds=1)
    )
    assert out.outcome == "exhausted"
    assert out.used_tools == ("echo",)
    assert out.reply == "fallback summary after exhaustion"
