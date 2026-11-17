# FrostCore 架构与核心不变式

`FrostCore` 是一个**微内核 + 双生命体支柱**的拟人化 Agent SDK，核心运行时仅依赖 `pydantic` 与 `aiohttp`（配合 Python 3.11+ 标准库）。本文档描述当前（`M2.5`）的真实架构、数据流与内核硬边界。

---

## 一、分层边界与包结构 (`src/frostcore/`)

| 层级 | 包路径 | 职责与稳定性 |
|---|---|---|
| **公共契约层** | `contracts/` | `Protocol` 接口与 Pydantic 模型（`IncomingEvent` / `SystemInjectedEvent`、`ChatPolicy` / `DecisionFactor`、`TurnInput` / `TurnOutput`、`ChannelAdapter`、`LLMClient`、`PromptComposer`、`InboundMiddleware`/`OutboundMiddleware`、`EventSink`、`HealthReporter`）。严格遵循 SemVer。 |
| **便携个体种子** | `memory/` | `FrostSeed`（`seed.lock` + `seed.sqlite` WAL 单写队列 + `history/<chat_id>.jsonl`）、CJK 友好 FTS5 召回、`DiaryEngine` 两阶段水位线总结协议。 |
| **微内核核心** | `kernel/`、`focus/`、`modes/` | 显式 DI 容器 `FrostApp`、`SessionPipeline`、纯求值函数 `TurnEvaluator` (`F`)、`FocusManager` / `ToolView`、`ModeRouter`、`ToolCallManager`、`ToolHistoryPruner`。 |
| **生物钟与决策** | `rhythm/` | 默认常数 `ConstantClock(1.0)`、可选 `GaussianClockPreset`、`RhythmStateStore`（异步读写 `rhythm_snapshots`）与 `DecisionEngine`。 |
| **纯 Python LLM** | `llm/` | 实例隔离的 `OpenAICompatLLMClient`（独占 `aiohttp.ClientSession` 与主备熔断状态）与 `LLMDiarySummarizer`。 |
| **可观测与守卫** | `observability/` | 脱敏 `EventSink`（`JsonlEventSink`/`MemoryEventSink`/`CompositeEventSink`）、独立线程 `/health` 探针、`FROSTCORE_DRY_RUN`、`ReadOnlyPersonaSnapshot`、`safe_truncate`。 |
| **外置适配器与测试** | `adapters/`、`testing/` | 内置 `CLIAdapter`；随包提供 `FakeLLMClient`、`MemoryChannelAdapter`、`FakeClock` 与 5 项可复用离线契约验证函数。 |

---

## 二、核心拓扑与消息流

```text
宿主进程（支持单进程显式注入多个 FrostApp，无全局单例）
  └── FrostApp(app_id, seed, llm, adapters, ...)
        │
ChannelAdapter(s) ──► InboundMiddleware 链（支持短路）
  │
  ▼  ModeRouter.route(chat_id)
  ├─ ROUTE_MODE   ──► SystemInjectedEvent 优先投递 Mode.on_inner_event()；真人消息交付 Mode.on_input()
  ├─ ROUTE_FROZEN ──► 仅入缓冲并计数 missed_events，模式退出后统一合成 catchup 事件
  └─ ROUTE_CHAT   ──► SessionPipeline (per-chat asyncio.Lock + PolicyResolver 防抖聚合)
                        ├─ 异步更新节奏 (RhythmStateStore.a_boost_activity)
                        ├─ RecallPipeline (内置 FTS5 + 外部 RecallPlugin 并行去重)
                        ├─ DecisionEngine.a_evaluate (BioClock + ChatPolicy + DecisionFactor)
                        ├─ PromptComposer.compose -> ReadOnlyPersonaSnapshot.verify_composed()
                        ├─ TurnEvaluator.evaluate(TurnInput) -> TurnOutput  [纯内存求值，零写盘]
                        ├─ 原子提交 JsonlHistoryStore (受 ModeCapabilities.writes_chat_history 门禁)
                        ├─ OutboundMiddleware 链 -> ChannelAdapter.send() (受 can_send 门禁)
                        ├─ ToolHistoryPruner.after_turn() (折叠闲置工具输出)
                        └─ DiaryEngine.consolidate() (异步两阶段水位线切片，受 triggers_diary 门禁)
```

---

## 三、五大核心不变式

