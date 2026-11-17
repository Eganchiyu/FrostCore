"""Tests for CJK-friendly FTS5 diary recall and multi-plugin RecallPipeline."""
from __future__ import annotations

import time
from pathlib import Path

from frostcore.contracts import RecallItem
from frostcore.memory import FrostSeed, FTS5RecallPlugin, RecallPipeline


class _MockVectorRecall:
    async def search(
        self,
        query: str,
        *,
        chat_id: str,
        entity_ids: tuple[str, ...] = (),
        top_k: int = 5,
    ) -> list[RecallItem]:
        return [
            RecallItem(
                diary_id="vec-1",
                content="semantic hit from plugin",
                chat_id=chat_id,
                score=0.95,
                source="chroma",
            )
        ]


async def test_fts5_cjk_and_english_recall_ranking(tmp_path: Path) -> None:
    now = time.time()
    with FrostSeed(tmp_path / "s.seed") as seed:
        d_old = seed.append_diary(
            chat_id="other_chat",
            content="去年冬天大家一起吃了火锅，特别热闹。",
            entity_ids=["e_bob"],
            created_at=now - 365 * 86400,
        )
        d_recent_same = seed.append_diary(
            chat_id="chat_1",
            content="今晚在群里聊到了抹茶火锅和甜点，Alice 说很好吃。",
            entity_ids=["e_alice"],
            created_at=now - 3600,
        )
        seed.append_diary(
            chat_id="chat_1",
            content="调试 Python asyncio 测试用例，全部通过。",
            entity_ids=[],
            created_at=now - 600,
        )

        plugin = FTS5RecallPlugin(seed)
        hits = await plugin.search("火锅", chat_id="chat_1", entity_ids=["e_alice"], top_k=5)
        assert [h.diary_id for h in hits] == [d_recent_same, d_old]

        en_hits = await plugin.search("asyncio", chat_id="chat_1", top_k=5)
        assert len(en_hits) == 1
        assert "asyncio" in en_hits[0].content

        pipeline = RecallPipeline([plugin, _MockVectorRecall()])
        merged = await pipeline.retrieve("火锅", chat_id="chat_1", top_k=2)
        assert len(merged) == 2
        assert merged[0].diary_id == "vec-1"
        assert merged[1].diary_id == d_recent_same
