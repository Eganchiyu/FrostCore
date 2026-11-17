"""Tool specifications, registry, context, and async execution manager."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from frostcore.contracts.ports import HistoryStore

SKIP_REPLY_SENTINEL = "\x00FROSTCORE_SKIP_REPLY\x00"


@dataclass
class ToolResult:
    """Standard result envelope returned by every tool handler."""

    success: bool
    content: str
    data: Any = None
    error: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if self.success:
            self.error = ""
        elif not self.error:
            self.error = "tool_failed"

    @classmethod
    def failure(
        cls,
        content: str,
        error: str,
        *,
        data: Any = None,
        name: str = "",
    ) -> ToolResult:
        return cls(success=False, content=content, data=data, error=error, name=name)

    def to_message_content(self) -> str:
        payload = {
            "success": self.success,
            "content": self.content,
            "data": self.data,
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class ToolContext:
    """Explicit runtime context passed into tool handlers (zero global state)."""

    chat_id: str
    history: HistoryStore | None = None
    focus_manager: Any = None
    seed: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """Tool schema and async handler binding."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]
    persist_result: bool = True

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class FunctionRegistry:
    """Per-app registry of available tool specs."""

    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in specs or ():
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_spec(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_handler(self, name: str) -> Callable[..., Awaitable[ToolResult]] | None:
        spec = self._tools.get(name)
        return spec.handler if spec else None

    def list_functions(self) -> list[str]:
        return list(self._tools.keys())

    def get_tools(self, view: Any = None) -> list[dict[str, Any]]:
        if view is None:
            return [spec.to_schema() for spec in self._tools.values()]
        visible = view.visible if hasattr(view, "visible") else set(view)
        return [
            spec.to_schema()
            for spec in self._tools.values()
            if spec.name in visible
        ]


class ToolCallManager:
    """Executes tool calls with view gating, JSON validation, and timeouts."""

    def __init__(
        self,
        registry: FunctionRegistry,
        *,
        max_rounds: int = 4,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.registry = registry
        self.max_rounds = max(1, int(max_rounds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    async def execute_tool_call(
        self,
        tool_call: dict[str, Any],
        context: ToolContext,
        *,
        finish_reason: str = "",
        view: Any = None,
    ) -> dict[str, Any]:
        fn_info = tool_call.get("function") or {}
        name = str(fn_info.get("name") or "")
        raw_args = fn_info.get("arguments") or "{}"
        call_id = str(tool_call.get("id") or f"call_{int(time.time() * 1000)}")
        handler = self.registry.get_handler(name)

        if view is not None:
            visible = view.visible if hasattr(view, "visible") else set(view)
            if name not in visible:
                result = ToolResult.failure(
                    "Tool is not visible in the current tool view.",
                    "tool_not_in_view",
                    name=name,
                )
                return self._as_tool_message(call_id, name, result)

        if handler is None:
            result = ToolResult.failure("Tool not registered.", "handler_not_found", name=name)
            return self._as_tool_message(call_id, name, result)

        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                raise TypeError("Tool arguments must decode to a JSON object.")
            result = await asyncio.wait_for(
                handler(context, **args),
                timeout=self.timeout_seconds,
            )
            if not isinstance(result, ToolResult):
                raise TypeError("Tool handler must return ToolResult.")
            result.name = result.name or name
            if isinstance(result.data, dict) and result.data.get("skip_reply"):
                context.metadata["skip_reply"] = True
        except json.JSONDecodeError as exc:
            if finish_reason == "length":
                msg = f"Arguments truncated at token limit ({exc}); retry with concise arguments."
            else:
                msg = f"Invalid JSON arguments ({exc}); retry with a valid JSON object."
            result = ToolResult.failure(msg, "invalid_json", name=name)
        except TypeError as exc:
            result = ToolResult.failure(
                f"Invalid tool arguments: {exc}",
                "invalid_arguments",
                name=name,
            )
        except TimeoutError:
            result = ToolResult.failure(
                f"Tool timed out after {self.timeout_seconds:.1f}s.",
                "timeout",
                name=name,
            )
        except Exception as exc:
            result = ToolResult.failure(
                f"Tool execution failed: {exc}",
                "execution_failed",
                name=name,
            )

        return self._as_tool_message(call_id, name, result)

    async def execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        context: ToolContext,
        *,
        finish_reason: str = "",
        view: Any = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for call in tool_calls:
            results.append(
                await self.execute_tool_call(
                    call,
                    context,
                    finish_reason=finish_reason,
                    view=view,
                )
            )
        return results

    @staticmethod
    def _as_tool_message(call_id: str, name: str, result: ToolResult) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": result.to_message_content(),
        }


__all__ = [
    "FunctionRegistry",
    "SKIP_REPLY_SENTINEL",
    "ToolCallManager",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
]
