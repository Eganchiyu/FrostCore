"""End-to-end offline tests for SessionPipeline, FrostApp multi-bot, CLI, and probes."""
from __future__ import annotations

import io
import json
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

from frostcore import (
    ChatPolicy,
    CLIAdapter,
    DeliveryReceipt,
    EntityRef,
    FocusDomain,
    FocusForbiddenError,
    FrostApp,
    FrostSeed,
    HealthProbeServer,
    IncomingEvent,
    LLMResponse,
    MemoryEventSink,
    Mode,
    ModeCapabilities,
    OutboundMessage,
    ReadOnlyPersonaSnapshot,
    TurnOutput,
)
from frostcore.testing import (
    FakeDiarySummarizer,
    FakeLLMClient,
    MemoryChannelAdapter,
    StaticPolicyResolver,
)


class _BlockWordMiddleware:
    async def process_inbound(
        self,
        event,
        next_call: Callable[[object], Awaitable[None]],
    ) -> None:
        if "BLOCKME" in event.content:
            return
        await next_call(event)


class _SuffixMiddleware:
    async def process_outbound(
        self,
        message: OutboundMessage,
        turn_output: TurnOutput,
        next_call: Callable[[OutboundMessage], Awaitable[DeliveryReceipt]],
    ) -> DeliveryReceipt:
        return await next_call(message.model_copy(update={"text": f"{message.text} [ok]"}))


class _QuietMode(Mode):
    name = "quiet"
    capabilities = ModeCapabilities(
        can_send=False,
        writes_chat_history=False,
        accepts_human_input=False,
        allow_sub_focus=False,
        triggers_diary=False,
        accepts_inner_event=True,
    )

    async def run(self) -> dict[str, str]:
        return {"status": "ok"}


async def test_multibot_pipeline_middlewares_debounce_and_health(tmp_path: Path) -> None:
    probe = HealthProbeServer(host="127.0.0.1", port=0)
    port = probe.start()
    sink = MemoryEventSink()

    ad_a = MemoryChannelAdapter("a")
    ad_b = MemoryChannelAdapter("b")
    llm_a = FakeLLMClient(
        [
            LLMResponse(content="Alpha saw both"),
            LLMResponse(content="Alpha catchup reply"),
        ]
    )
    llm_b = FakeLLMClient([LLMResponse(content="Beta reply")])

    app_a = FrostApp(
        app_id="bot-a",
        seed=FrostSeed(tmp_path / "a.seed"),
        llm=llm_a,
        adapters=[ad_a],
        policy_resolver=StaticPolicyResolver(ChatPolicy(debounce_seconds=0.05, force_reply=False)),
        inbound_middlewares=[_BlockWordMiddleware()],
        outbound_middlewares=[_SuffixMiddleware()],
        diary_summarizer=FakeDiarySummarizer("BotA Diary:"),
        diary_min_turns=4,
        diary_keep_last=2,
        event_sink=sink,
        health_probe=probe,
    )
    app_b = FrostApp(
        app_id="bot-b",
        seed=FrostSeed(tmp_path / "b.seed"),
        llm=llm_b,
        adapters=[ad_b],
        event_sink=sink,
        health_probe=probe,
    )

    try:
        async with app_a, app_b:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2.0) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["ready"] is True
                assert set(data["apps"].keys()) == {"bot-a", "bot-b"}

            user = EntityRef(entity_id="u1", display_name="Alice")
            # Blocked by InboundMiddleware
            await ad_a.inject(IncomingEvent(chat_id="g1", sender=user, content="BLOCKME"))
            # Two rapid events coalesced by debounce into one turn
            await ad_a.inject(IncomingEvent(chat_id="g1", sender=user, content="part 1"))
            await ad_a.inject(IncomingEvent(chat_id="g1", sender=user, content="part 2"))
            await app_a.pipeline.drain()

            assert len(ad_a.sent_messages) == 1
            assert ad_a.sent_messages[0].text == "Alpha saw both [ok]"
            last_req = llm_a.requests[0].messages[-1]["content"]
            assert "part 1" in last_req and "part 2" in last_req

            # Mode freeze -> human message buffered -> exit triggers catchup reply
            app_a.mode_router.enter("other", "quiet", instance=_QuietMode(chat_id="other"))
            await ad_a.inject(IncomingEvent(chat_id="g1", sender=user, content="missed while away"))
            app_a.mode_router.exit("other")
            await app_a.mode_router.dispatch_catchups()
            await app_a.pipeline.drain()
            assert len(ad_a.sent_messages) == 2
            assert ad_a.sent_messages[1].text == "Alpha catchup reply [ok]"

            # Bot B stays completely isolated on its own seed
            await ad_b.inject(IncomingEvent(chat_id="g1", sender=user, content="hello B"))
            await app_b.pipeline.drain()
            assert [m.text for m in ad_b.sent_messages] == ["Beta reply"]
            assert app_b.seed.history.get_session("g1") != app_a.seed.history.get_session("g1")
    finally:
        probe.stop()


