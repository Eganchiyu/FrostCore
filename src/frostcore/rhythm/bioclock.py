"""Circadian bio-clock curves: default constant 1.0 and optional Gaussian preset."""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable
from dataclasses import dataclass

TimeCurve = Callable[[float], float]


@dataclass(frozen=True)
class ConstantClock:
    """Default flat time curve returning 1.0 so new bots stay responsive 24/7."""

    value: float = 1.0

    def __call__(self, ts: float) -> float:
        return self.value


@dataclass(frozen=True)
class GaussianPeak:
    """Single Gaussian activity peak around hour `mu` (0..24)."""

    mu: float
    sigma: float
    amplitude: float

    def evaluate(self, hour: float) -> float:
        return self.amplitude * math.exp(-((hour - self.mu) ** 2) / (2.0 * self.sigma**2))


@dataclass(frozen=True)
class GaussianClockPreset:
    """Optional circadian curve with night sleep floor and daytime Gaussian peaks."""

    tz_offset_hours: float = 8.0
    sleep_floor: float = 0.25
    day_base: float = 0.9
    min_weight: float = 0.2
    max_weight: float = 1.5
    peaks: tuple[GaussianPeak, ...] = (
        GaussianPeak(mu=8.0, sigma=0.6, amplitude=0.5),
        GaussianPeak(mu=12.8, sigma=0.8, amplitude=0.4),
        GaussianPeak(mu=20.0, sigma=1.5, amplitude=0.4),
    )

    def __call__(self, ts: float) -> float:
        tz = datetime.timezone(datetime.timedelta(hours=self.tz_offset_hours))
        dt = datetime.datetime.fromtimestamp(ts, tz=tz)
        t = dt.hour + dt.minute / 60.0 + dt.second / 3600.0

        if 0.0 <= t < 1.0:
            base = 0.7 - t * (0.7 - self.sleep_floor)
        elif 1.0 <= t < 7.0:
            base = self.sleep_floor
        elif 7.0 <= t < 7.8:
            base = self.sleep_floor + ((t - 7.0) / 0.8) * (0.7 - self.sleep_floor)
        elif t >= 23.8:
            base = self.day_base - (t - 23.8) * 0.8
        else:
            base = self.day_base

        weight = base + sum(p.evaluate(t) for p in self.peaks)
        return max(self.min_weight, min(weight, self.max_weight))


__all__ = ["ConstantClock", "GaussianClockPreset", "GaussianPeak", "TimeCurve"]
