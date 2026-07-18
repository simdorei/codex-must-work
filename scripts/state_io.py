"""Low-level safe filesystem operations for persisted plugin state."""

from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, final, override

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

_DIRECTORY_MODE: Final = stat.S_IRWXU
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_LOCK_TIMEOUT_SECONDS: Final = 1.0
_LOCK_RETRY_SECONDS: Final = 0.02


class _WindowsLockModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, count: int) -> int: ...


class _PosixLockModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> int: ...


_WINDOWS_LOCK = (
    cast("_WindowsLockModule", cast("object", importlib.import_module("msvcrt")))
    if os.name == "nt"
    else None
)
_POSIX_LOCK = (
    cast("_PosixLockModule", cast("object", importlib.import_module("fcntl")))
    if os.name != "nt"
    else None
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class StateError(Exception):
    """Base error for state persistence failures."""


@dataclass(frozen=True, slots=True)
class UnsafeStatePathError(StateError):
    """Report a state path that escapes or redirects outside its root."""

    root: Path
    path: Path

    @override
    def __str__(self) -> str:
        return f"state path is outside or redirects from {self.root}: {self.path}"


@dataclass(frozen=True, slots=True)
class StateLockTimeoutError(StateError):
    """Refuse a write when another process keeps its state lock."""

    path: Path
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS

    @override
    def __str__(self) -> str:
        return f"state write lock timed out at {self.path}: timeout_seconds={self.timeout_seconds}"


@final
class ExclusiveWriteLock:
    """Coordinate writers with a crash-released operating-system file lock."""

    def __init__(
        self,
        state_path: Path,
        timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    ) -> None:
        """Bind a lock file to one state path and bounded wait."""
        self.state_path = state_path
        self.timeout_seconds = timeout_seconds
        self._descriptor: int | None = None

    def __enter__(self) -> None:
        """Acquire this state file's exclusive lock."""
        lock_path = self._path()
        descriptor = _open_direct_file(lock_path, os.O_CREAT | os.O_RDWR)
        acquired = False
        try:
            lock_path.chmod(_FILE_MODE)
            if os.fstat(descriptor).st_size == 0:
                _ = os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _acquire_descriptor(descriptor, lock_path, self.timeout_seconds)
            acquired = True
        finally:
            if not acquired:
                os.close(descriptor)
        self._descriptor = descriptor

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Release this state file's exclusive lock."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)
            self._descriptor = None

    def _path(self) -> Path:
        return self.state_path.with_name(f".{self.state_path.name}.lock")


def _try_lock_descriptor(descriptor: int) -> bool:
    try:
        _ = os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            if _WINDOWS_LOCK is None:
                return False
            _ = _WINDOWS_LOCK.locking(descriptor, _WINDOWS_LOCK.LK_NBLCK, 1)
        else:
            if _POSIX_LOCK is None:
                return False
            _ = _POSIX_LOCK.flock(
                descriptor,
                _POSIX_LOCK.LOCK_EX | _POSIX_LOCK.LOCK_NB,
            )
    except OSError:
        return False
    return True


def _acquire_descriptor(descriptor: int, lock_path: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not _try_lock_descriptor(descriptor):
        if time.monotonic() >= deadline:
            raise StateLockTimeoutError(lock_path, timeout_seconds)
        time.sleep(_LOCK_RETRY_SECONDS)


def _unlock_descriptor(descriptor: int) -> None:
    _ = os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        if _WINDOWS_LOCK is not None:
            _ = _WINDOWS_LOCK.locking(descriptor, _WINDOWS_LOCK.LK_UNLCK, 1)
    elif _POSIX_LOCK is not None:
        _ = _POSIX_LOCK.flock(descriptor, _POSIX_LOCK.LOCK_UN)


def safe_absolute_path(root: Path, path: Path) -> tuple[Path, Path]:
    """Return contained absolute paths after checking existing redirects."""
    root_absolute = Path(os.path.abspath(root))  # noqa: PTH100
    path_absolute = Path(os.path.abspath(path))  # noqa: PTH100
    if path_absolute == root_absolute or not path_absolute.is_relative_to(root_absolute):
        raise UnsafeStatePathError(root=root_absolute, path=path_absolute)
    ensure_existing_components_are_direct(root_absolute, path_absolute)
    return root_absolute, path_absolute


def ensure_direct_regular_file(root: Path, path: Path) -> None:
    """Reject a hard-linked or redirected existing regular file."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    if not path_absolute.exists():
        return
    metadata = path_absolute.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsafeStatePathError(root_absolute, path_absolute)


def write_private_text(root: Path, path: Path, value: str) -> None:
    """Write a small private marker without following a final redirect."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    descriptor = _open_direct_file(path_absolute, os.O_CREAT | os.O_WRONLY)
    try:
        payload = value.encode("ascii")
        os.ftruncate(descriptor, 0)
        _ = os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    ensure_direct_regular_file(root_absolute, path_absolute)
    if os.name != "nt":
        path_absolute.chmod(_FILE_MODE, follow_symlinks=False)


def ensure_existing_components_are_direct(root: Path, path: Path) -> None:
    """Reject symlink, junction, and Windows reparse-point redirects."""
    relative = path.relative_to(root)
    current = root
    for part in ("", *relative.parts):
        current = current if part == "" else current / part
        if current.is_symlink() or current.is_junction():
            raise UnsafeStatePathError(root=root, path=current)
        if (
            os.name == "nt"
            and current.exists()
            and current.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise UnsafeStatePathError(root=root, path=current)


def prepare_parent_directories(root: Path, path: Path) -> None:
    """Create direct, current-user-only directories for a state file."""
    root.mkdir(parents=True, exist_ok=True)
    ensure_existing_components_are_direct(root, root)
    root.chmod(_DIRECTORY_MODE)
    current = root
    for part in path.parent.relative_to(root).parts:
        current /= part
        current.mkdir(exist_ok=True)
        ensure_existing_components_are_direct(root, current)
        current.chmod(_DIRECTORY_MODE)


def atomic_json_write(
    path: Path,
    *,
    schema_version: int,
    values: Mapping[str, JsonValue],
) -> None:
    """Flush JSON to a same-directory temporary file, then replace atomically."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                {"schema_version": schema_version, **values},
                handle,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            _ = handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(_FILE_MODE)
        _ = temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _open_direct_file(path: Path, flags: int) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | no_follow, _FILE_MODE)
    except OSError as error:
        raise UnsafeStatePathError(path.parent, path) from error
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError:
        os.close(descriptor)
        raise
    unsafe = (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or opened.st_dev != named.st_dev
        or opened.st_ino != named.st_ino
    )
    if unsafe:
        os.close(descriptor)
        raise UnsafeStatePathError(path.parent, path)
    return descriptor
