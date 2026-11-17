"""Tests for FrostSeed single-instance lock (same-process, cross-process, and multi-seed)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from frostcore.memory import FrostSeed, active_seed_count
from frostcore.testing import verify_seed_single_instance_contract


def test_seed_same_process_single_instance_contract(tmp_path: Path) -> None:
    seed_dir = tmp_path / "bot_a.seed"
    verify_seed_single_instance_contract(seed_dir)
    assert active_seed_count() == 0


def test_multiple_distinct_seeds_coexist_in_same_process(tmp_path: Path) -> None:
    dir_a = tmp_path / "bot_a.seed"
    dir_b = tmp_path / "bot_b.seed"
    with FrostSeed(dir_a) as seed_a, FrostSeed(dir_b) as seed_b:
        assert active_seed_count() == 2
        seed_a.set_note("mood", "cheerful")
        seed_b.set_note("mood", "sleepy")
        assert seed_a.get_note("mood") == "cheerful"
        assert seed_b.get_note("mood") == "sleepy"
    assert active_seed_count() == 0


def test_seed_cross_process_flock_blocks_second_opener(tmp_path: Path) -> None:
    seed_dir = tmp_path / "cross.seed"
    with FrostSeed(seed_dir):
        code = (
            "from frostcore.memory import FrostSeed, SeedLockedError\n"
            f"try:\n"
            f"    FrostSeed({str(seed_dir)!r})\n"
            "except SeedLockedError:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


def test_seed_entities_meta_and_rhythm_persist_across_reopen(tmp_path: Path) -> None:
    seed_dir = tmp_path / "persist.seed"
    with FrostSeed(seed_dir) as seed:
        seed.upsert_entity(
            "user_alice",
            "Alice",
            aliases=["ali", "小艾"],
            impression="kind friend",
            affinity=0.85,
        )
        seed.set_meta("persona_tag", "calm")
        seed.save_rhythm_snapshot(
            "g1",
            energy=72.5,
            activity=3.2,
            desire=64.0,
            last_active_at=100.0,
        )
        assert seed.epoch == 1

    with FrostSeed(seed_dir) as reopened:
        assert reopened.epoch == 2
        ent = reopened.get_entity("user_alice")
        assert ent is not None
        assert ent.display_name == "Alice"
        assert ent.aliases == ("ali", "小艾")
        assert ent.affinity == 0.85
        assert reopened.get_meta("persona_tag") == "calm"
        snap = reopened.load_rhythm_snapshot("g1")
        assert snap is not None
        assert snap.energy == 72.5
        assert snap.activity == 3.2
