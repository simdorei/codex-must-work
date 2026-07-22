"""Publish exact config snapshots with durability and qualified rollback."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final, Never

from scripts.config_metadata import (
    ConfigSnapshot,
    FileMetadata,
    apply_metadata,
    capture_config_snapshot,
    capture_metadata,
    open_metadata_descriptor,
    private_creation_metadata,
    read_config_bytes,
)
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import (
    FileIdentity,
    InstallerLease,
    file_identity,
    require_live_lease,
)
from scripts.state_io import UnsafeStatePathError, open_direct_file
from scripts.windows_file import (
    flush_directory,
    open_windows_path,
    rename_windows_file,
    replace_windows_file,
)

__all__ = (
    "ConfigSnapshot",
    "flush_directory",
    "read_config_bytes",
    "replace_windows_file",
    "write_config_bytes",
)

_CONCURRENT: Final = "codex_config_concurrent_change"
_METADATA_INVALID: Final = "codex_config_metadata_revalidation_failed"
_PUBLICATION_FAILED: Final = "codex_config_publication_failed"
_RESTORE_FAILED: Final = "codex_config_restore_failed"


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def _temporary(home: Path, data: bytes, metadata: FileMetadata) -> tuple[Path, FileIdentity]:
    descriptor, name = tempfile.mkstemp(prefix=".config.toml.cmw.", suffix=".tmp", dir=home)
    path = Path(name)
    identity = file_identity(os.fstat(descriptor))
    complete = False
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            _ = handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        secured = open_metadata_descriptor(path, descriptor) if os.name == "nt" else descriptor
        try:
            apply_metadata(secured, metadata)
            os.fsync(secured)
            if file_identity(path.lstat()) != identity:
                _fail(_PUBLICATION_FAILED)
            if capture_metadata(path, secured) != metadata:
                _fail(_METADATA_INVALID)
        finally:
            if secured != descriptor:
                os.close(secured)
        complete = True
        return path, identity
    finally:
        os.close(descriptor)
        if not complete:
            _remove_bound(path, identity)


def _unused_path(home: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".config.toml.cmw.", suffix=suffix, dir=home)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _rename_windows(descriptor: int, target: Path, *, replace: bool) -> None:
    try:
        rename_windows_file(descriptor, target, replace=replace)
    except OSError:
        if not replace and target.exists():
            _fail(_CONCURRENT)
        raise


def _windows_guard(path: Path, identity: FileIdentity, *, rename: bool) -> int:
    access = 0x00010080 if rename else 0x80
    descriptor = open_windows_path(path, access)
    if file_identity(os.fstat(descriptor)) != identity or os.get_inheritable(descriptor):
        os.close(descriptor)
        _fail(_PUBLICATION_FAILED)
    return descriptor


def _temporary_guard(expected: ConfigSnapshot, temporary: tuple[Path, FileIdentity]) -> int:
    path, identity = temporary
    if os.name == "nt":
        return _windows_guard(path, identity, rename=expected.identity is None)
    descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    if file_identity(os.fstat(descriptor)) != identity:
        os.close(descriptor)
        _fail(_PUBLICATION_FAILED)
    return descriptor


def publish_snapshot(
    lease: InstallerLease,
    expected: ConfigSnapshot,
    temporary: tuple[Path, FileIdentity],
    descriptor: int,
    backup: Path,
) -> FileIdentity:
    """Publish a verified temporary while preserving a raced destination."""
    temporary_path, temporary_identity = temporary
    try:
        if file_identity(os.fstat(descriptor)) != temporary_identity:
            _fail(_PUBLICATION_FAILED)
        if file_identity(temporary_path.lstat()) != temporary_identity:
            _fail(_PUBLICATION_FAILED)
        if expected.identity is None:
            if os.name == "nt":
                _rename_windows(descriptor, expected.path, replace=False)
            else:
                try:
                    os.link(temporary_path, expected.path, follow_symlinks=False)
                except FileExistsError:
                    _fail(_CONCURRENT)
                temporary_path.unlink()
            return file_identity(expected.path.lstat())
        if os.name != "nt":
            return _publish_posix(lease, expected, temporary, backup)
        replace_windows_file(expected.path, temporary_path, backup)
    finally:
        os.close(descriptor)
    captured = capture_config_snapshot(backup, lease)
    published_identity = file_identity(expected.path.lstat())
    if published_identity == temporary_identity and captured.state == expected.state:
        return temporary_identity
    if captured.identity is None:
        _fail(_RESTORE_FAILED)
    backup_descriptor = _windows_guard(backup, captured.identity, rename=True)
    try:
        _rename_windows(backup_descriptor, expected.path, replace=True)
    finally:
        os.close(backup_descriptor)
    if capture_config_snapshot(expected.path, lease).state != captured.state:
        _fail(_RESTORE_FAILED)
    _fail(_CONCURRENT)


def _publish_posix(
    lease: InstallerLease,
    expected: ConfigSnapshot,
    temporary: tuple[Path, FileIdentity],
    backup: Path,
) -> FileIdentity:
    temporary_path, temporary_identity = temporary
    _ = expected.path.replace(backup)
    captured = capture_config_snapshot(backup, lease)
    if captured.state != expected.state:
        with suppress(FileExistsError):
            os.link(backup, expected.path, follow_symlinks=False)
        _fail(_CONCURRENT)
    try:
        os.link(temporary_path, expected.path, follow_symlinks=False)
    except FileExistsError:
        _fail(_CONCURRENT)
    temporary_path.unlink()
    return temporary_identity


def _remove_bound(path: Path, identity: FileIdentity) -> None:
    with suppress(FileNotFoundError):
        if file_identity(path.lstat()) == identity:
            path.unlink()


def _restore(
    lease: InstallerLease,
    expected: ConfigSnapshot,
    published: tuple[FileIdentity, bytes],
    backup: Path,
) -> None:
    temporary_identity, replacement = published
    current = capture_config_snapshot(expected.path, lease)
    if current == expected:
        return
    if current.identity != temporary_identity or current.data != replacement:
        _fail(_CONCURRENT)
    try:
        if expected.identity is None:
            _remove_bound(expected.path, temporary_identity)
        elif backup.exists() and file_identity(backup.lstat()) == expected.identity:
            _ = backup.replace(expected.path)
        elif file_identity(expected.path.lstat()) != expected.identity:
            _fail(_RESTORE_FAILED)
        flush_directory(lease.home)
        if capture_config_snapshot(expected.path, lease) != expected:
            _fail(_RESTORE_FAILED)
    except (OSError, InstallPluginError, UnsafeStatePathError) as error:
        raise InstallPluginError(_RESTORE_FAILED) from error


def _verify_published(
    lease: InstallerLease,
    expected: ConfigSnapshot,
    replacement: bytes,
    identity: FileIdentity,
    metadata: FileMetadata,
) -> None:
    descriptor = open_direct_file(expected.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if os.name == "nt" and expected.identity is not None:
            secured = open_metadata_descriptor(expected.path, handle.fileno())
            try:
                apply_metadata(secured, metadata)
            finally:
                os.close(secured)
        os.fsync(handle.fileno())
    flush_directory(lease.home)
    final = capture_config_snapshot(expected.path, lease)
    if final.state != (replacement, identity, metadata):
        _fail(_METADATA_INVALID)


def write_config_bytes(
    lease: InstallerLease,
    expected: ConfigSnapshot,
    replacement: bytes,
) -> bytes:
    """Compare, publish, flush, verify, and rollback one exact replacement."""
    require_live_lease(lease)
    if expected.lease_owner != lease.owner or expected.path.parent != lease.home:
        _fail("installer_lock_lease_mismatch")
    if capture_config_snapshot(expected.path, lease) != expected:
        _fail(_CONCURRENT)
    if expected.identity is not None and replacement == expected.data:
        return replacement
    metadata = expected.metadata or private_creation_metadata(
        capture_metadata(lease.lock_path, lease.descriptor)
    )
    temporary = _temporary(lease.home, replacement, metadata)
    temporary_path, temporary_identity = temporary
    backup = _unused_path(lease.home, ".bak")
    published = False

    try:
        if capture_config_snapshot(expected.path, lease) != expected:
            _fail(_CONCURRENT)
        descriptor = _temporary_guard(expected, temporary)
        actual_identity = publish_snapshot(lease, expected, temporary, descriptor, backup)
        published = True
        if actual_identity != temporary_identity:
            _fail(_PUBLICATION_FAILED)
        _verify_published(lease, expected, replacement, temporary_identity, metadata)
    except (OSError, InstallPluginError, UnsafeStatePathError) as error:
        if published or backup.exists():
            _restore(lease, expected, (temporary_identity, replacement), backup)
        if isinstance(error, InstallPluginError):
            raise
        raise InstallPluginError(_PUBLICATION_FAILED) from error
    finally:
        _remove_bound(temporary_path, temporary_identity)
        if expected.identity is not None:
            _remove_bound(backup, expected.identity)
    return replacement
