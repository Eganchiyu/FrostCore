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

    def verify(self, current_text: str, *, sink: EventSink | None = None) -> None:
        current_digest = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
        if current_digest != self.digest:
            if sink is not None:
                sink.emit(
                    StructuredEvent(
                        app_id=self.app_id,
                        kind="persona.tampered",
                        payload={
                            "expected_digest": self.digest,
                            "actual_digest": current_digest,
                        },
                    )
                )
            raise PersonaTamperedError(
                f"Persona integrity check failed for app={self.app_id}"
            )


__all__ = ["PersonaTamperedError", "ReadOnlyPersonaSnapshot"]
