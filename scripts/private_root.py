"""Protect the plugin state root before any private state is written."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from typing import Final

from scripts.private_root_windows import (
    PrivateRootError,
    PrivateRootReason,
    secure_windows_root,
)

__all__ = ["PrivateRootError", "PrivateRootReason", "ensure_private_root", "verify_private_root"]

_DIRECTORY_MODE: Final = stat.S_IRWXU
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_MARKER_NAME: Final = ".private-root-v1"
_MARKER_CONTENT: Final = b"private-root-v1\n"


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@unique
class _RootSecurityMode(StrEnum):
    APPLY = "apply"
    VERIFY = "verify"


def ensure_private_root(root: Path) -> None:
    """Create or verify a state root restricted to the current OS user."""
    absolute = Path(os.path.abspath(root))  # noqa: PTH100
    _require_direct_parent(absolute)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        _initialize_root(absolute)
        return
    _require_direct_directory(absolute, metadata)
    marker = absolute / _MARKER_NAME
    try:
        marker_metadata = marker.lstat()
    except FileNotFoundError as error:
        raise PrivateRootError(absolute, PrivateRootReason.MIGRATION_REQUIRED) from error
    if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink != 1:
        raise PrivateRootError(absolute, PrivateRootReason.PATH_UNSAFE)
    _secure_root(absolute, _RootSecurityMode.VERIFY)


def verify_private_root(root: Path) -> None:
    """Verify an existing private root without creating any filesystem object."""
    absolute = Path(os.path.abspath(root))  # noqa: PTH100
    _require_direct_parent(absolute)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as error:
        raise PrivateRootError(absolute, PrivateRootReason.PATH_UNSAFE) from error
    _require_direct_directory(absolute, metadata)
    marker = absolute / _MARKER_NAME
    try:
        marker_metadata = marker.lstat()
    except FileNotFoundError as error:
        raise PrivateRootError(absolute, PrivateRootReason.MIGRATION_REQUIRED) from error
    if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink != 1:
        raise PrivateRootError(absolute, PrivateRootReason.PATH_UNSAFE)
    _secure_root(absolute, _RootSecurityMode.VERIFY)


def _initialize_root(root: Path) -> None:
    root.mkdir(mode=_DIRECTORY_MODE)
    identity = _identity(root.lstat())
    initialized = False
    try:
        _secure_root(root, _RootSecurityMode.APPLY)
        try:
            secured_metadata = root.lstat()
        except FileNotFoundError as error:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE) from error
        _require_direct_directory(root, secured_metadata)
        if _identity(secured_metadata) != identity:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE)
        _create_marker(root / _MARKER_NAME)
        initialized = True
    finally:
        if not initialized:
            _remove_empty_same_root(root, identity)


def _secure_root(root: Path, mode: _RootSecurityMode) -> None:
    if os.name == "nt":
        secure_windows_root(root, verify=mode is _RootSecurityMode.VERIFY)
        return
    root.chmod(_DIRECTORY_MODE)


def _require_direct_parent(root: Path) -> None:
    parent = root.parent
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE) from error
        _require_direct_directory(root, metadata)


def _require_direct_directory(root: Path, metadata: os.stat_result) -> None:
    redirected = stat.S_ISLNK(metadata.st_mode) or (
        os.name == "nt" and bool(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )
    if redirected or not stat.S_ISDIR(metadata.st_mode):
        raise PrivateRootError(root, PrivateRootReason.PATH_UNSAFE)


def _create_marker(marker: Path) -> None:
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
    identity = _identity(os.fstat(descriptor))
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(_MARKER_CONTENT)
            handle.flush()
            os.fsync(handle.fileno())
        complete = True
    finally:
        if not complete:
            _unlink_same_file(marker, identity)


def _remove_empty_same_root(root: Path, identity: _FileIdentity) -> None:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if _identity(metadata) != identity:
        return
    with suppress(OSError):
        root.rmdir()


def _unlink_same_file(path: Path, identity: _FileIdentity) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _identity(metadata) == identity:
        with suppress(OSError):
            path.unlink()


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(device=metadata.st_dev, inode=metadata.st_ino)
