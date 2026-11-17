"""Example: running two distinct FrostApp identities inside a single process."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from frostcore import (
    ConstantClock,
    EntityRef,
    FrostApp,
    FrostSeed,
    GaussianClockPreset,
    HealthProbeServer,
    IncomingEvent,
    LLMResponse,
    MemoryEventSink,
)
from frostcore.testing import FakeLLMClient, MemoryChannelAdapter


async def run_demo(base_dir: Path | None = None) -> dict[str, str]:
    owned_tmp: tempfile.TemporaryDirectory[str] | None = None
    if base_dir is None:
        owned_tmp = tempfile.TemporaryDirectory()
        base_dir = Path(owned_tmp.name)

    try:
        probe = HealthProbeServer(host="127.0.0.1", port=0)
        probe.start()
        sink = MemoryEventSink()

        adapter_a = MemoryChannelAdapter("chan-a")
        adapter_b = MemoryChannelAdapter("chan-b")

        app_a = FrostApp(
            app_id="bot-alpha",
            seed=FrostSeed(base_dir / "alpha.seed"),
            llm=FakeLLMClient([LLMResponse(content="Alpha checking in!")]),
            adapters=[adapter_a],
            persona="You are Alpha, always awake and upbeat.",
            bio_clock=ConstantClock(1.0),
            event_sink=sink,
            health_probe=probe,
        )
        app_b = FrostApp(
            app_id="bot-beta",
            seed=FrostSeed(base_dir / "beta.seed"),
            llm=FakeLLMClient([LLMResponse(content="Beta here, following circadian rhythm.")]),
            adapters=[adapter_b],
            persona="You are Beta, gentle and circadian-paced.",
            bio_clock=GaussianClockPreset(),
            event_sink=sink,
            health_probe=probe,
        )

        async with app_a, app_b:
            await adapter_a.inject(
                IncomingEvent(
                    chat_id="room-1",
                    sender=EntityRef(entity_id="u1", display_name="User"),
                    content="Morning Alpha",
                )
            )
            await adapter_b.inject(
                IncomingEvent(
                    chat_id="room-1",
                    sender=EntityRef(entity_id="u1", display_name="User"),
                    content="Morning Beta",
                )
            )
            await asyncio.gather(app_a.pipeline.drain(), app_b.pipeline.drain())
            snap = probe.snapshot()
            assert snap["ready"] is True
            assert set(snap["apps"].keys()) == {"bot-alpha", "bot-beta"}

        probe.stop()
        return {
            "alpha": adapter_a.sent_messages[-1].text,
            "beta": adapter_b.sent_messages[-1].text,
        }
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


if __name__ == "__main__":
    print(asyncio.run(run_demo()))
