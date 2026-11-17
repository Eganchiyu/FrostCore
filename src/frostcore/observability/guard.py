"""Read-only persona snapshot and integrity guard contracts."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from frostcore.contracts.probe import EventSink, StructuredEvent


class PersonaTamperedError(RuntimeError):
    """Raised when an immutable persona/system prompt snapshot changes unexpectedly."""


@dataclass(frozen=True)
class ReadOnlyPersonaSnapshot:
    """Immutable SHA-256 fingerprint of a host's core persona prompt."""

    app_id: str
    persona_text: str
    digest: str

    @classmethod
    def from_text(cls, app_id: str, persona_text: str) -> ReadOnlyPersonaSnapshot:
        digest = hashlib.sha256(persona_text.encode("utf-8")).hexdigest()
        return cls(app_id=app_id, persona_text=persona_text, digest=digest)

    def _emit_tampered(self, actual_digest: str, sink: EventSink | None) -> None:
        if sink is not None:
            sink.emit(
                StructuredEvent(
                    app_id=self.app_id,
                    kind="persona.tampered",
                    payload={
                        "expected_digest": self.digest,
                        "actual_digest": actual_digest,
                    },
                )
            )

    def verify(self, current_text: str, *, sink: EventSink | None = None) -> None:
        """Verify exact equality between `current_text` and the locked persona snapshot."""
        expected = hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest()
        current_digest = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
        if expected != self.digest or current_digest != self.digest:
            self._emit_tampered(current_digest, sink)
            raise PersonaTamperedError(
                f"Persona integrity check failed for app={self.app_id}"
            )

    def verify_composed(
        self,
        system_prompt: str,
        *,
        persona_block: str | None = None,
        sink: EventSink | None = None,
    ) -> None:
        """Verify composed prompt integrity while allowing dynamic memory/time slots.

        If `persona_block` is provided by the PromptComposer, verify its exact SHA-256
        against the locked persona. Otherwise verify that the immutable persona anchor
        is preserved verbatim inside `system_prompt`.
        """
        if persona_block is not None:
            self.verify(persona_block, sink=sink)
            return

        expected = hashlib.sha256(self.persona_text.encode("utf-8")).hexdigest()
        if expected != self.digest or self.persona_text not in system_prompt:
            actual_digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
            self._emit_tampered(actual_digest, sink)
            raise PersonaTamperedError(
                f"Persona anchor missing or tampered in system_prompt for app={self.app_id}"
            )


__all__ = ["PersonaTamperedError", "ReadOnlyPersonaSnapshot"]
