"""Serialize installer work with one persistent OS-backed lease."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

try:
    import fcntl as _fcntl_module
except ImportError:
    _fcntl_module = None

try:
    import msvcrt as _msvcrt_module
except ImportError:
    _msvcrt_module = None

from scripts import private_root
from scripts.install_errors import InstallPluginError
from scripts.state_io import UnsafeStatePathError, open_direct_file
from scripts.windows_file import final_windows_path, open_windows_path

_LOCK_DIRECTORY: Final = "cmw-installer-locks"
_LOCK_RETRY_SECONDS: Final = 0.1
_LOCK_TIMEOUT_SECONDS: Final = 120.0
_FAILED: Final = "installer_lock_failed"
_INVALID: Final = "installer_lock_lease_invalid"
_REENTRY: Final = "installer_lock_reentry"
_TIMEOUT: Final = "installer_lock_timeout"
_UNSAFE: Final = "installer_lock_unsafe"
_REGISTRY: Final[set[tuple[str, int, int]]] = set()
_REGISTRY_LOCK: Final = threading.Lock()


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Identify one opened filesystem object."""

    device: int
    inode: int


@dataclass(frozen=True, slots=True, repr=False)
class InstallerLease:
    """Carry proof that this process and thread own the installer lock."""

    home: Path = field(repr=False)
    home_key: str = field(repr=False)
    owner: tuple[int, int]
    home_identity: FileIdentity
    lock_path: Path = field(repr=False)
    lock_identity: FileIdentity
    descriptor: int = field(repr=False)
    home_descriptor: int = field(default=-1, repr=False)


@dataclass(frozen=True, slots=True)
class _WindowsLockOps:
    try_lock: Callable[[int], None]
    unlock: Callable[[int], None]
    clock: Callable[[], float] = time.monotonic
    pause: Callable[[float], None] = time.sleep


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def _fail_from(reason: str, error: BaseException) -> Never:
    raise InstallPluginError(reason) from error


def file_identity(metadata: os.stat_result) -> FileIdentity:
    """Return the stable identity fields shared by supported hosts."""
    return FileIdentity(metadata.st_dev, metadata.st_ino)


def _canonical_home(codex_home: Path) -> tuple[Path, int, FileIdentity]:
    absolute = codex_home.absolute()
    descriptor: int | None = None
    try:
        if os.name == "nt":
            descriptor = open_windows_path(absolute, 0x80, attributes=0x02000000)
            resolved = final_windows_path(descriptor)
        else:
            resolved = absolute.resolve(strict=True)
            descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        opened = os.fstat(descriptor)
        named = absolute.stat()
        target = resolved.stat()
    except (OSError, RuntimeError) as error:
        if descriptor is not None:
            os.close(descriptor)
        _fail_from(_UNSAFE, error)
    identity = file_identity(opened)
    identities = {file_identity(named), file_identity(target)}
    if not stat.S_ISDIR(opened.st_mode) or identities != {identity}:
        os.close(descriptor)
        _fail(_UNSAFE)
    if os.get_inheritable(descriptor):
        os.close(descriptor)
        _fail(_UNSAFE)
    return resolved, descriptor, identity


def _home_key(home: Path) -> str:
    normalized = os.path.normcase(str(home)).replace("/", os.sep)
    return normalized.casefold() if os.name == "nt" else normalized


def home_lock_key(codex_home: Path) -> str:
    """Derive the private normalized identity used only for lock naming."""
    home, descriptor, _ = _canonical_home(codex_home)
    os.close(descriptor)
    return _home_key(home)


def _lock_path(key: str) -> Path:
    root = Path(tempfile.gettempdir()) / _LOCK_DIRECTORY
    try:
        private_root.ensure_private_root(root)
    except private_root.PrivateRootError as error:
        _fail_from(_UNSAFE, error)
    return root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"


def _lock_precondition(path: Path) -> tuple[FileIdentity, FileIdentity | None]:
    try:
        previous = file_identity(path.lstat())
    except FileNotFoundError:
        previous = None
    return file_identity(path.parent.lstat()), previous


def _verify_open_lock(
    path: Path, descriptor: int, precondition: tuple[FileIdentity, FileIdentity | None]
) -> None:
    opened = os.fstat(descriptor)
    root_identity, previous_identity = precondition
    if previous_identity is not None and previous_identity != file_identity(opened):
        _fail(_UNSAFE)
    if file_identity(path.parent.lstat()) != root_identity:
        _fail(_UNSAFE)
    if os.name != "nt" and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o077):
        _fail(_UNSAFE)
    if os.get_inheritable(descriptor):
        _fail(_UNSAFE)
    if opened.st_size == 0:
        _ = os.write(descriptor, b"\0")
        os.fsync(descriptor)


