"""Tests for BioClock defaults, GaussianClockPreset, RhythmStateStore, and DecisionEngine."""
from __future__ import annotations

import datetime
from pathlib import Path

from frostcore.contracts import ChatPolicy, DecisionContext, EntityRef, IncomingEvent
from frostcore.memory import FrostSeed
from frostcore.rhythm import (
    ConstantClock,
    DecisionEngine,
    GaussianClockPreset,
    RhythmStateStore,
)


class _PlusTenFactor:
    def score(self, ctx: DecisionContext) -> float:
        return 10.0


def test_default_constant_clock_is_always_one() -> None:
    clock = ConstantClock()
    assert clock(0.0) == 1.0
    assert clock(1_700_000_000.0) == 1.0


def test_gaussian_clock_preset_night_vs_noon() -> None:
    clock = GaussianClockPreset(tz_offset_hours=8.0)
    tz = datetime.timezone(datetime.timedelta(hours=8.0))
    night_ts = datetime.datetime(2026, 11, 17, 3, 30, tzinfo=tz).timestamp()
    noon_ts = datetime.datetime(2026, 11, 17, 12, 48, tzinfo=tz).timestamp()
    assert clock(night_ts) < 0.4
    assert clock(noon_ts) > 1.1


def test_rhythm_persistence_and_decision_policies(tmp_path: Path) -> None:
    event = IncomingEvent(
        chat_id="c1",
        sender=EntityRef(entity_id="u1", display_name="Bob"),
        content="hi",
    )
    seed_dir = tmp_path / "rhythm.seed"
    with FrostSeed(seed_dir) as seed:
        rhythm = RhythmStateStore(seed=seed, initial_energy=50.0, cost_per_reply=10.0)
        engine = DecisionEngine(clock=ConstantClock(1.0), rhythm=rhythm)

        assert engine.evaluate("c1", [event], policy=ChatPolicy(dormant=True)).should_reply is False
        assert (
            engine.evaluate("c1", [event], policy=ChatPolicy(force_reply=True)).should_reply
            is True
        )

        # Drain energy below min_active_energy and check low-energy suppression vs VIP weight
        for _ in range(4):
            rhythm.consume_energy("c1", now=100.0)
        low_res = engine.evaluate("c1", [event], policy=ChatPolicy(), now=100.0)
        assert low_res.should_reply is False
        assert low_res.reason == "low_energy"

        vip_res = engine.evaluate(
            "c1",
            [event],
            policy=ChatPolicy(speaker_weight=3.0),
            now=100.0,
        )
        # High speaker weight bypasses low-energy gate and gains speaker bonus
        assert vip_res.reason != "low_energy"

    # Reopen seed and ensure persisted energy survived restart
    with FrostSeed(seed_dir) as reopened:
        rhythm2 = RhythmStateStore(seed=reopened, initial_energy=100.0)
        snap = rhythm2.snapshot("c1", now=100.0)
        assert snap.energy == 10.0

    # Custom DecisionFactor affects borderline score
    r3 = RhythmStateStore(initial_energy=60.0)
    eng_plain = DecisionEngine(clock=ConstantClock(1.0), rhythm=r3, reply_threshold=40.0)
    eng_boost = DecisionEngine(
        clock=ConstantClock(1.0),
        rhythm=RhythmStateStore(initial_energy=60.0),
        factors=[_PlusTenFactor()],
        reply_threshold=40.0,
    )
    plain = eng_plain.evaluate("c2", [event], now=100.0)
    boosted = eng_boost.evaluate("c2", [event], now=100.0)
    assert boosted.score == round(plain.score + 10.0, 2)
