"""FrostSeed: portable single-identity memory directory (SQLite WAL + per-chat JSONL)."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from frostcore.memory.history import JsonlHistoryStore
from frostcore.memory.lock import SeedLock, SeedLockedError

T = TypeVar("T")

_SCHEMA_VERSION = 1


def tokenize_for_fts(text: str) -> str:
    """Zero-dependency tokenizer producing ASCII words + CJK unigrams and bigrams."""
    import re

    tokens: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", text):
        chunk = match.group(0)
        if chunk[0].isascii():
            tokens.append(chunk.lower())
        else:
            chars = list(chunk)
            tokens.extend(chars)
            for idx in range(len(chars) - 1):
                tokens.append(chars[idx] + chars[idx + 1])
    return " ".join(tokens)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class DiaryRecord:
    diary_id: str
    chat_id: str
    entity_ids: tuple[str, ...]
    content: str
    created_at: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    display_name: str
    aliases: tuple[str, ...]
    impression: str
    affinity: float
    updated_at: float


@dataclass(frozen=True)
class RhythmSnapshot:
    chat_id: str
    energy: float
    activity: float
    desire: float
    last_active_at: float
    updated_at: float


class FrostSeed:
    """Portable identity seed backed by seed.lock + seed.sqlite + history/*.jsonl."""

    def __init__(
        self,
        seed_dir: Path | str,
        *,
        instance_id: str | None = None,
        lease_ttl_seconds: float = 60.0,
    ) -> None:
        self.seed_dir = Path(seed_dir)
        self.seed_dir.mkdir(parents=True, exist_ok=True)
        self.instance_id = instance_id or uuid.uuid4().hex
        self.lease_ttl_seconds = max(5.0, float(lease_ttl_seconds))
        self.host = socket.gethostname()
        self.pid = os.getpid()

        self._lock = SeedLock(self.seed_dir / "seed.lock")
        self._lock.acquire()

        self.db_path = self.seed_dir / "seed.sqlite"
        self.history = JsonlHistoryStore(self.seed_dir / "history")
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"frostseed-{self.instance_id[:8]}",
        )
        self._conn: sqlite3.Connection | None = None
        self._epoch = 0
        self._closed = False

        try:
            self._epoch = self._call_sync(self._init_and_claim_lease)
        except Exception:
            self.close()
            raise

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _call_sync(self, fn: Callable[[], T]) -> T:
        if self._closed:
            raise RuntimeError("FrostSeed is already closed")
        return self._executor.submit(fn).result()

    async def _call_async(self, fn: Callable[[], T]) -> T:
        if self._closed:
            raise RuntimeError("FrostSeed is already closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def _init_and_claim_lease(self) -> int:
        conn = self._connect()
        now = time.time()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seed_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instance_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                instance_id TEXT NOT NULL,
                host TEXT NOT NULL,
                pid INTEGER NOT NULL,
                epoch INTEGER NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS diaries (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                entity_ids_json TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_diaries_chat_time
                ON diaries(chat_id, created_at DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS diaries_fts USING fts5(
                diary_id UNINDEXED,
                chat_id UNINDEXED,
                tokens,
                tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                impression TEXT NOT NULL,
                affinity REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS long_term_notes (
                key TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rhythm_snapshots (
                chat_id TEXT PRIMARY KEY,
                energy REAL NOT NULL,
                activity REAL NOT NULL,
                desire REAL NOT NULL,
                last_active_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                active_focus_domain TEXT,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_focus
                ON chat_sessions(active_focus_domain)
                WHERE active_focus_domain IS NOT NULL;
            """
        )

        row = conn.execute("SELECT * FROM instance_lease WHERE id = 1").fetchone()
        next_epoch = 1
        if row is not None:
            active = bool(row["active"])
            same_inst = row["instance_id"] == self.instance_id
            age = now - float(row["heartbeat_at"])
            same_host = row["host"] == self.host
            pid_alive = _pid_is_alive(int(row["pid"])) if same_host else True
            if active and not same_inst and age < self.lease_ttl_seconds and pid_alive:
                raise SeedLockedError(
                    f"Active FrostSeed lease held by instance={row['instance_id']} "
                    f"host={row['host']} pid={row['pid']} epoch={row['epoch']}"
                )
            next_epoch = int(row["epoch"]) + 1

        conn.execute(
            """
            INSERT INTO instance_lease (
                id, instance_id, host, pid, epoch, started_at, heartbeat_at, active
            ) VALUES (1, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                instance_id = excluded.instance_id,
                host = excluded.host,
                pid = excluded.pid,
                epoch = excluded.epoch,
                started_at = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at,
                active = 1
            """,
            (self.instance_id, self.host, self.pid, next_epoch, now, now),
        )
        conn.execute(
            """
            INSERT INTO seed_meta (key, value, updated_at) VALUES ('schema_version', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (str(_SCHEMA_VERSION), now),
        )
        conn.commit()
        return next_epoch

    def touch_lease(self) -> None:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                "UPDATE instance_lease SET heartbeat_at = ? WHERE id = 1 AND instance_id = ?",
                (now, self.instance_id),
            )
            conn.commit()

        self._call_sync(_op)

    async def a_touch_lease(self) -> None:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                "UPDATE instance_lease SET heartbeat_at = ? WHERE id = 1 AND instance_id = ?",
                (now, self.instance_id),
            )
            conn.commit()

        await self._call_async(_op)

    # ------------------------------------------------------------ Diaries (append-only)
    def _build_diary_inserter(
        self,
        *,
        chat_id: str,
        content: str,
        entity_ids: Sequence[str],
        metadata: dict[str, Any] | None,
        created_at: float | None,
        diary_id: str | None,
    ) -> Callable[[], str]:
        did = diary_id or uuid.uuid4().hex
        ts = time.time() if created_at is None else float(created_at)
        clean_content = str(content).strip()
        entities_json = json.dumps(list(entity_ids), ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        fts_tokens = tokenize_for_fts(clean_content)

        def _op() -> str:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO diaries (
                    id, chat_id, entity_ids_json, content, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (did, str(chat_id), entities_json, clean_content, ts, meta_json),
            )
            conn.execute(
                "INSERT INTO diaries_fts (diary_id, chat_id, tokens) VALUES (?, ?, ?)",
                (did, str(chat_id), fts_tokens),
            )
            conn.commit()
            return did

        return _op

    def append_diary(
        self,
        *,
        chat_id: str,
        content: str,
        entity_ids: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        created_at: float | None = None,
        diary_id: str | None = None,
    ) -> str:
        return self._call_sync(
            self._build_diary_inserter(
                chat_id=chat_id,
                content=content,
                entity_ids=entity_ids,
                metadata=metadata,
                created_at=created_at,
                diary_id=diary_id,
            )
        )

    async def a_append_diary(
        self,
        *,
        chat_id: str,
        content: str,
        entity_ids: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        created_at: float | None = None,
        diary_id: str | None = None,
    ) -> str:
        return await self._call_async(
            self._build_diary_inserter(
                chat_id=chat_id,
                content=content,
                entity_ids=entity_ids,
                metadata=metadata,
                created_at=created_at,
                diary_id=diary_id,
            )
        )

    def list_diaries(
        self,
        *,
        chat_id: str | None = None,
        limit: int = 50,
    ) -> list[DiaryRecord]:
        def _op() -> list[DiaryRecord]:
            conn = self._connect()
            if chat_id is not None:
                rows = conn.execute(
                    "SELECT * FROM diaries WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(chat_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM diaries ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_diary(r) for r in rows]

        return self._call_sync(_op)

    def search_diaries_fts(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[tuple[DiaryRecord, float]]:
        tokens = tokenize_for_fts(query).split()
        if not tokens:
            return []
        match_expr = " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))

        def _op() -> list[tuple[DiaryRecord, float]]:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT d.*, -bm25(diaries_fts) AS fts_score
                FROM diaries_fts
                JOIN diaries d ON d.id = diaries_fts.diary_id
                WHERE diaries_fts MATCH ?
                ORDER BY fts_score DESC
                LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
            return [(self._row_to_diary(r), float(r["fts_score"])) for r in rows]

        return self._call_sync(_op)

    async def a_search_diaries_fts(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[tuple[DiaryRecord, float]]:
        tokens = tokenize_for_fts(query).split()
        if not tokens:
            return []
        match_expr = " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))

        def _op() -> list[tuple[DiaryRecord, float]]:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT d.*, -bm25(diaries_fts) AS fts_score
                FROM diaries_fts
                JOIN diaries d ON d.id = diaries_fts.diary_id
                WHERE diaries_fts MATCH ?
                ORDER BY fts_score DESC
                LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
            return [(self._row_to_diary(r), float(r["fts_score"])) for r in rows]

        return await self._call_async(_op)

    @staticmethod
    def _row_to_diary(row: sqlite3.Row) -> DiaryRecord:
        return DiaryRecord(
            diary_id=str(row["id"]),
            chat_id=str(row["chat_id"]),
            entity_ids=tuple(json.loads(row["entity_ids_json"] or "[]")),
            content=str(row["content"]),
            created_at=float(row["created_at"]),
            metadata=dict(json.loads(row["metadata_json"] or "{}")),
        )

    # ------------------------------------------------------------ Entities, Notes & Rhythm
    def _build_upsert_entity_op(
        self,
        entity_id: str,
        display_name: str,
        aliases: Sequence[str],
        impression: str,
        affinity: float,
    ) -> Callable[[], None]:
        now = time.time()
        aliases_json = json.dumps(list(dict.fromkeys(aliases)), ensure_ascii=False)

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO entities (
                    entity_id, display_name, aliases_json, impression, affinity, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    aliases_json = excluded.aliases_json,
                    impression = excluded.impression,
                    affinity = excluded.affinity,
                    updated_at = excluded.updated_at
                """,
                (entity_id, display_name, aliases_json, impression, float(affinity), now),
            )
            conn.commit()

        return _op

    def upsert_entity(
        self,
        entity_id: str,
        display_name: str,
        *,
        aliases: Sequence[str] = (),
        impression: str = "",
        affinity: float = 0.0,
    ) -> None:
        self._call_sync(
            self._build_upsert_entity_op(entity_id, display_name, aliases, impression, affinity)
        )

    async def a_upsert_entity(
        self,
        entity_id: str,
        display_name: str,
        *,
        aliases: Sequence[str] = (),
        impression: str = "",
        affinity: float = 0.0,
    ) -> None:
        await self._call_async(
            self._build_upsert_entity_op(entity_id, display_name, aliases, impression, affinity)
        )

    def _build_get_entity_op(self, entity_id: str) -> Callable[[], EntityRecord | None]:
        def _op() -> EntityRecord | None:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
            if row is None:
                return None
            return EntityRecord(
                entity_id=str(row["entity_id"]),
                display_name=str(row["display_name"]),
                aliases=tuple(json.loads(row["aliases_json"] or "[]")),
                impression=str(row["impression"]),
                affinity=float(row["affinity"]),
                updated_at=float(row["updated_at"]),
            )

        return _op

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        return self._call_sync(self._build_get_entity_op(entity_id))

    async def a_get_entity(self, entity_id: str) -> EntityRecord | None:
        return await self._call_async(self._build_get_entity_op(entity_id))

    def _build_set_note_op(self, key: str, content: str) -> Callable[[], None]:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO long_term_notes (key, content, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (key, content, now),
            )
            conn.commit()

        return _op

    def set_note(self, key: str, content: str) -> None:
        self._call_sync(self._build_set_note_op(key, content))

    async def a_set_note(self, key: str, content: str) -> None:
        await self._call_async(self._build_set_note_op(key, content))

    def _build_get_note_op(self, key: str) -> Callable[[], str | None]:
        def _op() -> str | None:
            conn = self._connect()
            row = conn.execute(
                "SELECT content FROM long_term_notes WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["content"]) if row is not None else None

        return _op

    def get_note(self, key: str) -> str | None:
        return self._call_sync(self._build_get_note_op(key))

    async def a_get_note(self, key: str) -> str | None:
        return await self._call_async(self._build_get_note_op(key))

    def set_meta(self, key: str, value: str) -> None:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO seed_meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            conn.commit()

        self._call_sync(_op)

    def get_meta(self, key: str) -> str | None:
        def _op() -> str | None:
            conn = self._connect()
            row = conn.execute(
                "SELECT value FROM seed_meta WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row is not None else None

        return self._call_sync(_op)

    def _build_save_rhythm_op(
        self,
        chat_id: str,
        energy: float,
        activity: float,
        desire: float,
        last_active_at: float,
    ) -> Callable[[], None]:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO rhythm_snapshots (
                    chat_id, energy, activity, desire, last_active_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    energy = excluded.energy,
                    activity = excluded.activity,
                    desire = excluded.desire,
                    last_active_at = excluded.last_active_at,
                    updated_at = excluded.updated_at
                """,
                (str(chat_id), float(energy), float(activity), float(desire), last_active_at, now),
            )
            conn.commit()

        return _op

    def save_rhythm_snapshot(
        self,
        chat_id: str,
        *,
        energy: float,
        activity: float,
        desire: float,
        last_active_at: float,
    ) -> None:
        self._call_sync(
            self._build_save_rhythm_op(chat_id, energy, activity, desire, last_active_at)
        )

    async def a_save_rhythm_snapshot(
        self,
        chat_id: str,
        *,
        energy: float,
        activity: float,
        desire: float,
        last_active_at: float,
    ) -> None:
        await self._call_async(
            self._build_save_rhythm_op(chat_id, energy, activity, desire, last_active_at)
        )

    def _build_load_rhythm_op(self, chat_id: str) -> Callable[[], RhythmSnapshot | None]:
        def _op() -> RhythmSnapshot | None:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM rhythm_snapshots WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if row is None:
                return None
            return RhythmSnapshot(
                chat_id=str(row["chat_id"]),
                energy=float(row["energy"]),
                activity=float(row["activity"]),
                desire=float(row["desire"]),
                last_active_at=float(row["last_active_at"]),
                updated_at=float(row["updated_at"]),
            )

        return _op

    def load_rhythm_snapshot(self, chat_id: str) -> RhythmSnapshot | None:
        return self._call_sync(self._build_load_rhythm_op(chat_id))

    async def a_load_rhythm_snapshot(self, chat_id: str) -> RhythmSnapshot | None:
        return await self._call_async(self._build_load_rhythm_op(chat_id))

    def _build_set_chat_focus_op(
        self,
        chat_id: str,
        filename: str,
        active_focus_domain: str | None,
    ) -> Callable[[], None]:
        now = time.time()

        def _op() -> None:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, filename, active_focus_domain, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    filename = excluded.filename,
                    active_focus_domain = excluded.active_focus_domain,
                    updated_at = excluded.updated_at
                """,
                (str(chat_id), filename, active_focus_domain, now),
            )
            conn.commit()

        return _op

    def set_chat_focus_domain(
        self,
        chat_id: str,
        active_focus_domain: str | None,
        *,
        filename: str = "",
    ) -> None:
        from frostcore.memory.history import safe_chat_filename

        fname = filename or safe_chat_filename(chat_id)
        self._call_sync(self._build_set_chat_focus_op(chat_id, fname, active_focus_domain))

    async def a_set_chat_focus_domain(
        self,
        chat_id: str,
        active_focus_domain: str | None,
        *,
        filename: str = "",
    ) -> None:
        from frostcore.memory.history import safe_chat_filename

        fname = filename or safe_chat_filename(chat_id)
        await self._call_async(
            self._build_set_chat_focus_op(chat_id, fname, active_focus_domain)
        )

    def _build_list_active_focus_op(self) -> Callable[[], list[str]]:
        def _op() -> list[str]:
            conn = self._connect()
            rows = conn.execute(
                "SELECT chat_id FROM chat_sessions WHERE active_focus_domain IS NOT NULL"
            ).fetchall()
            return [str(r["chat_id"]) for r in rows]

        return _op

    def list_active_focus_chats(self) -> list[str]:
        return self._call_sync(self._build_list_active_focus_op())

    async def a_list_active_focus_chats(self) -> list[str]:
        return await self._call_async(self._build_list_active_focus_op())

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._conn is not None:
                def _release() -> None:
                    assert self._conn is not None
                    self._conn.execute(
                        """
                        UPDATE instance_lease
                        SET active = 0, heartbeat_at = ?
                        WHERE id = 1 AND instance_id = ?
                        """,
                        (time.time(), self.instance_id),
                    )
                    self._conn.commit()
                    self._conn.close()
                    self._conn = None

                self._executor.submit(_release).result(timeout=5.0)
        except Exception:
            pass
        finally:
            self._closed = True
            self._executor.shutdown(wait=True)
            self._lock.release()

    def __enter__(self) -> FrostSeed:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "DiaryRecord",
    "EntityRecord",
    "FrostSeed",
    "RhythmSnapshot",
    "tokenize_for_fts",
]
