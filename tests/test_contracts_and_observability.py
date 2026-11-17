"""Tests for FrostCore public contracts and observability primitives."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from frostcore.contracts import (
    AppHealthSnapshot,
    ChatPolicy,
    EntityRef,
    IncomingEvent,
    StructuredEvent,
    SystemInjectedEvent,
    TurnInput,
    TurnOutput,
)
from frostcore.observability import (
    CompositeEventSink,
    HealthProbeServer,
    JsonlEventSink,
    MemoryEventSink,
    PersonaTamperedError,
    ReadOnlyPersonaSnapshot,
    is_dry_run,
    safe_truncate,
)


class _FakeReporter:
    def __init__(self, app_id: str, ready: bool) -> None:
        self.app_id = app_id
        self.ready = ready

    def health_snapshot(self) -> AppHealthSnapshot:
        return AppHealthSnapshot(app_id=self.app_id, ready=self.ready, epoch=1)


def _http_get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def _http_post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_event_models_and_system_flag() -> None:
    user = EntityRef(entity_id="u1", display_name="Alice", platform="cli")
    inc = IncomingEvent(chat_id="c1", sender=user, content="hello")
    sys_ev = SystemInjectedEvent(chat_id="c1", system_kind="catchup", content="missed 2 msgs")
    assert inc.is_system_injected is False
    assert sys_ev.is_system_injected is True
    assert ChatPolicy().allow_skip is True
    t_in = TurnInput(chat_id="c1", now=123.0, rng_seed=42)
    t_out = TurnOutput(reply="hi", raw_reply="hi")
    assert t_in.rng_seed == 42
    assert t_out.outcome == "reply"


def test_event_sinks_redact_secrets_and_media(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "events.jsonl"
    mem_sink = MemoryEventSink()
    file_sink = JsonlEventSink(jsonl_path)
    sink = CompositeEventSink([mem_sink, file_sink])

    sink.emit(
        StructuredEvent(
            app_id="bot-1",
            kind="llm.request",
            chat_id="c1",
            payload={
                "api_key": "sk-secret-123",
                "nested": {"authorization": "Bearer abc"},
                "img": "data:image/png;base64,AAAA",
                "ok": "visible",
            },
        )
    )

    events = mem_sink.events(kind="llm.request")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["api_key"] == "***REDACTED***"
    assert payload["nested"]["authorization"] == "***REDACTED***"
    assert payload["img"] == "<image_omitted>"
    assert payload["ok"] == "visible"

    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "sk-secret-123" not in lines[0]


def test_health_probe_aggregates_multi_bot_and_dry_run(monkeypatch) -> None:
    monkeypatch.setenv("FROSTCORE_DRY_RUN", "1")
    assert is_dry_run() is True

    probe = HealthProbeServer(host="127.0.0.1", port=0)
    port = probe.start()
    try:
        url = f"http://127.0.0.1:{port}/health"
        code, body = _http_get(url)
        assert code == 503
        assert body["ready"] is False
        assert body["dry_run"] is True

        rep_a = _FakeReporter("bot-a", True)
        rep_b = _FakeReporter("bot-b", False)
        probe.register("bot-a", rep_a)
        probe.register("bot-b", rep_b)

        code, body = _http_get(url)
        assert code == 503
        assert body["apps"]["bot-a"]["ready"] is True
        assert body["apps"]["bot-b"]["ready"] is False

        rep_b.ready = True
        code, body = _http_get(url)
        assert code == 200
        assert body["ready"] is True

        probe.set_notifier(lambda data: (True, f"ack:{data.get('msg')}"))
        ncode, nbody = _http_post(f"http://127.0.0.1:{port}/notify", {"msg": "restart"})
        assert ncode == 200
        assert nbody == {"ok": True, "detail": "ack:restart"}
    finally:
        probe.stop()


def test_read_only_persona_guard() -> None:
    sink = MemoryEventSink()
    snap = ReadOnlyPersonaSnapshot.from_text("bot-1", "You are Frost.")
    snap.verify("You are Frost.", sink=sink)
    assert sink.events() == []

    # Dynamic memory/time slots must NOT cause false positives in verify_composed
    snap.verify_composed(
        "You are Frost.\n\nRelevant past memories:\n- met Alice",
        persona_block="You are Frost.",
        sink=sink,
    )
    snap.verify_composed(
        "[2026-11-17] You are Frost.\n\nContext...",
        persona_block=None,
        sink=sink,
    )
    assert sink.events() == []

    try:
        snap.verify("Modified persona", sink=sink)
        raise AssertionError("Expected PersonaTamperedError")
    except PersonaTamperedError:
        pass

    try:
        snap.verify_composed("Completely replaced prompt", persona_block=None, sink=sink)
        raise AssertionError("Expected PersonaTamperedError on missing anchor")
    except PersonaTamperedError:
        pass

    assert len(sink.events(kind="persona.tampered")) == 2


def test_safe_truncate_preserves_zwj_emoji_and_combining_clusters() -> None:
    family = "Hi 👨‍👩‍👧‍👦!"
    # Cutting inside the family ZWJ sequence must step back before the cluster
    assert safe_truncate(family, 6) == "Hi "
    assert safe_truncate(family, len(family)) == family

    accented = "cafe\u0301 test"
    assert safe_truncate(accented, 4) == "caf"
    assert safe_truncate("hello", 0) == ""
