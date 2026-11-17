"""Example: custom OutboundMiddleware, FocusDomain, and DecisionFactor."""
from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from frostcore import (
    ChatPolicy,
    DecisionContext,
    DeliveryReceipt,
    EntityRef,
    FocusDomain,
    FrostApp,
    FrostSeed,
    IncomingEvent,
    LLMResponse,
    OutboundMessage,
    ToolContext,
    ToolResult,
    ToolSpec,
    TurnOutput,
    focus_finish_spec,
    focus_start_spec,
)
from frostcore.testing import FakeLLMClient, MemoryChannelAdapter, StaticPolicyResolver

_THOUGHT_TAG_RE = re.compile(r"<thought>.*?</thought>", re.DOTALL)


class StripThoughtMiddleware:
    """Host-defined OutboundMiddleware keeping internal <thought> tags out of chat."""

    async def process_outbound(
        self,
        message: OutboundMessage,
        turn_output: TurnOutput,
        next_call: Callable[[OutboundMessage], Awaitable[DeliveryReceipt]],
    ) -> DeliveryReceipt:
        cleaned = _THOUGHT_TAG_RE.sub("", message.text).strip()
        return await next_call(message.model_copy(update={"text": cleaned}))


class QuestionBoostFactor:
    """Host-defined DecisionFactor boosting score on explicit questions."""

    def score(self, ctx: DecisionContext) -> float:
        return 15.0 if any("?" in ev.content for ev in ctx.events) else 0.0


async def run_demo(base_dir: Path | None = None) -> str:
    owned_tmp: tempfile.TemporaryDirectory[str] | None = None
    if base_dir is None:
        owned_tmp = tempfile.TemporaryDirectory()
        base_dir = Path(owned_tmp.name)

    try:
        sketch_domain = FocusDomain(
            name="sketch",
            title="Sketchpad",
            entry_tool="sketch_start",
            exit_tool="sketch_finish",
            domain_tools=frozenset({"sketch_draw"}),
        )
        adapter = MemoryChannelAdapter("demo")
        llm = FakeLLMClient(
            [
                LLMResponse(
                    content="opening sketchpad",
                    tool_calls=[
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "sketch_start", "arguments": '{"goal":"star"}'},
                        }
                    ],
                ),
                LLMResponse(
                    content="drawing star",
                    tool_calls=[
                        {
                            "id": "2",
                            "type": "function",
                            "function": {"name": "sketch_draw", "arguments": '{"shape":"star"}'},
                        }
                    ],
                ),
                LLMResponse(
                    content="done",
                    tool_calls=[
                        {
                            "id": "3",
                            "type": "function",
                            "function": {
                                "name": "sketch_finish",
                                "arguments": '{"summary":"Drew a star"}',
                            },
                        }
                    ],
                ),
                LLMResponse(content="<thought>secret</thought>Here is your star!"),
            ]
        )

        app: FrostApp

        async def _draw(ctx: ToolContext, shape: str) -> ToolResult:
            app.focus_manager.record_step(ctx.chat_id, f"draw:{shape}", domain="sketch")
            return ToolResult(success=True, content=f"drawn {shape}", name="sketch_draw")

        seed = FrostSeed(base_dir / "custom.seed")
        app = FrostApp(
            app_id="custom-bot",
            seed=seed,
            llm=llm,
            adapters=[adapter],
            policy_resolver=StaticPolicyResolver(ChatPolicy(force_reply=False, allow_skip=True)),
            decision_factors=[QuestionBoostFactor()],
            outbound_middlewares=[StripThoughtMiddleware()],
            focus_domains=[sketch_domain],
            tools=[
                ToolSpec(
                    name="sketch_draw",
                    description="Draw shape",
                    parameters={"type": "object", "properties": {"shape": {"type": "string"}}},
                    handler=_draw,
                ),
            ],
        )
        # Bind focus start/finish specs against app.focus_manager
        app.registry.register(focus_start_spec(sketch_domain, app.focus_manager))
        app.registry.register(focus_finish_spec(sketch_domain, app.focus_manager))

        async with app:
            await adapter.inject(
                IncomingEvent(
                    chat_id="g1",
                    sender=EntityRef(entity_id="u1", display_name="User"),
                    content="Can you draw a star?",
                )
            )
            await app.pipeline.drain()
            return adapter.sent_messages[-1].text
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


if __name__ == "__main__":
    print(asyncio.run(run_demo()))