def _open_persistent_lock(path: Path) -> int:
    precondition = _lock_precondition(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    try:
        descriptor = open_direct_file(path, flags)
    except (OSError, UnsafeStatePathError) as error:
        _fail_from(_UNSAFE, error)
    try:
        _verify_open_lock(path, descriptor, precondition)
    except (OSError, InstallPluginError) as error:
        os.close(descriptor)
        if isinstance(error, InstallPluginError):
            raise
        _fail_from(_UNSAFE, error)
    return descriptor


def _windows_lock_ops() -> _WindowsLockOps:
    runtime = _msvcrt_module
    if runtime is None:
        _fail(_FAILED)

    def try_lock(descriptor: int) -> None:
        runtime.locking(descriptor, runtime.LK_NBLCK, 1)

    def unlock(descriptor: int) -> None:
        runtime.locking(descriptor, runtime.LK_UNLCK, 1)

    return _WindowsLockOps(try_lock, unlock)


def _acquire_windows_lock(
    descriptor: int, timeout_seconds: float, operations: _WindowsLockOps
) -> None:
    deadline = operations.clock() + timeout_seconds
    while True:
        try:
            _ = os.lseek(descriptor, 0, os.SEEK_SET)
            operations.try_lock(descriptor)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EDEADLK}:
                _fail_from(_FAILED, error)
            if operations.clock() >= deadline:
                _fail_from(_TIMEOUT, error)
            operations.pause(_LOCK_RETRY_SECONDS)
        else:
            return


def _acquire(descriptor: int, timeout_seconds: float) -> None:
    if sys.platform == "win32":
        _acquire_windows_lock(descriptor, timeout_seconds, _windows_lock_ops())
        return
    runtime = _fcntl_module
    if runtime is None:
        _fail(_FAILED)
    try:
        runtime.flock(descriptor, runtime.LOCK_EX)
    except OSError as error:
        _fail_from(_FAILED, error)


def _unlock(descriptor: int) -> None:
    try:
        _ = os.lseek(descriptor, 0, os.SEEK_SET)
        if sys.platform == "win32":
            _windows_lock_ops().unlock(descriptor)
            return
        runtime = _fcntl_module
        if runtime is None:
            _fail(_FAILED)
        runtime.flock(descriptor, runtime.LOCK_UN)
    except OSError as error:
        _fail_from(_FAILED, error)


def require_live_lease(lease: InstallerLease) -> None:
    """Reject stale, moved, cross-thread, or forged lease values."""
    registration = (lease.home_key, *lease.owner)
    try:
        home_identity = file_identity(os.fstat(lease.home_descriptor))
        named_home = file_identity(lease.home.stat())
        lock_identity = file_identity(os.fstat(lease.descriptor))
        named_lock = file_identity(lease.lock_path.lstat())
    except OSError as error:
        _fail_from(_INVALID, error)
    if not (
        lease.owner == (os.getpid(), threading.get_ident())
        and registration in _REGISTRY
        and home_identity == lease.home_identity == named_home
        and lock_identity == lease.lock_identity == named_lock
    ):
        _fail(_INVALID)


@contextmanager
def installer_lock(
    codex_home: Path, timeout_seconds: float = _LOCK_TIMEOUT_SECONDS
) -> Generator[InstallerLease]:
    """Acquire one persistent, non-reentrant installer lease."""
    home, home_descriptor, home_identity = _canonical_home(codex_home)
    key = _home_key(home)
    path = _lock_path(key)
    owner = os.getpid(), threading.get_ident()
    registration = (key, *owner)
    with _REGISTRY_LOCK:
        if registration in _REGISTRY:
            _fail(_REENTRY)
        _REGISTRY.add(registration)
    descriptor: int | None = None
    acquired = False
    try:
        descriptor = _open_persistent_lock(path)
        _acquire(descriptor, timeout_seconds)
        acquired = True
        yield InstallerLease(
            home,
            key,
            owner,
            home_identity,
            path,
            file_identity(os.fstat(descriptor)),
            descriptor,
            home_descriptor,
        )
    finally:
        try:
            if acquired and descriptor is not None:
                _unlock(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(home_descriptor)
            with _REGISTRY_LOCK:
                _REGISTRY.discard(registration)
