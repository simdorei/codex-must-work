"""Hold one crash-released resident-manager lease per runtime."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.manager_runtime_values import runtime_file
from scripts.state import CorruptReason, CorruptStateError, StateError, StateLockTimeoutError
from scripts.state_io import (
    ExclusiveWriteLock,
    UnsafeStatePathError,
    ensure_direct_regular_file,
    prepare_parent_directories,
    safe_absolute_path,
)
from scripts.state_text import read_private_text, write_private_text

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


def manager_lease_owner(root: Path, runtime_name: str) -> int | None:
    """Return the validated PID holding the manager lease, if any."""
    _ = runtime_file(root, runtime_name)
    path = root / "managers" / f"{runtime_name}.lease"
    _ = safe_absolute_path(root, path)
    if not path.parent.is_dir():
        return None
    parent_identity = _manager_directory_identity(root, path.parent)
    ensure_direct_regular_file(root, path)
    lock_path = path.with_name(f".{path.name}.lock")
    ensure_direct_regular_file(root, lock_path)
    if not lock_path.exists():
        _require_same_manager_directory(root, path.parent, parent_identity)
        return None
    lock = ExclusiveWriteLock(path, timeout_seconds=0.0, create=False)
    try:
        lock.__enter__()
    except StateLockTimeoutError:
        _require_same_manager_directory(root, path.parent, parent_identity)
        marker = read_private_text(root, path, max_bytes=32).strip()
        _require_same_manager_directory(root, path.parent, parent_identity)
        try:
            owner = int(marker)
        except ValueError as error:
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from error
        if owner < 1 or marker != str(owner):
            raise CorruptStateError(path, CorruptReason.INVALID_VALUE) from None
        return owner
    try:
        _require_same_manager_directory(root, path.parent, parent_identity)
    finally:
        lock.__exit__(None, None, None)
    return None


def _manager_directory_identity(root: Path, path: Path) -> tuple[int, int]:
    root_absolute, path_absolute = safe_absolute_path(root, path)
    try:
        metadata = path_absolute.lstat()
    except OSError as error:
        raise UnsafeStatePathError(root_absolute, path_absolute) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeStatePathError(root_absolute, path_absolute)
    return metadata.st_dev, metadata.st_ino


def _require_same_manager_directory(
    root: Path,
    path: Path,
    expected: tuple[int, int],
) -> None:
    if _manager_directory_identity(root, path) != expected:
        root_absolute, path_absolute = safe_absolute_path(root, path)
        raise UnsafeStatePathError(root_absolute, path_absolute)


def release_manager_lease(lease: ManagerLease) -> None:
    """Remove the PID marker and release its operating-system lock."""
    lease.path.unlink(missing_ok=True)
    lease.lock.__exit__(None, None, None)
