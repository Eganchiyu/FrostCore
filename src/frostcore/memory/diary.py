"""DiaryEngine: two-phase lockless watermark consolidation protocol."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from frostcore.contracts.ports import DiarySummarizer
from frostcore.contracts.probe import EventSink, StructuredEvent
from frostcore.memory.dialogue_filter import is_dialogue_message
from frostcore.memory.seed import FrostSeed


class DiaryEngine:
    """Consolidates chat history into first-person diaries without blocking live replies.

    Protocol:
    1. Phase 1 (short chat_lock): snapshot dialogue messages and record watermark_seq.
    2. Phase 2 (lockless): summarize via DiarySummarizer and append_diary to FrostSeed.
    3. Phase 3 (short chat_lock): truncate only seq <= watermark_seq while keeping newer
       turns untouched and protecting any active focus workspace.
    """

    def __init__(
        self,
        seed: FrostSeed,
        summarizer: DiarySummarizer,
        lock_for_chat: Callable[[str], asyncio.Lock],
        *,
        is_chat_focused: Callable[[str], bool] | None = None,
        can_trigger_diary: Callable[[str], bool] | None = None,
        min_dialogue_turns: int = 12,
        keep_last_turns: int = 10,
        event_sink: EventSink | None = None,
        app_id: str = "frostcore",
    ) -> None:
        self.seed = seed
        self.summarizer = summarizer
        self._lock_for_chat = lock_for_chat
        self._is_chat_focused = is_chat_focused or (lambda _cid: False)
        self._can_trigger_diary = can_trigger_diary or (lambda _cid: True)
        self.min_dialogue_turns = max(1, min_dialogue_turns)
        self.keep_last_turns = max(0, keep_last_turns)
        self._event_sink = event_sink
        self._app_id = app_id
        self._consolidating: set[str] = set()

    def is_consolidating(self, chat_id: str) -> bool:
        return str(chat_id) in self._consolidating

    def _emit(self, kind: str, chat_id: str, payload: dict[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink.emit(
                StructuredEvent(app_id=self._app_id, kind=kind, chat_id=chat_id, payload=payload)
            )

    async def consolidate(self, chat_id: str, *, force: bool = False) -> str | None:
        cid = str(chat_id)
        if not self._can_trigger_diary(cid):
            self._emit("mode.capability_denied", cid, {"capability": "triggers_diary"})
            return None
        chat_lock = self._lock_for_chat(cid)

        # Phase 1: short lock to snapshot pure dialogue and watermark_seq
        async with chat_lock:
            if cid in self._consolidating or self._is_chat_focused(cid):
                return None
            entries = self.seed.history.get_entries(cid)
            dialogue = [e for e in entries if is_dialogue_message(e)]
            if not force and len(dialogue) < self.min_dialogue_turns:
                return None
            if not dialogue:
                return None
            watermark_seq = max(int(e["seq"]) for e in entries)
            self._consolidating.add(cid)

        try:
            # Phase 2: lockless async LLM summary + append-only diary write
            clean_dialogue = [
                {k: v for k, v in item.items() if k != "seq"} for item in dialogue
            ]
            summary = (await self.summarizer.summarize(cid, clean_dialogue)).strip()
            if not summary:
                return None

            entity_ids = sorted(
                {
                    str(e["entity_id"])
                    for e in dialogue
                    if e.get("entity_id")
                }
            )
            diary_id = await self.seed.a_append_diary(
                chat_id=cid,
                content=summary,
                entity_ids=entity_ids,
                metadata={"watermark_seq": watermark_seq, "turns": len(dialogue)},
            )

            # Phase 3: short lock to slice only <= watermark_seq
            async with chat_lock:
                preserve_from: int | None = None
                if self._is_chat_focused(cid):
                    # Preserve focus work added after Phase 1 while trimming pre-focus turns
                    current_entries = self.seed.history.get_entries(cid)
                    for item in current_entries:
                        if item.get("focus_work"):
                            preserve_from = int(item["seq"])
                            break
                removed = self.seed.history.truncate_before_seq(
                    cid,
                    watermark_seq,
                    keep_last_dialogue=self.keep_last_turns,
                    preserve_from_seq=preserve_from,
                )

            self._emit(
                "diary.consolidated",
                cid,
                {
                    "diary_id": diary_id,
                    "watermark_seq": watermark_seq,
                    "removed_entries": removed,
                },
            )
            return diary_id
        finally:
            self._consolidating.discard(cid)


__all__ = ["DiaryEngine"]
