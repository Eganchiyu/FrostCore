"""Tests for DiaryEngine two-phase watermark concurrency and dialogue filtering."""
from __future__ import annotations

import asyncio
from pathlib import Path

from frostcore.focus import FocusDomain, FocusManager
from frostcore.memory import DiaryEngine, FrostSeed, dialogue_length
from frostcore.testing import FakeDiarySummarizer, verify_diary_concurrency_contract


async def test_diary_concurrency_contract(tmp_path: Path) -> None:
    await verify_diary_concurrency_contract(tmp_path / "diary.seed")


def test_dialogue_filter_excludes_tools_thoughts_and_system_injected() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "thinking...", "is_tool_thought": True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "{}"},
        {"role": "user", "content": "catchup", "is_system_injected": True},
        {"role": "assistant", "content": "work", "focus_work": "art"},
        {"role": "assistant", "content": "hello!"},
    ]
    assert dialogue_length(msgs) == 2


async def test_diary_phase3_protects_focus_started_during_phase2(tmp_path: Path) -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()
    locks: dict[str, asyncio.Lock] = {}

    def _get_lock(cid: str) -> asyncio.Lock:
        return locks.setdefault(cid, asyncio.Lock())

    fm = FocusManager()
    fm.register(
        FocusDomain(
            name="art",
            title="Art",
            entry_tool="art_start",
            exit_tool="art_finish",
            domain_tools=frozenset({"art_draw"}),
        )
    )

    with FrostSeed(tmp_path / "focus_diary.seed") as seed:
        for i in range(4):
            seed.history.append_message("c1", "user" if i % 2 == 0 else "assistant", f"msg {i}")

        engine = DiaryEngine(
            seed,
            FakeDiarySummarizer(started_event=started, proceed_event=proceed),
            _get_lock,
            is_chat_focused=fm.is_focused,
            min_dialogue_turns=4,
            keep_last_turns=1,
        )
        task = asyncio.create_task(engine.consolidate("c1"))
        await started.wait()

        # Enter focus during Phase 2 and append focus work entries
        async with _get_lock("c1"):
            fm.enter("c1", "art", goal="sketch")
            seed.history.append_message(
                "c1",
                "tool",
                '{"ok":true}',
                name="art_draw",
                focus_work="art",
            )

        proceed.set()
        diary_id = await task
        assert diary_id is not None

        contents = [m["content"] for m in seed.history.get_session("c1")]
        assert '{"ok":true}' in contents
        assert "msg 3" in contents
        assert "msg 0" not in contents
