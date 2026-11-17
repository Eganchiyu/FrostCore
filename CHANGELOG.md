# Changelog

All notable changes to FrostCore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-17

### Added
- Initial Clean-Room M1+M2 MVP of the FrostCore microkernel SDK.
- Stable `frostcore.contracts` protocols and models (`IncomingEvent`, `SystemInjectedEvent`, `ChatPolicy`, `TurnInput`/`TurnOutput`, `ChannelAdapter`, `LLMClient`, `RecallPlugin`, `PromptComposer`, middlewares, `EventSink`, `HealthReporter`).
- `FrostSeed` portable identity directory (`seed.lock` + `seed.sqlite` WAL single-writer + `history/<chat_id>.jsonl`) with dual-layer single-instance guard (in-process registry + OS `flock` + `instance_lease`).
- Explicit dependency-injected `FrostApp` supporting multi-bot single-process hosting without global singletons.
- Deterministic `TurnEvaluator` (`F`), `FocusManager`/`FocusPort`/`ToolView`, `ModeRouter`, `BioClock`/`GaussianClockPreset`, `DiaryEngine` (watermark 2-phase lockless slice), CJK-friendly SQLite FTS5 recall, `OpenAICompatLLMClient`, `SessionPipeline`, `HealthProbeServer`, `CLIAdapter`, `frostcore.testing`, and read-only container deployment examples.
