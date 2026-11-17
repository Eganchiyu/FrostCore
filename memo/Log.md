# 开发日志

## 2026-11-17

### M0 · 独立仓库脚手架、公共契约与可观测底线
- 在 `/home/asumi/FrostCore` 初始化 Clean-Room 独立 Git 仓库与 Python 3.13 `.venv`，核心仅声明 `pydantic` 与 `aiohttp`，配 `py.typed` 与严格 `mypy`/`ruff` 规则。
- 完成 `frostcore.contracts` 公共契约模型与 Protocol（`IncomingEvent`、`SystemInjectedEvent`、`EntityRef`、`ChatPolicy`、`PolicyResolver`、`DecisionFactor`、`TurnInput`/`TurnOutput`、`LLMClient`、`ChannelAdapter`、`HistoryStore`、`RecallPlugin`、`PromptComposer`、中间件、`EventSink`、`HealthReporter`）。
- 完成 `frostcore.observability`：带密钥与图片脱敏的 `EventSink`（Memory/JSONL/Composite）、支持多 Bot 聚合与 `FROSTCORE_DRY_RUN` 的独立线程 HTTP `/health` 探针，以及 `ReadOnlyPersonaSnapshot` 完整性守卫。
