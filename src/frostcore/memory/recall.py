"""Zero-dependency FTS5 recall plugin and multi-plugin recall pipeline."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence

from frostcore.contracts.policy import RecallItem
from frostcore.contracts.ports import RecallPlugin
from frostcore.memory.seed import FrostSeed


class FTS5RecallPlugin:
    """SQLite FTS5 diary retrieval with chat, entity, and recency boosts."""

    def __init__(
        self,
        seed: FrostSeed,
        *,
        chat_boost: float = 0.15,
        entity_boost: float = 0.10,
        half_life_days: float = 180.0,
        recency_floor: float = 0.55,
    ) -> None:
        self.seed = seed
        self.chat_boost = chat_boost
        self.entity_boost = entity_boost
        self.half_life_days = max(1.0, half_life_days)
        self.recency_floor = recency_floor

    async def search(
        self,
        query: str,
        *,
        chat_id: str,
        entity_ids: Sequence[str] = (),
        top_k: int = 5,
    ) -> list[RecallItem]:
        if not query.strip() or top_k <= 0:
            return []

        raw_hits = await self.seed.a_search_diaries_fts(query, limit=max(top_k * 4, 15))
        if not raw_hits:
            return []

        max_fts = max(max(score, 0.0) for _, score in raw_hits) or 1.0
        max_boost = 1.0 + max(0.0, self.chat_boost) + max(0.0, self.entity_boost)
        target_entities = set(entity_ids)
        now = time.time()

        scored: list[RecallItem] = []
        for record, fts_score in raw_hits:
            base = max(fts_score, 0.0) / max_fts
            boost = 1.0
            if record.chat_id == str(chat_id):
                boost += self.chat_boost
            if target_entities and (set(record.entity_ids) & target_entities):
                boost += self.entity_boost

            age_days = max(0.0, (now - record.created_at) / 86400.0)
            decay = math.pow(0.5, age_days / self.half_life_days)
            recency = max(self.recency_floor, decay)
            final_score = (base * boost * recency) / max_boost

            scored.append(
                RecallItem(
                    diary_id=record.diary_id,
                    content=record.content,
                    chat_id=record.chat_id,
                    created_at=record.created_at,
                    score=round(final_score, 6),
                    source="fts5",
                    entity_ids=record.entity_ids,
                    metadata=record.metadata,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]


class RecallPipeline:
    """Runs built-in FTS5 and optional external RecallPlugins, deduplicating by diary_id."""

    def __init__(self, plugins: Sequence[RecallPlugin]) -> None:
        self.plugins: list[RecallPlugin] = list(plugins)

    async def retrieve(
        self,
        query: str,
        *,
        chat_id: str,
        entity_ids: Sequence[str] = (),
        top_k: int = 5,
    ) -> list[RecallItem]:
        if not self.plugins or not query.strip() or top_k <= 0:
            return []

        batches = await asyncio.gather(
            *(
                plugin.search(query, chat_id=chat_id, entity_ids=entity_ids, top_k=top_k)
                for plugin in self.plugins
            ),
            return_exceptions=True,
        )

        best_by_id: dict[str, RecallItem] = {}
        for batch in batches:
            if isinstance(batch, BaseException):
                continue
            for item in batch:
                prev = best_by_id.get(item.diary_id)
                if prev is None or item.score > prev.score:
                    best_by_id[item.diary_id] = item

        ranked = sorted(best_by_id.values(), key=lambda x: x.score, reverse=True)
        return ranked[:top_k]


__all__ = ["FTS5RecallPlugin", "RecallPipeline"]
