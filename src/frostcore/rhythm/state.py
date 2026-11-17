"""Per-app rhythm state store backed by FrostSeed.rhythm_snapshots."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from frostcore.memory.seed import FrostSeed


@dataclass
class ChatRhythm:
    """In-memory biological rhythm state for a single chat_id."""

    energy: float
    activity: float
    desire: float
    topic_hormone: float
    last_energy_at: float
    last_active_at: float


class RhythmStateStore:
    """Manages energy recovery, activity decay, and desire calculation per FrostApp."""

    def __init__(
        self,
        seed: FrostSeed | None = None,
        *,
        initial_energy: float = 100.0,
        max_energy: float = 100.0,
        recovery_per_min: float = 0.4,
        cost_per_reply: float = 4.0,
        activity_sensitivity: float = 0.25,
        activity_decay: float = 0.5,
        sigmoid_alpha: float = 0.06,
        sigmoid_centre: float = 50.0,
    ) -> None:
        self.seed = seed
        self.initial_energy = float(initial_energy)
        self.max_energy = float(max_energy)
        self.recovery_per_min = float(recovery_per_min)
        self.cost_per_reply = float(cost_per_reply)
        self.activity_sensitivity = float(activity_sensitivity)
        self.activity_decay = float(activity_decay)
        self.sigmoid_alpha = float(sigmoid_alpha)
        self.sigmoid_centre = float(sigmoid_centre)
        self._states: dict[str, ChatRhythm] = {}

    def _get_or_load(self, chat_id: str, now: float) -> ChatRhythm:
        cid = str(chat_id)
        state = self._states.get(cid)
        if state is not None:
            return state

        snap = self.seed.load_rhythm_snapshot(cid) if self.seed is not None else None
        if snap is not None:
            state = ChatRhythm(
                energy=snap.energy,
                activity=snap.activity,
                desire=snap.desire,
                topic_hormone=0.0,
                last_energy_at=snap.updated_at or now,
                last_active_at=snap.last_active_at or now,
            )
        else:
            state = ChatRhythm(
                energy=self.initial_energy,
                activity=0.0,
                desire=50.0,
                topic_hormone=0.0,
                last_energy_at=now,
                last_active_at=now,
            )
        self._states[cid] = state
        return state

    def _persist(self, chat_id: str, state: ChatRhythm) -> None:
        if self.seed is None or self.seed.is_closed:
            return
        self.seed.save_rhythm_snapshot(
            chat_id,
            energy=state.energy,
            activity=state.activity,
            desire=state.desire,
            last_active_at=state.last_active_at,
        )

    def update_energy(self, chat_id: str, *, now: float | None = None) -> float:
        ts = time.time() if now is None else float(now)
        state = self._get_or_load(chat_id, ts)
        elapsed_mins = max(0.0, (ts - state.last_energy_at) / 60.0)
        state.energy = min(self.max_energy, state.energy + elapsed_mins * self.recovery_per_min)
        state.last_energy_at = ts
        return state.energy

    def consume_energy(self, chat_id: str, *, now: float | None = None) -> float:
        ts = time.time() if now is None else float(now)
        state = self._get_or_load(chat_id, ts)
        state.energy = max(0.0, state.energy - self.cost_per_reply)
        state.last_energy_at = ts
        self._persist(chat_id, state)
        return state.energy

    def boost_activity(self, chat_id: str, *, now: float | None = None) -> float:
        ts = time.time() if now is None else float(now)
        state = self._get_or_load(chat_id, ts)
        increment = (10.0 - state.activity) * self.activity_sensitivity
        state.activity = min(10.0, max(0.0, state.activity + increment))
        state.last_active_at = ts
        return state.activity

    def decay_all(self) -> None:
        for cid, state in list(self._states.items()):
            state.activity *= self.activity_decay
            if state.activity < 0.1:
                state.activity = 0.0
            self._persist(cid, state)

    def update_hormone(self, chat_id: str, recall_interest: float, gain: float = 80.0) -> float:
        ts = time.time()
        state = self._get_or_load(chat_id, ts)
        clamped = min(max(recall_interest, 0.0), 1.0)
        state.topic_hormone = min(100.0, state.topic_hormone * 0.6 + clamped * gain)
        return state.topic_hormone

    def compute_desire(
        self,
        chat_id: str,
        clock_weight: float,
        *,
        now: float | None = None,
    ) -> float:
        ts = time.time() if now is None else float(now)
        state = self._get_or_load(chat_id, ts)
        activity_level = min(state.activity / 5.0, 1.0)
        follow_desire = activity_level * 80.0 * (state.energy / max(1.0, self.max_energy))
        ice_break_desire = (
            (1.0 - activity_level)
            * 60.0
            * max(0.0, (state.energy - 60.0) / 40.0)
        )
        raw_desire = max(follow_desire, ice_break_desire) * max(0.0, clock_weight)
        normalized = 100.0 / (
            1.0 + math.exp(-self.sigmoid_alpha * (raw_desire - self.sigmoid_centre))
        )
        state.desire = round(normalized, 2)
        self._persist(chat_id, state)
        return state.desire

    def snapshot(self, chat_id: str, *, now: float | None = None) -> ChatRhythm:
        ts = time.time() if now is None else float(now)
        return self._get_or_load(chat_id, ts)


__all__ = ["ChatRhythm", "RhythmStateStore"]
