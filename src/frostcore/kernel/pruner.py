"""Tool history pruner collapsing stale tool outputs into compact stubs."""
from __future__ import annotations

import json
from typing import Any

from frostcore.contracts.ports import HistoryStore
from frostcore.focus.manager import FocusManager

STUB_PREFIX = "[Tool History Collapsed] "
STUB_MAX_CHARS = 220


def _decode_payload(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_stub(name: str, payload: dict[str, Any], focus_manager: FocusManager | None) -> str:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    note = str(data.get("note") or "").strip()

    domain = focus_manager.domain_of_tool(name) if focus_manager is not None else None
    if domain is not None and domain.stub is not None:
        text = domain.stub(payload)
    else:
        text = str(payload.get("content") or "").replace("\n", " ").strip()

    if note:
        text = f"{text} note:{note}".strip()
    return f"{STUB_PREFIX}{text}"[:STUB_MAX_CHARS]


class ToolHistoryPruner:
    """Tracks per-chat idle turns without tool calls and collapses old tool outputs."""

    def __init__(
        self,
        history: HistoryStore,
        focus_manager: FocusManager | None = None,
        *,
        idle_turns: int = 2,
    ) -> None:
        self.history = history
        self.focus_manager = focus_manager
        self.idle_turns = max(1, int(idle_turns))
        self._idle_counts: dict[str, int] = {}

    def note_turn(self, chat_id: str, tools_executed: bool) -> bool:
        cid = str(chat_id)
        if tools_executed:
            self._idle_counts[cid] = 0
            return False
        count = self._idle_counts.get(cid, 0) + 1
        if count >= self.idle_turns:
            self._idle_counts[cid] = 0
            return True
        self._idle_counts[cid] = count
        return False

    def prune(self, chat_id: str) -> int:
        cid = str(chat_id)
        session = self.history.get_session(cid)
        pruned = 0
        for message in session:
            if message.get("role") != "tool" or message.get("pruned"):
                continue
            payload = _decode_payload(message.get("content"))
            message["content"] = _format_stub(
                str(message.get("name") or ""),
                payload,
                self.focus_manager,
            )
            message["pruned"] = True
            pruned += 1
        if pruned > 0:
            self.history.replace_session(cid, session)
        return pruned

    def after_turn(self, chat_id: str, tools_executed: bool) -> int:
        if self.note_turn(chat_id, tools_executed):
            return self.prune(chat_id)
        return 0


__all__ = ["STUB_MAX_CHARS", "STUB_PREFIX", "ToolHistoryPruner"]
