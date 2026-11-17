"""Tests for JsonlHistoryStore append-only writes, recovery, and truncation."""
from __future__ import annotations

from pathlib import Path

from frostcore.memory import JsonlHistoryStore, safe_chat_filename


def test_safe_chat_filename_prevents_traversal_and_collisions() -> None:
    a = safe_chat_filename("../../etc/passwd")
    b = safe_chat_filename(".._.._etc_passwd")
    assert "/" not in a and "\\" not in a and ".." not in a
    assert a != b


def test_history_append_persists_across_reopen_and_recovers_corrupt_tail(tmp_path: Path) -> None:
    h_dir = tmp_path / "history"
    store = JsonlHistoryStore(h_dir)
    s1 = store.append_message("room/1", "user", "hello")
    s2 = store.append_message("room/1", "assistant", "hi there")
    assert (s1, s2) == (1, 2)

    target_file = h_dir / safe_chat_filename("room/1")
    assert target_file.exists()
    # Simulate crash leaving truncated final JSON line
    with target_file.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "role": "user", "content": "broken')

    reopened = JsonlHistoryStore(h_dir)
    assert reopened.list_chat_ids() == ["room/1"]
    msgs = reopened.get_session("room/1")
    assert [m["content"] for m in msgs] == ["hello", "hi there"]
    assert reopened.corrupted_lines_skipped == 1


def test_history_truncate_before_seq_keeps_newer_and_system(tmp_path: Path) -> None:
    store = JsonlHistoryStore(tmp_path / "history")
    store.append_message("c1", "system", "sys prompt")
    store.append_message("c1", "user", "u1")
    store.append_message("c1", "assistant", "a1")
    wm = store.append_message("c1", "user", "u2")
    store.append_message("c1", "assistant", "a2_new")

    removed = store.truncate_before_seq("c1", wm, keep_last_dialogue=1)
    assert removed == 2
    contents = [m["content"] for m in store.get_session("c1")]
    assert contents == ["sys prompt", "u2", "a2_new"]
