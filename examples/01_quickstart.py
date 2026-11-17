"""Quickstart example: single-identity companion on CLI.

Run live:
    FROSTCORE_BASE_URL=https://api.openai.com/v1 FROSTCORE_API_KEY=sk-... \\
    FROSTCORE_MODEL=gpt-4o-mini python examples/01_quickstart.py

Run dry-run preflight (offline):
    FROSTCORE_DRY_RUN=1 python examples/01_quickstart.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from frostcore import (
    CLIAdapter,
    FrostApp,
    FrostSeed,
    LLMClient,
    OpenAICompatLLMClient,
    SimplePromptComposer,
)
from frostcore.testing import FakeLLMClient


def build_llm() -> LLMClient:
    api_key = os.getenv("FROSTCORE_API_KEY", "").strip()
    if not api_key:
        return FakeLLMClient(default_reply="Hello! (Set FROSTCORE_API_KEY for live LLM replies.)")
    return OpenAICompatLLMClient(
        base_url=os.getenv("FROSTCORE_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        model=os.getenv("FROSTCORE_MODEL", "gpt-4o-mini"),
    )


async def main() -> None:
    seed_dir = Path(os.getenv("FROSTCORE_SEED_DIR", "./quickstart.seed"))
    seed = FrostSeed(seed_dir)
    app = FrostApp(
        app_id="quickstart-bot",
        seed=seed,
        llm=build_llm(),
        adapters=[CLIAdapter(bot_name="Frost")],
        prompt_composer=SimplePromptComposer(
            "You are Frost, a warm and attentive companion with continuous memory."
        ),
    )
    async with app:
        if app.health_snapshot().details.get("dry_run"):
            print("Dry-run preflight ok:", app.health_snapshot().model_dump())
            return
        await app.run_until_stopped()


if __name__ == "__main__":
    asyncio.run(main())
