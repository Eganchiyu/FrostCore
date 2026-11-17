"""FrostApp: explicit dependency-injected microkernel container (multi-bot safe)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from frostcore.contracts.events import DeliveryReceipt, InboundEvent, OutboundMessage
from frostcore.contracts.policy import DecisionFactor, PolicyResolver
from frostcore.contracts.ports import (
    ChannelAdapter,
    DiarySummarizer,
    InboundMiddleware,
    LLMClient,
    OutboundMiddleware,
    PromptComposer,
    RecallPlugin,
)
from frostcore.contracts.probe import AppHealthSnapshot, EventSink, StructuredEvent
from frostcore.focus.domain import FocusDomain
from frostcore.focus.manager import FocusManager
from frostcore.kernel.composer import SimplePromptComposer
from frostcore.kernel.evaluator import TurnEvaluator
from frostcore.kernel.pipeline import SessionPipeline
from frostcore.kernel.pruner import ToolHistoryPruner
from frostcore.kernel.toolchain import FunctionRegistry, ToolSpec
from frostcore.llm.openai_compat import LLMDiarySummarizer
from frostcore.memory.diary import DiaryEngine
from frostcore.memory.recall import FTS5RecallPlugin, RecallPipeline
from frostcore.memory.seed import FrostSeed
from frostcore.modes.base import Mode
from frostcore.modes.router import ModeRouter
from frostcore.observability.events import NullEventSink
from frostcore.observability.guard import ReadOnlyPersonaSnapshot
from frostcore.observability.health import HealthProbeServer, is_dry_run
from frostcore.rhythm.bioclock import ConstantClock, TimeCurve
from frostcore.rhythm.decision import DecisionEngine
from frostcore.rhythm.state import RhythmStateStore


class FrostApp:
    """Top-level explicit DI container and lifecycle coordinator for one companion identity."""

    def __init__(
        self,
        *,
        app_id: str,
        seed: FrostSeed,
        llm: LLMClient,
        adapters: Sequence[ChannelAdapter] = (),
        prompt_composer: PromptComposer | None = None,
        persona: str = "You are a warm, thoughtful companion with continuous memory.",
        policy_resolver: PolicyResolver | None = None,
        bio_clock: TimeCurve | None = None,
        decision_factors: Sequence[DecisionFactor] = (),
        focus_domains: Sequence[FocusDomain] = (),
        tools: Sequence[ToolSpec] = (),
        modes: dict[str, Callable[[], Mode]] | None = None,
        inbound_middlewares: Sequence[InboundMiddleware] = (),
        outbound_middlewares: Sequence[OutboundMiddleware] = (),
        recall_plugins: Sequence[RecallPlugin] = (),
        diary_summarizer: DiarySummarizer | None = None,
        enable_diary: bool = True,
        diary_min_turns: int = 12,
        diary_keep_last: int = 10,
        prune_idle_turns: int = 2,
        max_rounds: int = 4,
        event_sink: EventSink | None = None,
        health_probe: HealthProbeServer | None = None,
        persona_guard: ReadOnlyPersonaSnapshot | None = None,
        now_fn: Callable[[], float] | None = None,
        close_seed_on_stop: bool = True,
    ) -> None:
        self.app_id = str(app_id)
        self.seed = seed
        self.llm = llm
        self.adapters: dict[str, ChannelAdapter] = {a.name: a for a in adapters}
        self._adapter_order: list[str] = [a.name for a in adapters]
        self.event_sink: EventSink = event_sink or NullEventSink()
        self.health_probe = health_probe
        self.persona_guard = persona_guard
        self.close_seed_on_stop = close_seed_on_stop

        self.focus_manager = FocusManager()
        for domain in focus_domains:
            self.focus_manager.register(domain)

        self.registry = FunctionRegistry(list(tools))
        self.mode_router = ModeRouter(event_sink=self.event_sink, app_id=self.app_id)
        for name, factory in (modes or {}).items():
            self.mode_router.register_mode(name, factory)

        self.rhythm = RhythmStateStore(seed=self.seed)
        self.bio_clock: TimeCurve = bio_clock or ConstantClock(1.0)
        self.decision_engine = DecisionEngine(
            clock=self.bio_clock,
            rhythm=self.rhythm,
            factors=decision_factors,
        )

        plugins: list[RecallPlugin] = [FTS5RecallPlugin(self.seed), *recall_plugins]
        self.recall_pipeline = RecallPipeline(plugins)
        self.pruner = ToolHistoryPruner(
            self.seed.history,
            self.focus_manager,
            idle_turns=prune_idle_turns,
        )
        self.evaluator = TurnEvaluator(
            llm=self.llm,
            registry=self.registry,
            focus_manager=self.focus_manager,
            history=self.seed.history,
            seed=self.seed,
            pruner=self.pruner,
            event_sink=self.event_sink,
            app_id=self.app_id,
        )

        self._lock_map: dict[str, asyncio.Lock] = {}

        def _lock_for(cid: str) -> asyncio.Lock:
            return self.pipeline.lock_for(cid)

        self.diary_engine: DiaryEngine | None = None
        if enable_diary:
            summarizer = diary_summarizer or LLMDiarySummarizer(self.llm)
            self.diary_engine = DiaryEngine(
                seed=self.seed,
                summarizer=summarizer,
                lock_for_chat=_lock_for,
                is_chat_focused=self.focus_manager.is_focused,
                min_dialogue_turns=diary_min_turns,
                keep_last_turns=diary_keep_last,
                event_sink=self.event_sink,
                app_id=self.app_id,
            )

        composer = prompt_composer or SimplePromptComposer(persona=persona)
        self.pipeline = SessionPipeline(
            app_id=self.app_id,
            history=self.seed.history,
            evaluator=self.evaluator,
            prompt_composer=composer,
            decision_engine=self.decision_engine,
            rhythm=self.rhythm,
            mode_router=self.mode_router,
            recall_pipeline=self.recall_pipeline,
            send_callback=self._send_to_adapter,
            policy_resolver=policy_resolver,
            inbound_middlewares=inbound_middlewares,
            outbound_middlewares=outbound_middlewares,
            diary_engine=self.diary_engine,
            persona_guard=self.persona_guard,
            event_sink=self.event_sink,
            now_fn=now_fn,
            max_rounds=max_rounds,
        )

        self._ready = False
        self._started = False
        self._dry_run = False
        self._lease_task: asyncio.Task[None] | None = None
        self._adapter_tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()

    async def _send_to_adapter(self, chat_id: str, message: OutboundMessage) -> DeliveryReceipt:
        name = self.pipeline.preferred_adapter(chat_id)
        adapter = self.adapters.get(name)
        if adapter is None and self._adapter_order:
            adapter = self.adapters[self._adapter_order[0]]
        if adapter is None:
            return DeliveryReceipt(ok=False, error="no_adapter_configured")
        return await adapter.send(message)

    def health_snapshot(self) -> AppHealthSnapshot:
        return AppHealthSnapshot(
            app_id=self.app_id,
            ready=self._ready,
            seed_path=str(self.seed.seed_dir),
            epoch=self.seed.epoch,
            details={
                "dry_run": self._dry_run,
                "adapters": list(self._adapter_order),
                "mode_idle": self.mode_router.is_idle(),
            },
        )

    async def _lease_heartbeat_loop(self) -> None:
        interval = max(1.0, self.seed.lease_ttl_seconds / 3.0)
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval)
                if not self.seed.is_closed:
                    await self.seed.a_touch_lease()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def start(self, *, dry_run: bool | None = None) -> None:
        if self._started:
            return
        self._stop_event.clear()
        self._dry_run = is_dry_run() if dry_run is None else bool(dry_run)

        if self.persona_guard is not None:
            self.persona_guard.verify(self.persona_guard.persona_text, sink=self.event_sink)

        swept = self.focus_manager.sweep_orphans(self.seed.history)
        if swept:
            self.event_sink.emit(
                StructuredEvent(
                    app_id=self.app_id,
                    kind="focus.orphans_swept",
                    payload={"count": swept},
                )
            )

        if self.health_probe is not None:
            self.health_probe.register(self.app_id, self)

        if self._dry_run:
            self._ready = True
            self._started = True
            self.event_sink.emit(
                StructuredEvent(
                    app_id=self.app_id,
                    kind="app.dry_run_ready",
                    payload={"epoch": self.seed.epoch},
                )
            )
            return

        self._lease_task = asyncio.create_task(self._lease_heartbeat_loop())
        for adapter in self.adapters.values():
            async def _on_event(
                ev: InboundEvent,
                src_name: str = adapter.name,
            ) -> None:
                await self.pipeline.enqueue(ev, adapter_name=src_name)

            task = asyncio.create_task(adapter.start(_on_event))
            self._adapter_tasks.append(task)

        # Let newly scheduled adapter.start() coroutines bind their handlers before returning
        await asyncio.sleep(0)

        self._ready = True
        self._started = True
        self.event_sink.emit(
            StructuredEvent(
                app_id=self.app_id,
                kind="app.started",
                payload={"epoch": self.seed.epoch, "adapters": list(self._adapter_order)},
            )
        )

    async def run_until_stopped(self) -> None:
        """Wait until stop() or all adapter tasks finish (e.g., CLI EOF)."""
        if self._dry_run or not self._adapter_tasks:
            return
        await asyncio.gather(*self._adapter_tasks, return_exceptions=True)
        await self.pipeline.drain()

    async def stop(self) -> None:
        self._ready = False
        self._stop_event.set()
        if self.health_probe is not None:
            self.health_probe.unregister(self.app_id)

        for adapter in self.adapters.values():
            try:
                await adapter.stop()
            except Exception:
                pass

        for task in list(self._adapter_tasks):
            if not task.done():
                task.cancel()
        if self._adapter_tasks:
            await asyncio.gather(*self._adapter_tasks, return_exceptions=True)
        self._adapter_tasks.clear()

        if self._lease_task is not None and not self._lease_task.done():
            self._lease_task.cancel()
            await asyncio.gather(self._lease_task, return_exceptions=True)
        self._lease_task = None

        await self.pipeline.close()
        close_llm = getattr(self.llm, "close", None)
        if callable(close_llm):
            res = close_llm()
            if asyncio.iscoroutine(res):
                await res

        if self.close_seed_on_stop:
            self.seed.close()

        self._started = False
        self.event_sink.emit(
            StructuredEvent(app_id=self.app_id, kind="app.stopped", payload={})
        )

    async def __aenter__(self) -> FrostApp:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()


__all__ = ["FrostApp"]
