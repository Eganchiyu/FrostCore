"""Standard entry/exit ToolSpec factories for FocusDomain."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from frostcore.focus.domain import FocusDomain
from frostcore.focus.manager import FocusManager
from frostcore.kernel.toolchain import ToolContext, ToolResult, ToolSpec


def focus_start_spec(
    domain: FocusDomain,
    manager: FocusManager,
    *,
    description: str = "",
    handler: Callable[[ToolContext, str], Awaitable[ToolResult]] | None = None,
) -> ToolSpec:
    """Create a standard entry ToolSpec bound to a specific FocusManager."""

    async def _default_start(context: ToolContext, goal: str = "") -> ToolResult:
        hist = None if context.metadata.get("defer_history_persist") else context.history
        state = manager.enter(context.chat_id, domain.name, goal=goal, history=hist)
        return ToolResult(
            success=True,
            content=f"Entered {domain.title} focus mode.",
            data={"focus": "entered", "domain": domain.name, "goal": state.goal},
            name=domain.entry_tool,
        )

    return ToolSpec(
        name=domain.entry_tool,
        description=description or f"Enter {domain.title} focus workspace.",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Optional goal for this focus session."}
            },
            "required": [],
        },
        handler=handler or _default_start,
    )


def focus_finish_spec(
    domain: FocusDomain,
    manager: FocusManager,
    *,
    description: str = "",
    handler: Callable[[ToolContext, str], Awaitable[ToolResult]] | None = None,
) -> ToolSpec:
    """Create a standard exit ToolSpec bound to a specific FocusManager."""

    async def _default_finish(context: ToolContext, summary: str = "") -> ToolResult:
        hist = None if context.metadata.get("defer_history_persist") else context.history
        exit_ev = manager.exit(
            context.chat_id,
            history=hist,
            summary=summary,
            reason="finish",
            domain=domain.name,
        )
        exits = context.metadata.setdefault("focus_exits", [])
        if exit_ev is not None:
            exits.append(exit_ev)
        return ToolResult(
            success=True,
            content=f"Exited {domain.title} focus mode.",
            data={
                "focus": "exited",
                "domain": domain.name,
                "summary": exit_ev.summary if exit_ev else summary,
                "no_persist": True,
            },
            name=domain.exit_tool,
        )

    return ToolSpec(
        name=domain.exit_tool,
        description=description or f"Finish and summarize {domain.title} focus work.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Concise summary of completed work."}
            },
            "required": ["summary"],
        },
        handler=handler or _default_finish,
        persist_result=False,
    )


__all__ = ["focus_finish_spec", "focus_start_spec"]
