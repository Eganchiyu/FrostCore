# Changelog

All notable changes to FrostCore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-11-17 (M2.5 Kernel Hardening & Docs)

### Added
- `ComposedPrompt.persona_block` and `ReadOnlyPersonaSnapshot.verify_composed()` supporting dynamic memory/time slots without false-positive tampering errors.
- Async `a_*` non-blocking APIs across `FrostSeed`, `RhythmStateStore`, and `DecisionEngine` used by `SessionPipeline`.
- `chat_sessions` index table in `seed.sqlite` enabling $O(1)$ orphan focus detection in `FrostApp.start()`.
- `ToolSpec.concurrent_safe` allowing `asyncio.gather` parallel execution for safe read-only tool batches.
- Grapheme-cluster-aware `safe_truncate()` helper preventing split ZWJ emoji / combining sequences.
- Architecture and extension documentation in `docs/architecture.md` and `docs/extension-guide.md`.

### Fixed
- Enforced `ModeCapabilities` (`can_send`, `writes_chat_history`, `allow_sub_focus`, `triggers_diary`) across `FrostApp`, `SessionPipeline`, `FocusManager`, and `DiaryEngine` (and enabled `CHAT_CAPABILITIES.allow_sub_focus=True`).
- Purified `TurnEvaluator` (`F`) to avoid direct `HistoryStore` mutations mid-turn and ensured `TurnHookChain.on_turn_end()` runs before building `TurnOutput`, with atomic persistence handled by `SessionPipeline`.

## [0.1.0] - 2026-11-17

### Added
- Initial Clean-Room M1+M2 MVP of the FrostCore microkernel SDK.
- Stable `frostcore.contracts` protocols and models (`IncomingEvent`, `SystemInjectedEvent`, `ChatPolicy`, `TurnInput`/`TurnOutput`, `ChannelAdapter`, `LLMClient`, `RecallPlugin`, `PromptComposer`, middlewares, `EventSink`, `HealthReporter`).
- `FrostSeed` portable identity directory (`seed.lock` + `seed.sqlite` WAL single-writer + `history/<chat_id>.jsonl`) with dual-layer single-instance guard (in-process registry + OS `flock` + `instance_lease`).
- Explicit dependency-injected `FrostApp` supporting multi-bot single-process hosting without global singletons.
- Deterministic `TurnEvaluator` (`F`), `FocusManager`/`FocusPort`/`ToolView`, `ModeRouter`, `BioClock`/`GaussianClockPreset`, `DiaryEngine` (watermark 2-phase lockless slice), CJK-friendly SQLite FTS5 recall, `OpenAICompatLLMClient`, `SessionPipeline`, `HealthProbeServer`, `CLIAdapter`, `frostcore.testing`, and read-only container deployment examples.
