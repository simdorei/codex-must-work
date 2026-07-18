"""Hold one crash-released resident-manager lease per runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.manager_runtime_values import runtime_file
from scripts.state import StateError, StateLockTimeoutError
from scripts.state_io import ExclusiveWriteLock, prepare_parent_directories, write_private_text

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ManagerLease:
    """Keep the operating-system lock and visible owner PID together."""

    path: Path
    lock: ExclusiveWriteLock


def acquire_manager_lease(root: Path, runtime_name: str) -> ManagerLease | None:
    """Acquire the singleton lease or return None when another manager owns it."""
    _ = runtime_file(root, runtime_name)
    path = root / "managers" / f"{runtime_name}.lease"
    prepare_parent_directories(root, path)
    lock = ExclusiveWriteLock(path, timeout_seconds=0.0)
    try:
        lock.__enter__()
    except StateLockTimeoutError:
        return None
    try:
        write_private_text(root, path, f"{os.getpid()}\n")
    except (OSError, StateError):
        lock.__exit__(None, None, None)
        raise
    return ManagerLease(path, lock)


def release_manager_lease(lease: ManagerLease) -> None:
    """Remove the PID marker and release its operating-system lock."""
    lease.path.unlink(missing_ok=True)
    lease.lock.__exit__(None, None, None)
