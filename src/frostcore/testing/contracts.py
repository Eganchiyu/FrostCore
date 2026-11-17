"""Reusable contract verification functions for FrostCore components and extensions."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from frostcore.contracts.events import EntityRef, IncomingEvent
from frostcore.contracts.turn import TurnInput
from frostcore.focus.domain import FocusDomain
from frostcore.focus.manager import FocusManager
from frostcore.focus.rewrite import rewrite_in_memory_messages
from frostcore.focus.view import ToolView
from frostcore.kernel.evaluator import TurnEvaluator
from frostcore.kernel.toolchain import FunctionRegistry
from frostcore.memory.diary import DiaryEngine
from frostcore.memory.history import JsonlHistoryStore
from frostcore.memory.lock import SeedLockedError
from frostcore.memory.seed import FrostSeed
from frostcore.modes.base import Mode
from frostcore.modes.router import (
    ROUTE_CHAT,
    ROUTE_FROZEN,
    ROUTE_MODE,
    FrozenChat,
    ModeBusyError,
    ModeRouter,
)
from frostcore.testing.fakes import FakeDiarySummarizer


def verify_seed_single_instance_contract(seed_dir: Path) -> None:
    """Verify same-process dual-open rejection and monotonic epoch on clean reopen."""
    with FrostSeed(seed_dir) as seed1:
        assert seed1.epoch == 1
        try:
            FrostSeed(seed_dir)
            raise AssertionError("Expected SeedLockedError when opening same FrostSeed twice")
        except SeedLockedError:
            pass

    with FrostSeed(seed_dir) as seed2:
        assert seed2.epoch == 2


def verify_focus_domain_contract(
    domain: FocusDomain,
    registry: FunctionRegistry,
    manager: FocusManager,
    history_dir: Path,
) -> None:
    """Verify progressive disclosure, in-memory rewrite, re-entry block, and orphan sweep."""
    chat_id = "contract_chat"
    registered = set(registry.list_functions())
    assert domain.entry_tool in registered, f"Missing entry_tool {domain.entry_tool}"
    assert domain.exit_tool in registered, f"Missing exit_tool {domain.exit_tool}"

    # 1. Default view hides exit_tool & domain_tools
    default_view = ToolView.resolve(registry, manager, chat_id)
    assert domain.entry_tool in default_view
    assert domain.exit_tool not in default_view
    for t in domain.domain_tools:
        assert t not in default_view

    # 2. Focused view restricts to domain.focus_allowed
    manager.enter(chat_id, domain.name, goal="verify contract")
    focused_view = ToolView.resolve(registry, manager, chat_id)
    assert focused_view.visible <= domain.focus_allowed
    assert domain.exit_tool in focused_view

    # 3. Same-turn re-entry prevention hides entry_tool
    manager.exit(chat_id, summary="done", reason="finish")
    post_exit_view = ToolView.resolve(registry, manager, chat_id, exited_this_turn={domain.name})
    assert domain.entry_tool not in post_exit_view

    # 4. In-memory rewrite preserves pairing & collapses work messages
    sample_tool = next(iter(domain.domain_tools), domain.entry_tool)
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": sample_tool, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": sample_tool, "content": "{}"},
        {"role": "user", "content": "interleaved human message"},
    ]
    rewrite_in_memory_messages(msgs, domain, "summary text")
    assert all(m.get("role") != "tool" for m in msgs)
    assert any(m.get("content") == "interleaved human message" for m in msgs)
    assert msgs[-1]["role"] == "user"
    assert "summary text" in msgs[-1]["content"]

    # 5. Orphan sweep cleans persisted leftover work
    history = JsonlHistoryStore(history_dir)
    history.append_message(chat_id, "user", "before")
    history.append_message(chat_id, "tool", "{}", name=sample_tool, focus_work=domain.name)
    swept = manager.sweep_orphans(history)
    assert swept >= 1
    cleaned = history.get_session(chat_id)
    assert all(m.get("role") != "tool" for m in cleaned)
    assert any(m.get("is_focus_summary") for m in cleaned)


async def verify_mode_router_contract(
    router: ModeRouter,
    mode_factory: Callable[[str], Mode],
) -> None:
    """Verify mode routing, global non-chat exclusivity, freeze counting, and catchup."""
    assert router.is_idle()
    assert router.route("c1")[0] == ROUTE_CHAT

    mode_inst = mode_factory("c1")
    router.enter("c1", mode_inst.name, instance=mode_inst)
    assert router.route("c1")[0] == ROUTE_MODE
    assert router.route("c2")[0] == ROUTE_FROZEN

    try:
        router.enter("c2", mode_inst.name)
        raise AssertionError("Expected ModeBusyError when entering second non-chat mode")
    except ModeBusyError:
        pass

    human = IncomingEvent(
        chat_id="c2",
        sender=EntityRef(entity_id="u1", display_name="Bob"),
        content="hey",
    )
    bot = IncomingEvent(
        chat_id="c2",
        sender=EntityRef(entity_id="b1", display_name="OtherBot"),
        content="auto",
        is_bot=True,
    )
    router.mark_frozen("c2", bot)
    router.mark_frozen("c2", human)
    assert router.pending_catchups()["c2"].messages == 1

    caught: list[FrozenChat] = []

    async def _on_catchup(f: FrozenChat) -> None:
        caught.append(f)

    router.set_catchup_handler(_on_catchup)
    router.exit("c1")
    assert router.is_idle()
    dispatched = await router.dispatch_catchups()
    assert dispatched == ["c2"]
    assert len(caught) == 1 and caught[0].messages == 1


async def verify_diary_concurrency_contract(seed_dir: Path) -> None:
    """Verify two-phase watermark slicing preserves messages added during Phase 2."""
    started = asyncio.Event()
    proceed = asyncio.Event()
    locks: dict[str, asyncio.Lock] = {}

    def _get_lock(cid: str) -> asyncio.Lock:
        return locks.setdefault(cid, asyncio.Lock())

    with FrostSeed(seed_dir) as seed:
        for i in range(6):
            role = "user" if i % 2 == 0 else "assistant"
            seed.history.append_message("chat_c", role, f"old turn {i}")

        summarizer = FakeDiarySummarizer(started_event=started, proceed_event=proceed)
        engine = DiaryEngine(
            seed,
            summarizer,
            _get_lock,
            min_dialogue_turns=4,
            keep_last_turns=2,
        )

        task = asyncio.create_task(engine.consolidate("chat_c"))
        await started.wait()

        # Simulate concurrent chat response arriving while Phase 2 is still running
        async with _get_lock("chat_c"):
            seed.history.append_message("chat_c", "user", "new live turn A")
            seed.history.append_message("chat_c", "assistant", "new live turn B")

        proceed.set()
        diary_id = await task
        assert diary_id is not None

        remaining = [m["content"] for m in seed.history.get_session("chat_c")]
        assert "new live turn A" in remaining
        assert "new live turn B" in remaining
        assert "old turn 0" not in remaining
        assert "old turn 4" in remaining and "old turn 5" in remaining


async def verify_turn_determinism_contract(
    evaluator_factory: Callable[[], TurnEvaluator],
    turn_input: TurnInput,
) -> None:
    """Verify identical TurnInput + FakeLLM produces byte-identical TurnOutput."""
    ev1 = evaluator_factory()
    ev2 = evaluator_factory()
    out1 = await ev1.evaluate(turn_input.model_copy(deep=True))
    out2 = await ev2.evaluate(turn_input.model_copy(deep=True))
    assert out1.model_dump() == out2.model_dump()


__all__ = [
    "verify_diary_concurrency_contract",
    "verify_focus_domain_contract",
    "verify_mode_router_contract",
    "verify_seed_single_instance_contract",
    "verify_turn_determinism_contract",
]
