"""Smoke tests for examples and Clean-Room non-leakage checks."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_example(rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_examples_02_and_03_run_cleanly(tmp_path: Path) -> None:
    mod2 = _load_example("examples/02_multi_bot_single_process.py")
    res2 = await mod2.run_demo(tmp_path / "ex2")
    assert res2 == {
        "alpha": "Alpha checking in!",
        "beta": "Beta here, following circadian rhythm.",
    }

    mod3 = _load_example("examples/03_custom_middleware_and_focus.py")
    res3 = await mod3.run_demo(tmp_path / "ex3")
    assert res3 == "Here is your star!"


def test_example_01_dry_run_preflight(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["FROSTCORE_DRY_RUN"] = "1"
    env["FROSTCORE_SEED_DIR"] = str(tmp_path / "qs.seed")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples/01_quickstart.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Dry-run preflight ok" in proc.stdout


def test_clean_room_source_has_no_yuki_legacy_symbols() -> None:
    forbidden = (
        "phone_focus_work",
        "is_phone_summary",
        "from config import",
        "send_master_private",
        "napcat",
        "<layout>",
        "[MEME:",
    )
    for py_file in (ROOT / "src" / "frostcore").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"Legacy symbol {token!r} leaked into {py_file}"
