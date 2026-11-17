"""Cross-platform + in-process single-instance locking for FrostSeed directories."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

if sys.platform == "win32":  # pragma: no cover
    import msvcrt
else:
    import fcntl


class SeedLockedError(RuntimeError):
    """Raised when attempting to open a FrostSeed already locked by another instance."""


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SEED_PATHS: set[str] = set()


def active_seed_count() -> int:
    """Return number of FrostSeed directories currently open in this process."""
    with _ACTIVE_LOCK:
        return len(_ACTIVE_SEED_PATHS)


class SeedLock:
    """Exclusive non-blocking lock combining an in-process set and OS file lock."""

    def __init__(self, lock_path: Path | str) -> None:
        self.lock_path = Path(lock_path)
        self._canonical = str(self.lock_path.resolve())
        self._fh: IO[bytes] | None = None
        self._held = False

    @property
    def is_held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        if self._held:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _ACTIVE_LOCK:
            if self._canonical in _ACTIVE_SEED_PATHS:
                raise SeedLockedError(
                    f"FrostSeed is already open in this process: {self.lock_path.parent}"
                )
            _ACTIVE_SEED_PATHS.add(self._canonical)

        fh: IO[bytes] | None = None
        try:
            fh = self.lock_path.open("a+b")
            self._flock_nb(fh.fileno())
            fh.seek(0)
            fh.truncate()
            marker = f"pid={os.getpid()} acquired_at={time.time():.3f}\n".encode()
            fh.write(marker)
            fh.flush()
        except Exception as exc:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            with _ACTIVE_LOCK:
                _ACTIVE_SEED_PATHS.discard(self._canonical)
            if isinstance(exc, SeedLockedError):
                raise
            raise SeedLockedError(
                f"Failed to lock FrostSeed ({self.lock_path.parent}): {exc}"
            ) from exc

        self._fh = fh
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        fh = self._fh
        self._fh = None
        self._held = False
        try:
            if fh is not None:
                self._funlock(fh.fileno())
                fh.close()
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_SEED_PATHS.discard(self._canonical)

    @staticmethod
    def _flock_nb(fd: int) -> None:
        if sys.platform == "win32":  # pragma: no cover
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _funlock(fd: int) -> None:
        if sys.platform == "win32":  # pragma: no cover
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def __enter__(self) -> SeedLock:
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


__all__ = ["SeedLock", "SeedLockedError", "active_seed_count"]
