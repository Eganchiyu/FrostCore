"""Built-in interactive CLI adapter for Quickstart and local verification."""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from typing import TextIO

from frostcore.contracts.events import (
    DeliveryReceipt,
    EntityRef,
    IncomingEvent,
    OutboundMessage,
)
from frostcore.contracts.ports import InboundHandler


class CLIAdapter:
    """Minimal asyncio ChannelAdapter reading lines from stdin or a provided line iterable."""

    def __init__(
        self,
        *,
        name: str = "cli",
        chat_id: str = "local_cli",
        user_id: str = "local_user",
        user_name: str = "You",
        bot_name: str = "Frost",
        lines: Iterable[str] | None = None,
        output: TextIO | None = None,
        quit_commands: frozenset[str] = frozenset({"/quit", "/exit"}),
    ) -> None:
        self.name = name
        self.chat_id = chat_id
        self.user_id = user_id
        self.user_name = user_name
        self.bot_name = bot_name
        self._lines = lines
        self._output = output
        self.quit_commands = quit_commands
        self._running = False

    def _out(self) -> TextIO:
        return self._output if self._output is not None else sys.stdout

    async def start(self, on_event: InboundHandler) -> None:
        self._running = True
        sender = EntityRef(
            entity_id=self.user_id,
            display_name=self.user_name,
            platform="cli",
            platform_user_id=self.user_id,
        )
        if self._lines is not None:
            for raw in self._lines:
                if not self._running:
                    break
                text = raw.strip()
                if not text:
                    continue
                if text in self.quit_commands:
                    break
                await on_event(
                    IncomingEvent(
                        chat_id=self.chat_id,
                        sender=sender,
                        content=text,
                        raw_text=text,
                        source="cli",
                    )
                )
                await asyncio.sleep(0)
            self._running = False
            return

        loop = asyncio.get_running_loop()
        while self._running:
            raw = await loop.run_in_executor(None, sys.stdin.readline)
            if raw == "":
                break
            text = raw.strip()
            if not text:
                continue
            if text in self.quit_commands:
                break
            await on_event(
                IncomingEvent(
                    chat_id=self.chat_id,
                    sender=sender,
                    content=text,
                    raw_text=text,
                    source="cli",
                )
            )
        self._running = False

    async def stop(self) -> None:
        self._running = False

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        stream = self._out()
        stream.write(f"{self.bot_name}> {message.text}\n")
        stream.flush()
        return DeliveryReceipt(message_id=f"cli-{message.chat_id}", ok=True)


__all__ = ["CLIAdapter"]
