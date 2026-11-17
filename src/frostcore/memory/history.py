"""Per-chat append-only JSONL history store with atomic compaction."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from frostcore.memory.dialogue_filter import is_dialogue_message

_SAFE_PREFIX_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_chat_filename(chat_id: str) -> str:
    """Map an arbitrary chat_id to a traversal-safe, collision-free JSONL filename."""
    cleaned = _SAFE_PREFIX_RE.sub("_", str(chat_id)).strip("._") or "chat"
    prefix = cleaned[:40]
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}.jsonl"


class JsonlHistoryStore:
    """Thread-safe per-chat JSONL history implementing the HistoryStore protocol."""

    def __init__(self, history_dir: Path | str) -> None:
        self.history_dir = Path(history_dir)
        self._lock = threading.RLock()
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._next_seq: dict[str, int] = {}
        self._chat_files: dict[str, Path] = {}
        self.corrupted_lines_skipped = 0

    def _file_for(self, chat_id: str) -> Path:
        cid = str(chat_id)
        path = self._chat_files.get(cid)
        if path is None:
            path = self.history_dir / safe_chat_filename(cid)
            self._chat_files[cid] = path
        return path

    def _ensure_loaded_locked(self, chat_id: str) -> list[dict[str, Any]]:
        cid = str(chat_id)
        if cid in self._sessions:
            return self._sessions[cid]

        path = self._file_for(cid)
        entries: list[dict[str, Any]] = []
        max_seq = 0
        if path.exists():
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for idx, line in enumerate(raw_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError:
                    # Recover gracefully if power loss truncated the final line.
                    if idx == len(raw_lines) - 1:
                        self.corrupted_lines_skipped += 1
                        continue
                    self.corrupted_lines_skipped += 1
                    continue
                if isinstance(item, dict):
                    seq = int(item.get("seq") or (max_seq + 1))
                    item["seq"] = seq
                    max_seq = max(max_seq, seq)
                    entries.append(item)

        self._sessions[cid] = entries
        self._next_seq[cid] = max_seq + 1
        return entries

    def _rewrite_file_locked(self, chat_id: str, entries: list[dict[str, Any]]) -> None:
        path = self._file_for(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for item in entries:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)

    def _append_line_locked(self, chat_id: str, item: dict[str, Any]) -> None:
        path = self._file_for(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            fh.flush()

    @staticmethod
    def _strip_internal_seq(item: dict[str, Any]) -> dict[str, Any]:
        return {
            k: copy.deepcopy(v)
            for k, v in item.items()
            if k not in ("seq", "_chat_id")
        }

    def _scan_disk_chats_locked(self) -> None:
        if not self.history_dir.exists():
            return
        for path in sorted(self.history_dir.glob("*.jsonl")):
            if path in self._chat_files.values():
                continue
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            chat_id: str | None = None
            for line in raw_lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("_chat_id"):
                    chat_id = str(item["_chat_id"])
                    break
            if chat_id is None:
                chat_id = path.stem
            self._chat_files[chat_id] = path
            self._ensure_loaded_locked(chat_id)

    def get_entries(self, chat_id: str) -> list[dict[str, Any]]:
        """Return full stored records including monotonic `seq`."""
        cid = str(chat_id)
        with self._lock:
            entries = self._ensure_loaded_locked(cid)
            return [copy.deepcopy(item) for item in entries]

    def max_seq(self, chat_id: str) -> int:
        cid = str(chat_id)
        with self._lock:
            entries = self._ensure_loaded_locked(cid)
            return int(entries[-1]["seq"]) if entries else 0

    def get_session(
        self,
        chat_id: str,
        system_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        cid = str(chat_id)
        with self._lock:
            entries = self._ensure_loaded_locked(cid)
            if system_prompt:
                if not entries:
                    self.append_message(cid, "system", system_prompt)
                elif entries[0].get("role") == "system":
                    if entries[0].get("content") != system_prompt:
                        entries[0]["content"] = system_prompt
                        self._rewrite_file_locked(cid, entries)
                else:
                    seq = self._next_seq[cid]
                    self._next_seq[cid] += 1
                    entries.insert(
                        0,
                        {
                            "seq": seq,
                            "_chat_id": cid,
                            "role": "system",
                            "content": system_prompt,
                            "ts": time.time(),
                        },
                    )
                    self._rewrite_file_locked(cid, entries)
            return [self._strip_internal_seq(item) for item in entries]

    def append_message(
        self,
        chat_id: str,
        role: str,
        content: Any,
        **extra: Any,
    ) -> int:
        cid = str(chat_id)
        with self._lock:
            entries = self._ensure_loaded_locked(cid)
            seq = self._next_seq[cid]
            self._next_seq[cid] += 1
            item: dict[str, Any] = {
                "seq": seq,
                "_chat_id": cid,
                "role": role,
                "content": copy.deepcopy(content),
                "ts": time.time(),
            }
            for k, v in extra.items():
                if k not in ("seq", "_chat_id"):
                    item[k] = copy.deepcopy(v)
            entries.append(item)
            self._append_line_locked(cid, item)
            return seq

    def replace_session(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cid = str(chat_id)
        with self._lock:
            self._ensure_loaded_locked(cid)
            seq = self._next_seq[cid]
            new_entries: list[dict[str, Any]] = []
            for msg in messages:
                entry = copy.deepcopy(msg)
                entry["_chat_id"] = cid
                entry_seq = entry.get("seq")
                if isinstance(entry_seq, int) and entry_seq > 0:
                    seq = max(seq, entry_seq + 1)
                else:
                    entry["seq"] = seq
                    seq += 1
                entry.setdefault("ts", time.time())
                new_entries.append(entry)
            self._next_seq[cid] = seq
            self._sessions[cid] = new_entries
            self._rewrite_file_locked(cid, new_entries)
            return [self._strip_internal_seq(item) for item in new_entries]

    def truncate_before_seq(
        self,
        chat_id: str,
        max_seq: int,
        keep_last_dialogue: int = 10,
        *,
        preserve_from_seq: int | None = None,
    ) -> int:
        """Truncate entries with seq <= max_seq while keeping system and trailing dialogue."""
        cid = str(chat_id)
        with self._lock:
            entries = self._ensure_loaded_locked(cid)
            if not entries:
                return 0

            before = [e for e in entries if int(e["seq"]) <= max_seq]
            after = [e for e in entries if int(e["seq"]) > max_seq]

            protected: list[dict[str, Any]] = []
            if preserve_from_seq is not None:
                protected = [e for e in before if int(e["seq"]) >= preserve_from_seq]
                before = [e for e in before if int(e["seq"]) < preserve_from_seq]

            systems = [e for e in before if e.get("role") == "system"]
            dialogue = [e for e in before if is_dialogue_message(e)]
            kept_dialogue = dialogue[-keep_last_dialogue:] if keep_last_dialogue > 0 else []

            kept_before = systems + kept_dialogue + protected
            kept_before.sort(key=lambda item: int(item["seq"]))

            new_entries = kept_before + after
            removed = len(entries) - len(new_entries)
            if removed > 0:
                self._sessions[cid] = new_entries
                self._rewrite_file_locked(cid, new_entries)
            return max(0, removed)

    def list_chat_ids(self) -> list[str]:
        with self._lock:
            self._scan_disk_chats_locked()
            return sorted(self._sessions.keys())

    def load_all(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            self._scan_disk_chats_locked()
            return {
                cid: [self._strip_internal_seq(item) for item in entries]
                for cid, entries in self._sessions.items()
            }


__all__ = ["JsonlHistoryStore", "safe_chat_filename"]
