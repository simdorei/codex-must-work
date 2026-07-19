# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run one bounded user-level watcher for opted-in sessions."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.private_root import ensure_private_root
from scripts.state import StateError, StateLockTimeoutError, state_root
from scripts.state_io import ExclusiveWriteLock, safe_absolute_path
from scripts.state_text import write_private_text
from scripts.watcher_engine import WatcherEngine

_POLL_SECONDS: Final = 1.0
_LEASE_HANDOFF_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class _WatcherLease:
    path: Path
    pid: int
    lock: ExclusiveWriteLock


def acquire_watcher_lease(
    root: Path,
    *,
    timeout_seconds: float = _LEASE_HANDOFF_SECONDS,
) -> _WatcherLease | None:
    """Acquire the singleton watcher lease or return None when one is live."""
    if not root.is_dir():
        return None
    ensure_private_root(root)
    _, path = safe_absolute_path(root, root / "watcher.lease")
    lock = ExclusiveWriteLock(path, timeout_seconds=timeout_seconds)
    try:
        lock.__enter__()
    except StateLockTimeoutError:
        return None
    pid = os.getpid()
    try:
        lease_path = path
        write_private_text(root, lease_path, f"{pid}\n")
    except (OSError, StateError):
        lock.__exit__(None, None, None)
        raise
    return _WatcherLease(path, pid, lock)


def refresh_watcher_lease(lease: _WatcherLease) -> None:
    """Refresh one live watcher marker without changing its owner."""
    write_private_text(lease.path.parent, lease.path, f"{lease.pid}\n")


def release_watcher_lease(lease: _WatcherLease) -> None:
    """Release a singleton lease only when this process still owns it."""
    lease.path.unlink(missing_ok=True)
    lease.lock.__exit__(None, None, None)


def _main() -> int:
    lease = acquire_watcher_lease(state_root())
    if lease is None:
        return 0
    try:
        engine = WatcherEngine(state_root())
        while engine.tick(time.monotonic(), datetime.now(UTC)):
            refresh_watcher_lease(lease)
            time.sleep(_POLL_SECONDS)
    except (OSError, StateError, ValueError):
        return 1
    finally:
        release_watcher_lease(lease)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