async def test_cli_adapter_and_auto_diary_consolidation(tmp_path: Path) -> None:
    out = io.StringIO()
    cli = CLIAdapter(
        lines=["hello", "how are you?", "/quit"],
        output=out,
        bot_name="Frost",
    )
    llm = FakeLLMClient(
        [
            LLMResponse(content="hi there"),
            LLMResponse(content="doing great"),
        ]
    )
    with FrostSeed(tmp_path / "cli.seed") as seed:
        app = FrostApp(
            app_id="cli-bot",
            seed=seed,
            llm=llm,
            adapters=[cli],
            diary_summarizer=FakeDiarySummarizer("CLI Diary:"),
            diary_min_turns=4,
            diary_keep_last=2,
            close_seed_on_stop=False,
        )
        async with app:
            await app.run_until_stopped()

        text = out.getvalue()
        assert "Frost> hi there" in text
        assert "Frost> doing great" in text
        diaries = seed.list_diaries()
        assert len(diaries) == 1
        assert "CLI Diary:" in diaries[0].content


async def test_frostapp_dry_run_skips_adapters_and_marks_probe_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FROSTCORE_DRY_RUN", "1")
    probe = HealthProbeServer(host="127.0.0.1", port=0)
    probe.start()
    adapter = MemoryChannelAdapter("dry")
    sink = MemoryEventSink()
    app = FrostApp(
        app_id="dry-bot",
        seed=FrostSeed(tmp_path / "dry.seed"),
        llm=FakeLLMClient(),
        adapters=[adapter],
        health_probe=probe,
        event_sink=sink,
    )
    try:
        async with app:
            assert adapter.started is False
            snap = probe.snapshot()
            assert snap["ready"] is True
            assert snap["dry_run"] is True
            assert len(sink.events(kind="app.dry_run_ready")) == 1
    finally:
        probe.stop()


async def test_persona_guard_with_memories_and_mode_capabilities_enforced(tmp_path: Path) -> None:
    persona = "You are Frost, gentle and consistent."
    sink = MemoryEventSink()
    adapter = MemoryChannelAdapter("cap")
    domain = FocusDomain(
        name="art",
        title="Art",
        entry_tool="art_start",
        exit_tool="art_finish",
        domain_tools=frozenset({"art_draw"}),
    )

    with FrostSeed(tmp_path / "caps.seed") as seed:
        seed.append_diary(chat_id="c1", content="Alice loves green tea ice cream.")
        app = FrostApp(
            app_id="cap-bot",
            seed=seed,
            llm=FakeLLMClient(
                [
                    LLMResponse(content="Remembered!"),
                    LLMResponse(content="Should not send or persist"),
                ]
            ),
            adapters=[adapter],
            persona=persona,
            persona_guard=ReadOnlyPersonaSnapshot.from_text("cap-bot", persona),
            focus_domains=[domain],
            diary_summarizer=FakeDiarySummarizer("D:"),
            diary_min_turns=1,
            event_sink=sink,
            close_seed_on_stop=False,
        )

        async with app:
            # 1. With recalled diary memory injected, persona_guard must NOT false-positive
            await adapter.inject(
                IncomingEvent(
                    chat_id="c1",
                    sender=EntityRef(entity_id="u1", display_name="Alice"),
                    content="green tea ice cream",
                )
            )
            await app.pipeline.drain()
            assert [m.text for m in adapter.sent_messages] == ["Remembered!"]

            # 2. Enter restrictive non-chat mode and verify all 4 capability gates enforce
            restrictive = _QuietMode(chat_id="c1")
            app.mode_router.enter("c1", "quiet", instance=restrictive)

            # 2a) allow_sub_focus=False blocks FocusManager.enter
            try:
                app.focus_manager.enter("c1", "art", goal="forbidden")
                raise AssertionError("Expected FocusForbiddenError")
            except FocusForbiddenError:
                pass

            # 2b) can_send=False blocks _send_to_adapter
            receipt = await app._send_to_adapter(
                "c1",
                OutboundMessage(chat_id="c1", text="forbidden"),
            )
            assert receipt.ok is False
            assert receipt.error == "mode_forbids_send"

            # 2c) triggers_diary=False blocks DiaryEngine.consolidate
            assert app.diary_engine is not None
            assert await app.diary_engine.consolidate("c1", force=True) is None

            # 2d) writes_chat_history=False blocks SessionPipeline history mutation
            before_len = len(seed.history.get_session("c1"))
            await app.pipeline._run_chat_turn("c1")
            assert len(seed.history.get_session("c1")) == before_len

            denied = [e.payload["capability"] for e in sink.events(kind="mode.capability_denied")]
            assert {"allow_sub_focus", "can_send", "triggers_diary"}.issubset(set(denied))
