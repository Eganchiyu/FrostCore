"""Policy-driven reply decision engine."""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from frostcore.contracts.events import InboundEvent
from frostcore.contracts.policy import ChatPolicy, DecisionContext, DecisionFactor, RecallItem
from frostcore.rhythm.bioclock import ConstantClock, TimeCurve
from frostcore.rhythm.state import ChatRhythm, RhythmStateStore


@dataclass(frozen=True)
class DecisionResult:
    """Outcome and score breakdown of reply willingness evaluation."""

    should_reply: bool
    score: float
    threshold: float
    reason: str
    breakdown: dict[str, float] = field(default_factory=dict)


class DecisionEngine:
    """Evaluates whether the companion should reply given ChatPolicy, BioClock, and factors."""

    def __init__(
        self,
        clock: TimeCurve | None = None,
        rhythm: RhythmStateStore | None = None,
        factors: Sequence[DecisionFactor] = (),
        *,
        reply_threshold: float = 50.0,
        min_active_energy: float = 15.0,
        high_energy_shortcut: float = 85.0,
        recall_hormone_gain: float = 80.0,
    ) -> None:
        self.clock: TimeCurve = clock or ConstantClock(1.0)
        self.rhythm = rhythm or RhythmStateStore()
        self.factors: list[DecisionFactor] = list(factors)
        self.reply_threshold = float(reply_threshold)
        self.min_active_energy = float(min_active_energy)
        self.high_energy_shortcut = float(high_energy_shortcut)
        self.recall_hormone_gain = float(recall_hormone_gain)

    def _score_state(
        self,
        chat_id: str,
        events: Sequence[InboundEvent],
        pol: ChatPolicy,
        ts: float,
        *,
        energy: float,
        clock_weight: float,
        desire: float,
        hormone: float,
        snap: ChatRhythm,
        recall_items: Sequence[RecallItem],
    ) -> DecisionResult:
        if pol.force_reply:
            return DecisionResult(
                should_reply=True,
                score=100.0,
                threshold=self.reply_threshold,
                reason="force_reply",
                breakdown={"energy": energy, "desire": desire, "clock_weight": clock_weight},
            )

        if energy >= self.high_energy_shortcut:
            return DecisionResult(
                should_reply=True,
                score=energy,
                threshold=self.reply_threshold,
                reason="high_energy",
                breakdown={"energy": energy, "desire": desire, "clock_weight": clock_weight},
            )

        if energy < self.min_active_energy and pol.speaker_weight <= 1.0:
            return DecisionResult(
                should_reply=False,
                score=energy,
                threshold=self.reply_threshold,
                reason="low_energy",
                breakdown={"energy": energy, "desire": desire, "clock_weight": clock_weight},
            )

        speaker_bonus = (pol.speaker_weight - 1.0) * 20.0
        base_score = energy * 0.4 + desire * 0.2 + hormone * 0.2 + speaker_bonus

        ctx = DecisionContext(
            chat_id=str(chat_id),
            policy=pol,
            energy=energy,
            activity=snap.activity,
            desire=desire,
            clock_weight=clock_weight,
            recall_items=tuple(recall_items),
            events=tuple(events),
            now=ts,
        )
        factor_sum = sum(float(factor.score(ctx)) for factor in self.factors)
        total_score = base_score + factor_sum
        will_reply = total_score >= self.reply_threshold
        return DecisionResult(
            should_reply=will_reply,
            score=round(total_score, 2),
            threshold=self.reply_threshold,
            reason="scored_pass" if will_reply else "scored_skip",
            breakdown={
                "energy": round(energy, 2),
                "desire": round(desire, 2),
                "hormone": round(hormone, 2),
                "clock_weight": round(clock_weight, 3),
                "speaker_bonus": round(speaker_bonus, 2),
                "factors": round(factor_sum, 2),
            },
        )

    def evaluate(
        self,
        chat_id: str,
        events: Sequence[InboundEvent],
        *,
        policy: ChatPolicy | None = None,
        recall_items: Sequence[RecallItem] = (),
        now: float | None = None,
    ) -> DecisionResult:
        pol = policy or ChatPolicy()
        ts = time.time() if now is None else float(now)

        if pol.dormant:
            return DecisionResult(
                should_reply=False,
                score=0.0,
                threshold=self.reply_threshold,
                reason="dormant",
            )

        energy = self.rhythm.update_energy(chat_id, now=ts)
        clock_weight = float(self.clock(ts))
        desire = self.rhythm.compute_desire(chat_id, clock_weight, now=ts)
        top_recall = max((item.score for item in recall_items), default=0.0)
        hormone = self.rhythm.update_hormone(chat_id, top_recall, gain=self.recall_hormone_gain)
        snap = self.rhythm.snapshot(chat_id, now=ts)
        return self._score_state(
            chat_id,
            events,
            pol,
            ts,
            energy=energy,
            clock_weight=clock_weight,
            desire=desire,
            hormone=hormone,
            snap=snap,
            recall_items=recall_items,
        )

    async def a_evaluate(
        self,
        chat_id: str,
        events: Sequence[InboundEvent],
        *,
        policy: ChatPolicy | None = None,
        recall_items: Sequence[RecallItem] = (),
        now: float | None = None,
    ) -> DecisionResult:
        """Non-blocking async evaluation using async FrostSeed rhythm persistence."""
        pol = policy or ChatPolicy()
        ts = time.time() if now is None else float(now)

        if pol.dormant:
            return DecisionResult(
                should_reply=False,
                score=0.0,
                threshold=self.reply_threshold,
                reason="dormant",
            )

        energy = await self.rhythm.a_update_energy(chat_id, now=ts)
        clock_weight = float(self.clock(ts))
        desire = await self.rhythm.a_compute_desire(chat_id, clock_weight, now=ts)
        top_recall = max((item.score for item in recall_items), default=0.0)
        hormone = await self.rhythm.a_update_hormone(
            chat_id,
            top_recall,
            gain=self.recall_hormone_gain,
        )
        snap = await self.rhythm.a_snapshot(chat_id, now=ts)
        return self._score_state(
            chat_id,
            events,
            pol,
            ts,
            energy=energy,
            clock_weight=clock_weight,
            desire=desire,
            hormone=hormone,
            snap=snap,
            recall_items=recall_items,
        )


__all__ = ["DecisionEngine", "DecisionResult"]
