"""Offline tests for pure-Python aiohttp OpenAICompatLLMClient."""
from __future__ import annotations

from typing import Any

from aiohttp import web

from frostcore.contracts import LLMRequest
from frostcore.llm import LLMRoute, OpenAICompatLLMClient, sanitize_payload


def test_sanitize_payload_strips_gemini_and_vl_incompatible_fields() -> None:
    payload = {
        "model": "gemini-2.5-flash",
        "messages": [],
        "frequency_penalty": 0.2,
        "presence_penalty": 0.1,
        "logit_bias": {},
        "response_format": {"type": "json_object"},
    }
    cleaned = sanitize_payload("gemini-2.5-flash", payload, "https://api.example.com/v1")
    assert "frequency_penalty" not in cleaned
    assert "presence_penalty" not in cleaned
    assert "logit_bias" not in cleaned
    assert "response_format" in cleaned

    vl_cleaned = sanitize_payload("qwen-vl-max", payload)
    assert "response_format" not in vl_cleaned


async def test_openai_compat_client_circuit_breaker_and_instance_isolation() -> None:
    primary_calls: list[dict[str, Any]] = []
    backup_calls: list[dict[str, Any]] = []
    fail_primary = True

    async def handle_primary(request: web.Request) -> web.Response:
        body = await request.json()
        primary_calls.append(body)
        if fail_primary:
            return web.Response(status=503, text="upstream down")
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {"content": "from primary", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    async def handle_backup(request: web.Request) -> web.Response:
        body = await request.json()
        backup_calls.append(body)
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {"content": "from backup", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    app_p = web.Application()
    app_p.router.add_post("/v1/chat/completions", handle_primary)
    runner_p = web.AppRunner(app_p)
    await runner_p.setup()
    site_p = web.TCPSite(runner_p, "127.0.0.1", 0)
    await site_p.start()
    port_p = site_p._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    app_b = web.Application()
    app_b.router.add_post("/v1/chat/completions", handle_backup)
    runner_b = web.AppRunner(app_b)
    await runner_b.setup()
    site_b = web.TCPSite(runner_b, "127.0.0.1", 0)
    await site_b.start()
    port_b = site_b._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    client_a = OpenAICompatLLMClient(
        base_url=f"http://127.0.0.1:{port_p}/v1",
        api_key="sk-a",
        model="gpt-4o-mini",
        backup_route=LLMRoute(
            base_url=f"http://127.0.0.1:{port_b}/v1",
            api_key="sk-b",
            model="backup-model",
        ),
        recovery_seconds=60.0,
    )
    client_b = OpenAICompatLLMClient(
        base_url=f"http://127.0.0.1:{port_p}/v1",
        api_key="sk-other",
        model="gpt-4o-mini",
    )

    try:
        # Primary fails -> client_a trips circuit and succeeds via backup
        resp1 = await client_a.complete(LLMRequest(messages=[{"role": "user", "content": "1"}]))
        assert resp1.content == "from backup"
        assert client_a.is_degraded is True
        # Instance isolation: client_b circuit state is untouched
        assert client_b.is_degraded is False

        # Subsequent call on degraded client_a goes directly to backup
        resp2 = await client_a.complete(LLMRequest(messages=[{"role": "user", "content": "2"}]))
        assert resp2.content == "from backup"
        assert len(primary_calls) == 1
        assert len(backup_calls) == 2

        # Reset circuit and restore primary
        fail_primary = False
        client_a.reset_circuit()
        resp3 = await client_a.complete(LLMRequest(messages=[{"role": "user", "content": "3"}]))
        assert resp3.content == "from primary"
    finally:
        await client_a.close()
        await client_b.close()
        await runner_p.cleanup()
        await runner_b.cleanup()
