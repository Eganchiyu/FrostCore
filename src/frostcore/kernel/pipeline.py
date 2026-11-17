"""Per-chat serialized SessionPipeline with middleware chains and debounce."""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from frostcore.contracts.events import (
    DeliveryReceipt,
    InboundEvent,
    IncomingEvent,
    OutboundMessage,
    SystemInjectedEvent,
)
from frostcore.contracts.policy import ChatPolicy, PolicyResolver
from frostcore.contracts.ports import (
    ComposeContext,
    HistoryStore,
    InboundMiddleware,
    OutboundMiddleware,
    PromptComposer,
)
from frostcore.contracts.probe import EventSink, StructuredEvent
from frostcore.contracts.turn import TurnInput, TurnOutput
from frostcore.kernel.composer import format_inbound_batch
from frostcore.kernel.evaluator import TurnEvaluator
from frostcore.kernel.toolchain import SKIP_REPLY_SENTINEL
from frostcore.memory.dialogue_filter import dialogue_length
from frostcore.memory.diary import DiaryEngine
from frostcore.memory.recall import RecallPipeline
from frostcore.modes.router import ROUTE_FROZEN, ROUTE_MODE, FrozenChat, ModeRouter
from frostcore.observability.guard import ReadOnlyPersonaSnapshot
from frostcore.rhythm.decision import DecisionEngine
from frostcore.rhythm.state import RhythmStateStore


class _DefaultPolicyResolver:
    def resolve(self, chat_id: str, events: Sequence[InboundEvent]) -> ChatPolicy:
        return ChatPolicy()


