"""Minimal built-in PromptComposer for Quickstart and generic hosts."""
from __future__ import annotations

from typing import Any

from frostcore.contracts.events import IncomingEvent, SystemInjectedEvent
from frostcore.contracts.ports import ComposeContext, ComposedPrompt

_OPENAI_MSG_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")


def format_inbound_batch(events: tuple[IncomingEvent | SystemInjectedEvent, ...]) -> str:
    """Render a batch of inbound events into a clean user message string."""
    lines: list[str] = []
    for ev in events:
        if isinstance(ev, SystemInjectedEvent):
            lines.append(f"[{ev.system_kind}] {ev.content}")
        else:
            speaker = ev.sender.display_name or ev.sender.entity_id
            lines.append(f"{speaker}: {ev.content}" if speaker else ev.content)
    return "\n".join(lines).strip()


class SimplePromptComposer:
    """Default zero-syntax PromptComposer assembling persona, memories, and recent turns."""

    def __init__(
        self,
        persona: str,
        *,
        memory_header: str = "Relevant past memories:",
        max_history_messages: int = 24,
    ) -> None:
        self.persona = persona
        self.memory_header = memory_header
        self.max_history_messages = max(1, int(max_history_messages))

    async def compose(self, ctx: ComposeContext) -> ComposedPrompt:
        sys_parts = [self.persona.strip()]
        if ctx.memories:
            mem_lines = [f"- {m.content}" for m in ctx.memories if m.content.strip()]
            if mem_lines:
                sys_parts.append(f"{self.memory_header}\n" + "\n".join(mem_lines))
        system_prompt = "\n\n".join(p for p in sys_parts if p)

        messages: list[dict[str, Any]] = []
        tail = [m for m in ctx.session if m.get("role") != "system"][-self.max_history_messages :]
        for msg in tail:
            clean = {k: msg[k] for k in _OPENAI_MSG_KEYS if k in msg and msg[k] is not None}
            if "role" in clean and "content" in clean:
                messages.append(clean)

        batch_text = format_inbound_batch(ctx.events)
        if batch_text:
            messages.append({"role": "user", "content": batch_text})

        return ComposedPrompt(system_prompt=system_prompt, messages=messages)


__all__ = ["SimplePromptComposer", "format_inbound_batch"]
