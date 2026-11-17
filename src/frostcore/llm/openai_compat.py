"""Pure Python aiohttp OpenAI-compatible LLM client with zero global state."""
from __future__ import annotations

import copy
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from frostcore.contracts.ports import LLMClient, LLMRequest, LLMResponse

_GEMINI_UNSUPPORTED_PARAMS = (
    "frequency_penalty",
    "presence_penalty",
    "logit_bias",
)

_TRANSIENT_CONN_ERRORS = (
    TimeoutError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
)


def _normalize_base_url(base_url: str) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    if cleaned.endswith("/chat/completions"):
        cleaned = cleaned[: -len("/chat/completions")]
    return cleaned


def _is_gemini_endpoint(model: str, base_url: str) -> bool:
    blob = f"{model} {base_url}".lower()
    return (
        "generativelanguage.googleapis.com" in blob
        or "gemini" in blob
        or "gemma-" in blob
    )


def sanitize_payload(
    model: str,
    payload: dict[str, Any],
    base_url: str = "",
) -> dict[str, Any]:
    """Strip provider-incompatible keys before sending OpenAI-compat payloads."""
    cleaned = dict(payload)
    if "vl" in model.lower():
        cleaned.pop("response_format", None)
    if _is_gemini_endpoint(model, base_url):
        for key in _GEMINI_UNSUPPORTED_PARAMS:
            cleaned.pop(key, None)
    return cleaned


@dataclass(frozen=True)
class LLMRoute:
    """Single OpenAI-compatible route configuration."""

    base_url: str
    api_key: str
    model: str
    extra_headers: dict[str, str] = field(default_factory=dict)


class OpenAICompatLLMClient:
    """Instance-scoped async OpenAI-compatible client using aiohttp.

    Maintains its own ClientSession and backup circuit-breaker state so
    multiple FrostApp instances in one process never share mutable globals.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        backup_route: LLMRoute | None = None,
        timeout_seconds: float = 60.0,
        recovery_seconds: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.primary = LLMRoute(
            base_url=_normalize_base_url(base_url),
            api_key=api_key,
            model=model,
            extra_headers=dict(extra_headers or {}),
        )
        self.backup = (
            LLMRoute(
                base_url=_normalize_base_url(backup_route.base_url),
                api_key=backup_route.api_key,
                model=backup_route.model,
                extra_headers=dict(backup_route.extra_headers),
            )
            if backup_route is not None
            else None
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.recovery_seconds = max(1.0, float(recovery_seconds))

        self._session: aiohttp.ClientSession | None = None
        self._degraded = False
        self._last_fail_time = 0.0

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def reset_circuit(self) -> None:
        self._degraded = False
        self._last_fail_time = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=10.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def reset_session(self) -> None:
        old, self._session = self._session, None
        if old is not None and not old.closed:
            await old.close()

    async def close(self) -> None:
        await self.reset_session()

    def _select_route(self) -> LLMRoute:
        if self.backup is None or not self._degraded:
            return self.primary
        if (time.monotonic() - self._last_fail_time) >= self.recovery_seconds:
            self._degraded = False
            return self.primary
        return self.backup

    @staticmethod
    def _build_payload(route: LLMRoute, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": route.model,
            "messages": copy.deepcopy(request.messages),
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = copy.deepcopy(request.tools)
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        payload.update(request.extra)
        return sanitize_payload(route.model, payload, route.base_url)

    async def _post_route(self, route: LLMRoute, request: LLMRequest) -> LLMResponse:
        endpoint = f"{route.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {route.api_key}",
            "Content-Type": "application/json",
            **route.extra_headers,
        }
        payload = self._build_payload(route, request)

        for attempt in range(2):
            session = await self._get_session()
            try:
                async with session.post(endpoint, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"LLM HTTP {resp.status}: {body[:400]}")
                    data = await resp.json()
                    choice = (data.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    return LLMResponse(
                        content=str(msg.get("content") or ""),
                        tool_calls=list(msg.get("tool_calls") or []),
                        finish_reason=str(choice.get("finish_reason") or "stop"),
                        raw=dict(data),
                    )
            except _TRANSIENT_CONN_ERRORS:
                await self.reset_session()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    async def complete(self, request: LLMRequest) -> LLMResponse:
        route = self._select_route()
        try:
            return await self._post_route(route, request)
        except Exception:
            if route is self.primary and self.backup is not None:
                self._degraded = True
                self._last_fail_time = time.monotonic()
                return await self._post_route(self.backup, request)
            raise


class LLMDiarySummarizer:
    """Default DiarySummarizer backed by any LLMClient."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        instruction: str = (
            "Summarize the following conversation into a concise, warm first-person diary entry. "
            "Focus on experiences, feelings, and relationships; omit technical formatting."
        ),
        max_tokens: int = 1024,
    ) -> None:
        self.llm = llm
        self.instruction = instruction
        self.max_tokens = max_tokens

    async def summarize(self, chat_id: str, dialogue: Sequence[dict[str, Any]]) -> str:
        lines = [
            f"{m.get('role', 'user')}: {m.get('content', '')}"
            for m in dialogue
        ]
        transcript = "\n".join(lines)
        resp = await self.llm.complete(
            LLMRequest(
                messages=[
                    {"role": "system", "content": self.instruction},
                    {"role": "user", "content": f"[chat={chat_id}]\n{transcript}"},
                ],
                temperature=0.6,
                max_tokens=self.max_tokens,
            )
        )
        return resp.content.strip()


__all__ = [
    "LLMDiarySummarizer",
    "LLMRoute",
    "OpenAICompatLLMClient",
    "sanitize_payload",
]
