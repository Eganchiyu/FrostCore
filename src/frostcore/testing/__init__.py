"""Offline fakes and reusable contract verification helpers."""
from frostcore.testing.contracts import (
    verify_diary_concurrency_contract,
    verify_focus_domain_contract,
    verify_mode_router_contract,
    verify_seed_single_instance_contract,
    verify_turn_determinism_contract,
)
from frostcore.testing.fakes import (
    FakeClock,
    FakeDiarySummarizer,
    FakeLLMClient,
    MemoryChannelAdapter,
    StaticPolicyResolver,
)

__all__ = [
    "FakeClock",
    "FakeDiarySummarizer",
    "FakeLLMClient",
    "MemoryChannelAdapter",
    "StaticPolicyResolver",
    "verify_diary_concurrency_contract",
    "verify_focus_domain_contract",
    "verify_mode_router_contract",
    "verify_seed_single_instance_contract",
    "verify_turn_determinism_contract",
]
