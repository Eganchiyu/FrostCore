# 开发日志

## 2026-11-17

### M0 · 独立仓库脚手架、公共契约与可观测底线
- 在 `/home/asumi/FrostCore` 初始化 Clean-Room 独立 Git 仓库与 Python 3.13 `.venv`，核心仅声明 `pydantic` 与 `aiohttp`，配 `py.typed` 与严格 `mypy`/`ruff` 规则。
- 完成 `frostcore.contracts` 公共契约模型与 Protocol（`IncomingEvent`、`SystemInjectedEvent`、`EntityRef`、`ChatPolicy`、`PolicyResolver`、`DecisionFactor`、`TurnInput`/`TurnOutput`、`LLMClient`、`ChannelAdapter`、`HistoryStore`、`RecallPlugin`、`PromptComposer`、中间件、`EventSink`、`HealthReporter`）。
- 完成 `frostcore.observability`：带密钥与图片脱敏的 `EventSink`（Memory/JSONL/Composite）、支持多 Bot 聚合与 `FROSTCORE_DRY_RUN` 的独立线程 HTTP `/health` 探针，以及 `ReadOnlyPersonaSnapshot` 完整性守卫。

### M1 · 种子、FocusPort、ModeRouter 与线性求值循环
- 完成 `frostcore.memory`：`SeedLock`（进程内路径集合 + OS `fcntl.flock` 双层守卫）、`FrostSeed`（SQLite WAL + `synchronous=FULL` + 单写者线程队列 + `instance_lease` 递增 `epoch`）、`JsonlHistoryStore`（分会话追加写、防目录穿越、损坏末行恢复、水位线截断）、`FTS5RecallPlugin`（纯 Python CJK unigram/bigram 分词 + 同群/实体/时间衰减重排）与 `DiaryEngine`（两阶段无长锁水位线协议）。
- 完成 `frostcore.focus`：`FocusDomain`、`ToolView`、`FocusManager`、`rewrite` 纯函数与工厂函数（彻底零 Yuki 历史字段残留）。
- 完成 `frostcore.modes`：`Mode`、`ModeCapabilities` 与可实例化 `ModeRouter`。
- 完成 `frostcore.kernel`（M1 部分）：`Toolchain`、`TurnHookChain`、`ToolHistoryPruner` 与确定性 `TurnEvaluator`（`F(TurnInput) -> TurnOutput`）。
- 完成 `frostcore.testing` 离线契约测试套件，23 项测试全部通过，`mypy --strict` 与 `ruff` 零警告。

### M2 · 生物钟、纯 Python LLM、SessionPipeline、多 Bot 装配与 Quickstart/部署模板
- 完成 `frostcore.rhythm`：`ConstantClock(1.0)` 默认常数曲线、可选 `GaussianClockPreset` 昼夜峰曲线、`RhythmStateStore`（持久化进 `FrostSeed.rhythm_snapshots`）与策略驱动 `DecisionEngine`。
- 完成 `frostcore.llm`：基于 `aiohttp` 的实例隔离 `OpenAICompatLLMClient`（独占会话与主备熔断状态，无模块级单例）及 `LLMDiarySummarizer`。
- 完成 `frostcore.kernel`（M2 部分）：`SimplePromptComposer`、全异步串联 `SessionPipeline`（入站/出站中间件链、`ModeRouter` 分流、防抖汇聚、per-chat 串行锁与 `drain()`）、显式 DI 容器 `FrostApp`（支持单进程多 Bot、`DRY_RUN` 与 `/health` 聚合）。
- 完成 `frostcore.adapters.CLIAdapter`、示例 `examples/01~03` 与只读容器部署模板 `examples/deploy/`。
- 全套 34 项离线测试、`compileall`、`mypy --strict`（46 个源文件）与 `ruff` 全部通过。
