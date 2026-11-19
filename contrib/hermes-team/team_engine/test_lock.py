"""Shared process-safe lock for heavyweight verification commands.

All controller workers use the Hermes data mount, and the host-side isolated
backend worker uses that same absolute data path. Commands delegated to that
worker acquire the lock themselves; an outer runner must not hold it while
waiting for the delegated command.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator, Sequence


def default_test_lock_path() -> Path:
    return Path(os.environ.get("HERMES_TEAM_TEST_LOCK",
                               str(Path.home() / ".hermes/team/data/.heavy-test.lock")))


def delegated_test_runner(argv: Sequence[str]) -> bool:
    """True when the command launches the separately locking backend queue."""
    for argument in argv:
        normalized = str(argument).replace("\\", "/")
        basename = normalized.rsplit("/", 1)[-1]
        if basename in {"hermes-backend-tests", "backend_test_jobs.py"}:
            return True
        if normalized == "team_engine.backend_test_jobs":
            return True
    return False


@contextmanager
def heavy_test_lock(path: str | os.PathLike[str] | None = None) -> Iterator[Path]:
    """Acquire the shared heavy-test lock until the current check finishes."""
    lock_path = Path(path) if path is not None else default_test_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
