# FrostCore

**连续独一的第一人称记忆即个体。**

FrostCore 是一个面向社区的通用拟人化 Agent 微内核 SDK（Python 3.11+，`asyncio` 原生），将「长期连续的第一人称记忆、生物节律与上下文洁癖」从具体聊天平台和重型 AI 依赖中解耦出来。

- **零重依赖默认安装**：核心仅依赖 `pydantic` 与 `aiohttp`，内置零重依赖 SQLite FTS5（中英文检索）与分会话 JSONL 历史。
- **便携个体种子 (`FrostSeed`)**：只要 `my_bot.seed/` 目录不丢失，就能在任何平台、机器与外周工具体系下重建完整的她；同进程与跨进程双层排他锁严防记忆分裂。
- **告别全局单例（支持单进程多 Bot）**：全显式依赖注入；同一个进程可运行多个 `FrostApp`（各自绑定独立的 `FrostSeed`）。
- **可组合消息流与渐进式聚焦**：支持任意 `InboundMiddleware` / `OutboundMiddleware` / `PromptComposer` / `FocusDomain` / `Mode` / `RecallPlugin`。
- **可观测与只读边界**：内置脱敏结构化 `EventSink`、独立线程 `/health` 探针、`FROSTCORE_DRY_RUN=1` 试启动与只读人格快照校验。

## 安装

```bash
pip install -e .
# 如需开发测试依赖
pip install -e ".[dev]"
```

## 5 分钟 Quickstart

```python
import asyncio
from frostcore import (
    CLIAdapter,
    FrostApp,
    FrostSeed,
    OpenAICompatLLMClient,
    SimplePromptComposer,
)

async def main() -> None:
    seed = FrostSeed("./my_bot.seed")
    llm = OpenAICompatLLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o-mini",
    )
    app = FrostApp(
        app_id="demo",
        seed=seed,
        llm=llm,
        adapters=[CLIAdapter()],
        prompt_composer=SimplePromptComposer("你是一个温柔、有自己生活节奏的伙伴。"),
    )
    async with app:
        await app.run_until_stopped()

if __name__ == "__main__":
    asyncio.run(main())
```

更多示例见 [`examples/`](./examples/)（含单进程多 Bot 装配、自定义中间件/聚焦域与只读容器部署模板）。
