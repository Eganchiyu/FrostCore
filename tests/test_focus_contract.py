"""FocusPort contract tests: zero-config identity, write gating, orphan sweep, no legacy fields."""
from __future__ import annotations

from pathlib import Path

from frostcore import FrostApp, FrostSeed
from frostcore.contracts import EntityRef, IncomingEvent, LLMResponse, TurnInput
from frostcore.focus import (
    FocusDomain,
    FocusManager,
    ToolView,
    focus_finish_spec,
    focus_start_spec,
)
from frostcore.kernel import FunctionRegistry, ToolContext, ToolResult, ToolSpec, TurnEvaluator
from frostcore.memory import JsonlHistoryStore
from frostcore.testing import FakeLLMClient, MemoryChannelAdapter, verify_focus_domain_contract


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
    assert len(out.focus_exits) == 1
    assert out.focus_exits[0].summary == "Drew cat"
    # Pure evaluator leaves HistoryStore untouched and drops ephemeral art messages
    assert out.persist_messages == []
    assert [m["content"] for m in history.get_session("c1")] == ["Draw a cat"]


async def test_focus_exit_pipeline_commits_clean_summary(tmp_path: Path) -> None:
    domain = FocusDomain(
        name="art",
        title="Art",
        entry_tool="art_start",
        exit_tool="art_finish",
        domain_tools=frozenset({"art_draw"}),
    )
    adapter = MemoryChannelAdapter("mem")
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
    with FrostSeed(tmp_path / "focus_pipe.seed") as seed:
        app = FrostApp(
            app_id="focus-pipe",
            seed=seed,
            llm=llm,
            adapters=[adapter],
            focus_domains=[domain],
            tools=[
                ToolSpec(
                    name="art_draw",
                    description="Draw art",
                    parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
                    handler=_draw,
                )
            ],
            enable_diary=False,
            close_seed_on_stop=False,
        )
        app.registry.register(focus_start_spec(domain, app.focus_manager))
        app.registry.register(focus_finish_spec(domain, app.focus_manager))

        async with app:
            await adapter.inject(
                IncomingEvent(
                    chat_id="c1",
                    sender=EntityRef(entity_id="u1", display_name="User"),
                    content="Draw a cat",
                )
            )
            await app.pipeline.drain()

        persisted = seed.history.get_session("c1")
        assert all(m.get("role") != "tool" for m in persisted)
        assert all("phone_focus_work" not in m and "is_phone_summary" not in m for m in persisted)
        assert any(m.get("is_focus_summary") and "Drew cat" in m["content"] for m in persisted)
        assert persisted[-1]["role"] == "assistant"
        assert persisted[-1]["content"] == "Here is your cat!"


async def test_focus_o1_orphan_sweep_via_chat_sessions_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    domain = FocusDomain(
        name="art",
        title="Art",
        entry_tool="art_start",
        exit_tool="art_finish",
        domain_tools=frozenset({"art_draw"}),
    )
    seed_dir = tmp_path / "orphan_idx.seed"
    with FrostSeed(seed_dir) as seed:
        seed.history.append_message("clean_chat", "user", "hello")
        seed.history.append_message("dirty_chat", "user", "start drawing")
        seed.history.append_message("dirty_chat", "tool", "{}", name="art_draw", focus_work="art")
        seed.set_chat_focus_domain("dirty_chat", "art")
        assert seed.list_active_focus_chats() == ["dirty_chat"]

    with FrostSeed(seed_dir) as reopened:
        def _forbid_full_scan() -> list[str]:
            raise AssertionError("Startup must use SQLite index instead of full JSONL scan")

        monkeypatch.setattr(reopened.history, "list_chat_ids", _forbid_full_scan)
        app = FrostApp(
            app_id="o1-sweep",
            seed=reopened,
            llm=FakeLLMClient(),
            focus_domains=[domain],
            enable_diary=False,
            close_seed_on_stop=False,
        )
        async with app:
            pass

        assert reopened.list_active_focus_chats() == []
        swept_session = reopened.history.get_session("dirty_chat")
        assert any(m.get("is_focus_summary") for m in swept_session)
