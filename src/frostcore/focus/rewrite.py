"""Pure rewrite functions for collapsing focus work traces into a single user summary."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from frostcore.focus.domain import FocusDomain


def message_focus_domain(
    msg: dict[str, Any],
    domain_of_tool: Callable[[str], FocusDomain | None] | None = None,
) -> str | None:
    """Identify the focus domain owning a message, or None if not focus work."""
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in ("tool", "assistant", "user"):
        return None
    if role == "assistant" and not msg.get("is_tool_thought"):
        return None
    if role == "user" and not msg.get("is_focus_ephemeral"):
        return None

    fw = msg.get("focus_work")
    if isinstance(fw, str) and fw:
        return fw

    if role == "tool" and domain_of_tool is not None:
        domain = domain_of_tool(str(msg.get("name") or ""))
        if domain is not None:
            return domain.name
    return None


def is_domain_work_message(msg: dict[str, Any], domain: FocusDomain) -> bool:
    """Return True if msg is ephemeral work belonging to `domain`."""
    if not isinstance(msg, dict):
        return False
    role = msg.get("role")
    matches_flag = msg.get("focus_work") == domain.name
    if role == "tool":
        return matches_flag or (msg.get("name") in domain.work_tools)
    if role == "assistant":
        return bool(msg.get("is_tool_thought") and matches_flag)
    if role == "user":
        return bool(msg.get("is_focus_ephemeral") and matches_flag)
    return False


def is_focus_summary_message(msg: dict[str, Any], domain_name: str | None = None) -> bool:
    """Return True if msg is a focus exit summary message."""
    if not isinstance(msg, dict) or not msg.get("is_focus_summary"):
        return False
    if domain_name is None:
        return True
    return msg.get("focus_domain") == domain_name


def build_summary_message(domain: FocusDomain, summary_text: str) -> dict[str, Any]:
    """Construct the persisted role='user' focus summary record."""
    return {
        "role": "user",
        "content": f"{domain.effective_summary_prefix}{summary_text}",
        "is_focus_summary": True,
        "focus_domain": domain.name,
    }


def rewrite_persistent_session(
    session: list[dict[str, Any]],
    domain: FocusDomain,
    summary_text: str,
) -> list[dict[str, Any]]:
    """Strip domain work messages from session and append a role='user' summary."""
    filtered = [msg for msg in session if not is_domain_work_message(msg, domain)]
    filtered.append(build_summary_message(domain, summary_text))
    return filtered


def rewrite_in_memory_messages(
    tool_messages: list[dict[str, Any]],
    domain: FocusDomain,
    summary_text: str,
) -> None:
    """In-place rewrite of in-turn tool_messages preserving OpenAI tool_calls pairing."""
    cleaned: list[dict[str, Any]] = []
    domain_call_ids: set[str] = set()
    work_tools = domain.work_tools

    for msg in tool_messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            calls = msg.get("tool_calls") or []
            remaining_calls: list[dict[str, Any]] = []
            for call in calls:
                fn_name = (call.get("function") or {}).get("name") or ""
                call_id = call.get("id") or ""
                if fn_name in work_tools:
                    if call_id:
                        domain_call_ids.add(str(call_id))
                else:
                    remaining_calls.append(call)
            if remaining_calls:
                updated = dict(msg)
                updated["tool_calls"] = remaining_calls
                cleaned.append(updated)
            continue

        if role == "tool":
            if (
                msg.get("name") in work_tools
                or msg.get("tool_call_id") in domain_call_ids
                or is_domain_work_message(msg, domain)
            ):
                continue
            cleaned.append(msg)
            continue

        if is_domain_work_message(msg, domain):
            continue

        cleaned.append(msg)

    cleaned.append(
        {
            "role": "user",
            "content": (
                f"{domain.effective_summary_prefix}{summary_text}\n"
                f"{domain.effective_exit_hint}"
            ),
        }
    )
    tool_messages[:] = cleaned


__all__ = [
    "build_summary_message",
    "is_domain_work_message",
    "is_focus_summary_message",
    "message_focus_domain",
    "rewrite_in_memory_messages",
    "rewrite_persistent_session",
]
