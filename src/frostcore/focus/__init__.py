"""FocusPort abstractions for progressive tool disclosure and clean history folding."""
from frostcore.focus.domain import FocusDomain
from frostcore.focus.manager import (
    DEFAULT_MAX_IDLE_TURNS,
    FocusExit,
    FocusForbiddenError,
    FocusManager,
    FocusState,
)
from frostcore.focus.rewrite import (
    build_summary_message,
    is_domain_work_message,
    is_focus_summary_message,
    message_focus_domain,
    rewrite_in_memory_messages,
    rewrite_persistent_session,
)
from frostcore.focus.tools import focus_finish_spec, focus_start_spec
from frostcore.focus.view import ToolView

__all__ = [
    "DEFAULT_MAX_IDLE_TURNS",
    "FocusDomain",
    "FocusExit",
    "FocusForbiddenError",
    "FocusManager",
    "FocusState",
    "ToolView",
    "build_summary_message",
    "focus_finish_spec",
    "focus_start_spec",
    "is_domain_work_message",
    "is_focus_summary_message",
    "message_focus_domain",
    "rewrite_in_memory_messages",
    "rewrite_persistent_session",
]
