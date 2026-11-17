# FrostCore 扩展与二开指南

`FrostCore` 通过 `frostcore.contracts` 暴露全套 `@runtime_checkable Protocol` 接口，宿主与社区插件无需继承内核私有基类即可接入。

---

## 1. 自定义渠道适配器 (`ChannelAdapter`)

```python
from frostcore import DeliveryReceipt, InboundHandler, OutboundMessage

class MyPlatformAdapter:
    name = "my_platform"

    async def start(self, on_event: InboundHandler) -> None:
        # 收到平台消息后构造 IncomingEvent 并 await on_event(event)
        ...

    async def stop(self) -> None:
        ...

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        return DeliveryReceipt(message_id="msg-1", ok=True)
```

> 同一个 `FrostApp` 可挂载多个 `ChannelAdapter`（共享同一个 `FrostSeed`），跨平台联系人通过 `EntityRef.entity_id` 与 `seed.upsert_entity()` 关联。

---

## 2. 自定义消息流 (`InboundMiddleware` / `PromptComposer` / `OutboundMiddleware`)

- **`InboundMiddleware`**：用于指令拦截（如 `/sleep` 短路不调用 `await next_call(event)`）、媒体转写预取或权限打标。
- **`PromptComposer`**：全权决定系统提示词与上下文窗口布局。
  - 若搭配 `ReadOnlyPersonaSnapshot` 使用，建议将原始核心人设文本填入 `ComposedPrompt.persona_block`，其余动态时间、小本本、召回记忆放入 `system_prompt` 或消息列表；若留空 `persona_block=None`，内核将验证 `persona_text in system_prompt`。
- **`OutboundMiddleware`**：在发送前剥离自定义隐匿标签（如思考标签）、切分长消息气泡或旁路记录观测轨迹。可参考 [`examples/03_custom_middleware_and_focus.py`](../examples/03_custom_middleware_and_focus.py)。

---

## 3. 注册聚焦工作域 (`FocusDomain`)

聚焦域通过「常态仅露入口工具 → 聚焦内收敛为白名单 → 退出时写入闸门原地折叠成单条 `role="user"` 总结」保持上下文洁癖：

```python
from frostcore import FocusDomain, ToolSpec, focus_finish_spec, focus_start_spec

domain = FocusDomain(
    name="notes",
    title="Notes",
    entry_tool="notes_start",
    exit_tool="notes_finish",
    domain_tools=frozenset({"notes_read", "notes_write"}),
    idle_turns=5,
    max_rounds=16,
)
```

- 只读、无副作用的工具可在 `ToolSpec` 设置 `concurrent_safe=True`，同批多个工具调用将通过 `asyncio.gather` 并发执行；涉及状态变更或 Focus 切换的工具保持默认 `concurrent_safe=False`。

---

## 4. 策略驱动会话与意愿因子 (`PolicyResolver` / `DecisionFactor`)

内核不硬编码群聊/私聊/特权用户概念，全部交由 `PolicyResolver` 与 `DecisionFactor` 表达：

- `PolicyResolver.resolve(chat_id, events) -> ChatPolicy(dormant, debounce_seconds, force_reply, allow_skip, speaker_weight)`
- `DecisionFactor.score(ctx: DecisionContext) -> float`：可基于提问语气、被提及情况、发言者权重或反刷屏规则给出加减分。

---

## 5. 外挂非聊天模式 (`Mode` + `ModeRouter`)

接入自主独处、后台巡检等非聊天模式时：
1. 声明合理的 `ModeCapabilities`（例如 `can_send=False, writes_chat_history=False, allow_sub_focus=False, triggers_diary=False, accepts_inner_event=True`）。
2. 通过 `app.mode_router.enter(chat_id, mode_name, instance=mode)` 抢占全局唯一非聊天锁；退出后调用 `exit()` 并执行 `await app.mode_router.dispatch_catchups()` 补台。
3. 若异步后台子代理产生结果，构造 `SystemInjectedEvent(chat_id=..., system_kind="subagent_report", content=...)` 投递给 `app.pipeline.enqueue()`：系统注入事件不会计入日记对话轮次。

---

## 6. 使用离线契约测试自证

所有新增扩展均可直接调用 `frostcore.testing` 中的契约函数进行零网络自证：

```python
from frostcore.testing import (
    FakeLLMClient,
    MemoryChannelAdapter,
    verify_diary_concurrency_contract,
    verify_focus_domain_contract,
    verify_mode_router_contract,
    verify_seed_single_instance_contract,
    verify_turn_determinism_contract,
)
```
