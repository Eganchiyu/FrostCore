"""Tests for instantiable ModeRouter exclusivity, inner events, and catchup."""
from __future__ import annotations

from frostcore.contracts import EntityRef, IncomingEvent, SystemInjectedEvent
from frostcore.modes import Mode, ModeCapabilities, ModeRouter
from frostcore.testing import verify_mode_router_contract


class _FakeMode(Mode):
    name = "reflect"
    capabilities = ModeCapabilities(
        can_send=False,
        writes_chat_history=False,
        accepts_human_input=False,
        allow_sub_focus=False,
        triggers_diary=False,
        accepts_inner_event=True,
    )

    def __init__(self, *, chat_id: str) -> None:
        super().__init__(chat_id=chat_id)
        self.received_inner: list[SystemInjectedEvent] = []

    async def run(self) -> dict[str, str]:
        return {"status": "done"}

    def on_inner_event(self, event) -> bool:
        self.received_inner.append(event)
        return True


async def test_mode_router_contract() -> None:
    router = ModeRouter()
    await verify_mode_router_contract(router, lambda cid: _FakeMode(chat_id=cid))


def test_mode_router_inner_event_and_catchup_builder() -> None:
    router = ModeRouter()
    mode = _FakeMode(chat_id="c1")
    router.enter("c1", "reflect", instance=mode)

    sub_ev = SystemInjectedEvent(
        chat_id="c1",
        system_kind="subagent_report",
        content="worker finished",
    )
    assert router.deliver_inner_event("c1", sub_ev) is True
    assert len(mode.received_inner) == 1

    # System events must not count toward missed human catchup
    router.mark_frozen("c2", sub_ev)
    assert router.pending_catchups() == {}

    human_ev = IncomingEvent(
        chat_id="c2",
        sender=EntityRef(entity_id="u1", display_name="Alice"),
        content="anyone here?",
    )
    frozen = router.mark_frozen("c2", human_ev)
    catchup = router.build_catchup_event(frozen)
    assert catchup.system_kind == "catchup"
    assert catchup.is_system_injected is True
    assert "Alice" in catchup.content