class SessionPipeline:
    """Coordinates inbound middlewares, mode routing, debounce, recall, decision, F, and diary."""

    def __init__(
        self,
        *,
        app_id: str,
        history: HistoryStore,
        evaluator: TurnEvaluator,
        prompt_composer: PromptComposer,
        decision_engine: DecisionEngine,
        rhythm: RhythmStateStore,
        mode_router: ModeRouter,
        recall_pipeline: RecallPipeline,
        send_callback: Callable[[str, OutboundMessage], Awaitable[DeliveryReceipt]],
        policy_resolver: PolicyResolver | None = None,
        inbound_middlewares: Sequence[InboundMiddleware] = (),
        outbound_middlewares: Sequence[OutboundMiddleware] = (),
        diary_engine: DiaryEngine | None = None,
        persona_guard: ReadOnlyPersonaSnapshot | None = None,
        event_sink: EventSink | None = None,
        now_fn: Callable[[], float] | None = None,
        rng_seed_base: int = 0,
        max_rounds: int = 4,
    ) -> None:
        self.app_id = app_id
        self.history = history
        self.evaluator = evaluator
        self.prompt_composer = prompt_composer
        self.decision_engine = decision_engine
        self.rhythm = rhythm
        self.mode_router = mode_router
        self.recall_pipeline = recall_pipeline
        self._send_callback = send_callback
        self.policy_resolver: PolicyResolver = policy_resolver or _DefaultPolicyResolver()
        self.inbound_middlewares = list(inbound_middlewares)
        self.outbound_middlewares = list(outbound_middlewares)
        self.diary_engine = diary_engine
        self.persona_guard = persona_guard
        self.event_sink = event_sink
        self._now_fn = now_fn or time.time
        self._rng_seed_base = rng_seed_base
        self.max_rounds = max(1, int(max_rounds))

        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._buffers: dict[str, list[InboundEvent]] = {}
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight_tasks: set[asyncio.Task[Any]] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_adapter_by_chat: dict[str, str] = {}
        self._turn_seq = 0
        self._closed = False

        self.mode_router.set_catchup_handler(self._handle_mode_catchup)

    def lock_for(self, chat_id: str) -> asyncio.Lock:
        cid = str(chat_id)
        lock = self._chat_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[cid] = lock
        return lock

    def preferred_adapter(self, chat_id: str) -> str:
        return self._last_adapter_by_chat.get(str(chat_id), "")

    def _emit(self, kind: str, chat_id: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink.emit(
                StructuredEvent(app_id=self.app_id, kind=kind, chat_id=chat_id, payload=payload)
            )

    async def _resolve_policy(self, chat_id: str, events: Sequence[InboundEvent]) -> ChatPolicy:
        result = self.policy_resolver.resolve(chat_id, events)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _handle_mode_catchup(self, frozen: FrozenChat) -> None:
        event = self.mode_router.build_catchup_event(frozen)
        await self.enqueue(event)

    async def enqueue(self, event: InboundEvent, *, adapter_name: str = "") -> None:
        if self._closed:
            return
        if adapter_name and not event.is_system_injected:
            self._last_adapter_by_chat[str(event.chat_id)] = adapter_name

        async def _terminal(ev: InboundEvent) -> None:
            await self._after_inbound_middlewares(ev)

        chain = _terminal
        for mw in reversed(self.inbound_middlewares):
            next_fn = chain

            async def _wrapped(
                ev: InboundEvent,
                current_mw: InboundMiddleware = mw,
                nxt: Callable[[InboundEvent], Awaitable[None]] = next_fn,
            ) -> None:
                await current_mw.process_inbound(ev, nxt)

            chain = _wrapped

        await chain(event)

    async def _after_inbound_middlewares(self, event: InboundEvent) -> None:
        cid = str(event.chat_id)
        action, _ = self.mode_router.route(cid)
        if action == ROUTE_MODE:
            if event.is_system_injected:
                if self.mode_router.deliver_inner_event(cid, event):
                    return
            else:
                self.mode_router.deliver_input(cid, event)
                return
        elif action == ROUTE_FROZEN:
            self.mode_router.mark_frozen(cid, event)
            return

        self._buffers.setdefault(cid, []).append(event)
        policy = await self._resolve_policy(cid, [event])

        existing = self._debounce_tasks.get(cid)
        if existing is not None and not existing.done():
            existing.cancel()

        delay = (
            0.0
            if (policy.force_reply or event.is_system_injected)
            else max(0.0, float(policy.debounce_seconds))
        )

        async def _debounced() -> None:
            current = asyncio.current_task()
            try:
                if delay > 0.0:
                    await asyncio.sleep(delay)
                if self._debounce_tasks.get(cid) is current:
                    self._debounce_tasks.pop(cid, None)
                await self._run_chat_turn(cid)
            except asyncio.CancelledError:
                return
            finally:
                if self._debounce_tasks.get(cid) is current:
                    self._debounce_tasks.pop(cid, None)
                if current is not None:
                    self._inflight_tasks.discard(current)

        task = asyncio.create_task(_debounced())
        self._debounce_tasks[cid] = task
        self._inflight_tasks.add(task)

    async def _run_chat_turn(self, chat_id: str) -> None:
        cid = str(chat_id)
        async with self.lock_for(cid):
            events = tuple(self._buffers.pop(cid, ()))
            if not events:
                return

            policy = await self._resolve_policy(cid, events)
            if policy.dormant:
                self._emit("pipeline.dormant_skipped", cid, {"event_count": len(events)})
                return

            now = float(self._now_fn())
            for ev in events:
                if isinstance(ev, IncomingEvent) and not ev.is_bot:
                    self.rhythm.boost_activity(cid, now=now)

            entity_ids = [
                ev.sender.entity_id
                for ev in events
                if isinstance(ev, IncomingEvent) and ev.sender.entity_id
            ]
            query_text = " ".join(ev.content for ev in events if ev.content).strip()
            memories = tuple(
                await self.recall_pipeline.retrieve(
                    query_text,
                    chat_id=cid,
                    entity_ids=entity_ids,
                    top_k=5,
                )
            )

            if any(
                isinstance(ev, SystemInjectedEvent) and ev.system_kind == "catchup"
                for ev in events
            ):
                policy = policy.model_copy(update={"force_reply": True})

            decision = self.decision_engine.evaluate(
                cid,
                events,
                policy=policy,
                recall_items=memories,
                now=now,
            )
            self._emit(
                "pipeline.decision",
                cid,
                {
                    "should_reply": decision.should_reply,
                    "score": decision.score,
                    "reason": decision.reason,
                },
            )

            batch_text = format_inbound_batch(events)
            only_system = all(ev.is_system_injected for ev in events)
            if not decision.should_reply:
                if batch_text and not only_system:
                    self.history.append_message(cid, "user", batch_text)
                return

            session = self.history.get_session(cid)
            snap = self.rhythm.snapshot(cid, now=now)
            clock_weight = float(self.decision_engine.clock(now))
            composed = await self.prompt_composer.compose(
                ComposeContext(
                    chat_id=cid,
                    events=events,
                    memories=memories,
                    session=session,
                    policy=policy,
                    energy=snap.energy,
                    activity=snap.activity,
                    clock_weight=clock_weight,
                )
            )

            if self.persona_guard is not None:
                self.persona_guard.verify(composed.system_prompt, sink=self.event_sink)

            if batch_text:
                extra_msg: dict[str, Any] = {}
                if only_system:
                    extra_msg["is_system_injected"] = True
                if entity_ids:
                    extra_msg["entity_id"] = entity_ids[0]
                self.history.append_message(cid, "user", batch_text, **extra_msg)

            self._turn_seq += 1
            turn_input = TurnInput(
                chat_id=cid,
                events=events,
                memories=memories,
                messages=composed.messages,
                system_prompt=composed.system_prompt,
                now=now,
                rng_seed=self._rng_seed_base + self._turn_seq,
                max_rounds=self.max_rounds,
                extra=composed.extra,
            )
            turn_output = await self.evaluator.evaluate(turn_input)

            if (
                turn_output.outcome == "skip"
                or turn_output.reply == SKIP_REPLY_SENTINEL
                or not turn_output.reply.strip()
            ):
                return

            reply_to = events[-1].event_id if events else None
            outbound = OutboundMessage(
                chat_id=cid,
                text=turn_output.reply,
                reply_to_event_id=reply_to,
            )
            receipt = await self._send_through_middlewares(outbound, turn_output)
            if receipt.ok:
                self.history.append_message(
                    cid,
                    "assistant",
                    turn_output.raw_reply or turn_output.reply,
                )
                self.rhythm.consume_energy(cid, now=now)

            if (
                self.diary_engine is not None
                and not only_system
                and dialogue_length(self.history.get_session(cid))
                >= self.diary_engine.min_dialogue_turns
            ):
                diary_task = asyncio.create_task(self.diary_engine.consolidate(cid))
                self._background_tasks.add(diary_task)
                diary_task.add_done_callback(self._background_tasks.discard)

    async def _send_through_middlewares(
        self,
        message: OutboundMessage,
        turn_output: TurnOutput,
    ) -> DeliveryReceipt:
        async def _terminal(msg: OutboundMessage) -> DeliveryReceipt:
            return await self._send_callback(msg.chat_id, msg)

        chain = _terminal
        for mw in reversed(self.outbound_middlewares):
            next_fn = chain

            async def _wrapped(
                msg: OutboundMessage,
                current_mw: OutboundMiddleware = mw,
                nxt: Callable[[OutboundMessage], Awaitable[DeliveryReceipt]] = next_fn,
            ) -> DeliveryReceipt:
                return await current_mw.process_outbound(msg, turn_output, nxt)

            chain = _wrapped

        return await chain(message)

    async def drain(self) -> None:
        """Await all currently pending debounce, active turn, and background diary tasks."""
        while True:
            debounce = [t for t in self._debounce_tasks.values() if not t.done()]
            inflight = [t for t in self._inflight_tasks if not t.done()]
            bg = [t for t in self._background_tasks if not t.done()]
            if not debounce and not inflight and not bg:
                break
            await asyncio.gather(*debounce, *inflight, *bg, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        all_tasks = [
            *self._debounce_tasks.values(),
            *self._inflight_tasks,
            *self._background_tasks,
        ]
        for task in all_tasks:
            if not task.done():
                task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._debounce_tasks.clear()
        self._inflight_tasks.clear()
        self._background_tasks.clear()


__all__ = ["SessionPipeline"]