### 3.1 单 Seed 独占与单进程多 Bot
- **零全局单例**：所有业务模块（`ModeRouter`、`FocusManager`、`OpenAICompatLLMClient`、`RhythmStateStore`、`ToolHistoryPruner`、`HealthProbeServer`）均按实例显式创建并注入 `FrostApp`。同一进程内可同时运行多个 `FrostApp`。
- **双层 Seed 排他锁**：同一 `FrostSeed` 目录由**进程内规范路径守卫集合 `_ACTIVE_SEED_PATHS`** 与 **OS 非阻塞文件锁 `seed.lock`（`fcntl.flock` / `msvcrt`）** 加上 SQLite `instance_lease` 共同保护；无论跨进程还是同进程重复打开同一 `FrostSeed`，一律抛出 `SeedLockedError`。

### 3.2 `FrostSeed` 混合存储与 $O(1)$ 启动扫描
- **`seed.sqlite`**（WAL + `synchronous=FULL` + 单写者串行线程池）：保存 `seed_meta`、`instance_lease`、`diaries`（只追加）、`diaries_fts`（纯 Python CJK unigram/bigram + ASCII 分词，无需 `jieba`）、`entities`、`long_term_notes`、`rhythm_snapshots` 与 `chat_sessions`。
- **`history/<safe_chat_id>.jsonl`**：按会话分片追加写入单行 JSON（含单调递增 `seq`），避免大文件全量重写；读取对损坏末行自动跳过容错。
- **$O(1)$ 孤儿清扫**：`FocusManager` 在聚焦进出时同步更新 `chat_sessions.active_focus_domain`；`FrostApp.start()` 仅查询 SQLite 中 `active_focus_domain IS NOT NULL` 的会话执行 `sweep_orphans()`，避免启动期全盘扫描大量历史文件。

### 3.3 纯函数 `F(TurnInput) -> TurnOutput` 与单一提交点
- `TurnEvaluator.evaluate(TurnInput)` 严格固定输入时间 `now` 与局部 `random.Random(rng_seed)`，**不在求值过程中直接修改 `HistoryStore`**。
- 工具循环外层 `try ... finally` 统一保证 `TurnHookChain.on_turn_end(st)` 先执行完毕后再构造 `TurnOutput`（包含 `persist_messages` 与 `focus_exits`），从而保证 `idle` 自动退出、写入闸门与确定性回放的一致性。
- `SessionPipeline` 作为唯一持久化提交点：仅当 `outcome not in ("skip", "error")` 且 `ModeCapabilities.writes_chat_history=True` 时，原子执行 Focus 历史折叠、工具结果追加与最终回复落盘。
- `ToolSpec(concurrent_safe=True)` 允许只读工具在同一批次下通过 `asyncio.gather` 并发执行；含状态/Focus 工具仍严格串行保序。

### 3.4 `ModeCapabilities` 机制强制
非聊天模式的能力声明由内核各执行出口强制校验，任一越权调用均被拦截并向 `EventSink` 发送 `mode.capability_denied` 事件：
- `can_send=False`：拦截 `SessionPipeline` 与 `FrostApp._send_to_adapter` 外发。
- `writes_chat_history=False`：拦截 `SessionPipeline` 向 `HistoryStore` 写盘。
- `allow_sub_focus=False`：拦截 `FocusManager.enter` 并抛出 `FocusForbiddenError`（常态聊天 `CHAT_CAPABILITIES` 默认 `allow_sub_focus=True`）。
- `triggers_diary=False`：拦截 `DiaryEngine.consolidate`。

### 3.5 人格快照校验与脱敏可观测
- **`ReadOnlyPersonaSnapshot`**：`SimplePromptComposer` 会在 `ComposedPrompt.persona_block` 返回未拼接记忆的原始人设块，校验其精确 SHA-256；自定义 `PromptComposer` 若只返回拼接后的 `system_prompt`（`persona_block=None`），则校验核心人格锚点原文完整存在于 `system_prompt` 中，允许安全拼接时间/记忆等动态槽位。
- **`EventSink` & `HealthProbeServer`**：所有结构化事件默认脱敏敏感键（`api_key`/`token`/`secret`/`authorization`）与 `data:image/` 载荷；`/health` 运行在独立标准库线程中，支持聚合多 `FrostApp` 就绪状态与 `FROSTCORE_DRY_RUN=1` 试启动预检。
