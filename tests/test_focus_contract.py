"""FocusPort contract tests: zero-config identity, write gating, orphan sweep, no legacy fields."""
from __future__ import annotations

from pathlib import Path

from frostcore.contracts import LLMResponse, TurnInput
from frostcore.focus import (
    FocusDomain,
    FocusManager,
    ToolView,
    focus_finish_spec,
    focus_start_spec,
)
from frostcore.kernel import FunctionRegistry, ToolContext, ToolResult, ToolSpec, TurnEvaluator
from frostcore.memory import JsonlHistoryStore
from frostcore.testing import FakeLLMClient, verify_focus_domain_contract


async def _draw(ctx: ToolContext, prompt: str) -> ToolResult:
    if ctx.focus_manager is not None:
        ctx.focus_manager.record_step(ctx.chat_id, f"drew:{prompt}", domain="art")
    return ToolResult(success=True, content=f"image for {prompt}", name="art_draw")


def test_focus_zero_config_identity_when_unregistered() -> None:
    reg = FunctionRegistry(
        [
            ToolSpec(
                name="ping",
                description="Ping",
                parameters={"type": "object", "properties": {}},
                handler=_draw,
            )
        ]
    )
    fm = FocusManager()
    view = ToolView.resolve(reg, fm, "c1")
    assert view.visible == frozenset({"ping"})


def test_focus_domain_contract_passes_cleanly(tmp_path: Path) -> None:
    domain = FocusDomain(
        name="art",
        title="Art",
        entry_tool="art_start",
        exit_tool="art_finish",
        domain_tools=frozenset({"art_draw"}),
    )
    fm = FocusManager()
    fm.register(domain)
    reg = FunctionRegistry(
        [
            focus_start_spec(domain, fm),
            ToolSpec(
                name="art_draw",
                description="Draw art",
                parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
                handler=_draw,
            ),
            focus_finish_spec(domain, fm),
        ]
    )
    verify_focus_domain_contract(domain, reg, fm, tmp_path / "hist")


async def test_focus_exit_write_gating_drops_ephemeral_work_from_history(tmp_path: Path) -> None:
    domain = FocusDomain(
        name="art",
        title="Art",
        entry_tool="art_start",
        exit_tool="art_finish",
        domain_tools=frozenset({"art_draw"}),
    )
    fm = FocusManager()
    fm.register(domain)
    reg = FunctionRegistry(
        [
            focus_start_spec(domain, fm),
            ToolSpec(
                name="art_draw",
                description="Draw art",
                parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
                handler=_draw,
            ),
            focus_finish_spec(domain, fm),
        ]
    )
    history = JsonlHistoryStore(tmp_path / "hist")
    history.append_message("c1", "user", "Draw a cat")

    llm = FakeLLMClient(
        [
            LLMResponse(
                content="entering studio",
                tool_calls=[
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "art_start", "arguments": '{"goal": "cat"}'},
                    }
                ],
            ),
            LLMResponse(
                content="sketching now",
                tool_calls=[
                    {
                        "id": "t2",
                        "type": "function",
                        "function": {"name": "art_draw", "arguments": '{"prompt": "cat"}'},
                    }
                ],
            ),
            LLMResponse(
                content="wrapping up",
                tool_calls=[
                    {
                        "id": "t3",
                        "type": "function",
                        "function": {"name": "art_finish", "arguments": '{"summary": "Drew cat"}'},
                    }
                ],
            ),
            LLMResponse(content="Here is your cat!"),
        ]
    )
    evaluator = TurnEvaluator(llm=llm, registry=reg, focus_manager=fm, history=history)
    out = await evaluator.evaluate(
        TurnInput(chat_id="c1", messages=history.get_session("c1"), max_rounds=4)
    )
    assert out.outcome == "reply"
    assert out.focus_delta.entered == "art"
    assert out.focus_delta.exited == ("art",)
    assert out.focus_delta.active_after is None

    persisted = history.get_session("c1")
    # Ephemeral focus thoughts & art_draw outputs must be replaced by clean summary
    assert all(m.get("role") != "tool" for m in persisted)
    assert all("phone_focus_work" not in m and "is_phone_summary" not in m for m in persisted)
    assert any(m.get("is_focus_summary") and "Drew cat" in m["content"] for m in persisted)
