"""Focus domain declaration: pure metadata and optional lifecycle hooks."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FocusDomain:
    """Declarative specification for a progressive-disclosure focus domain."""

    name: str
    title: str
    entry_tool: str
    exit_tool: str
    domain_tools: frozenset[str]
    keep_tools: frozenset[str] = frozenset()
    summary_prefix: str = ""
    exit_hint: str = ""
    idle_turns: int = 5
    max_rounds: int = 20
    max_trace: int = 20
    default_fallback: str = ""
    empty_trace_fallback: str = ""

    on_enter: Callable[[str, str], None] | None = None
    on_exit: Callable[[str, str, str], None] | None = None
    on_model_call: Callable[[str, str, list[dict[str, Any]]], None] | None = None
    on_final_reply: Callable[[str, str, str], None] | None = None
    on_incoming: Callable[[str, str], None] | None = None
    stub: Callable[[dict[str, Any]], str] | None = None

    @property
    def effective_summary_prefix(self) -> str:
        return self.summary_prefix or f"[{self.title} Summary] "

    @property
    def effective_exit_hint(self) -> str:
        return (
            self.exit_hint
            or f"(You have exited the {self.title} focus mode; reply directly using the summary.)"
        )

    @property
    def work_tools(self) -> frozenset[str]:
        return frozenset({self.entry_tool, self.exit_tool, *self.domain_tools})

    @property
    def hidden_in_default(self) -> frozenset[str]:
        return frozenset({self.exit_tool, *self.domain_tools})

    @property
    def focus_allowed(self) -> frozenset[str]:
        return frozenset({self.exit_tool, *self.domain_tools, *self.keep_tools})


__all__ = ["FocusDomain"]
