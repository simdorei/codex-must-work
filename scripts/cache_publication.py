"""Durable no-replace publication and identity-bound cleanup."""

from __future__ import annotations

import ctypes
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Never, cast

from scripts.cache_package import expected_directories
from scripts.cache_security import create_secure_directory, require_directory, secure_identity
from scripts.cache_semver import higher
from scripts.cache_types import CacheIdentity, identity
from scripts.cache_windows import mark_windows_delete, open_locked
from scripts.install_errors import InstallPluginError
from scripts.private_root import PrivateRootError, ensure_private_root
from scripts.state_io import UnsafeStatePathError, open_direct_file
from scripts.windows_file import flush_windows_directory, open_windows_path, rename_windows_file


def cache_roots(home: Path) -> tuple[Path, Path]:
    """Create or verify the private staging and immutable-version roots."""
    plugins = _ordinary_directory(home / "plugins")
    cache = _ordinary_directory(plugins / "cache")
    staging = plugins / ".cmw-install-staging"
    marketplace = cache / "codex-must-work-local"
    try:
        ensure_private_root(staging)
        ensure_private_root(marketplace)
    except PrivateRootError:
        _fail("cache_path_invalid")
    return create_secure_directory(staging / "codex-must-work"), create_secure_directory(
        marketplace / "codex-must-work"
    )


def check_selection(root: Path, source: str) -> None:
    """Reject local or higher versions while fencing the version-root identity."""
    before = secure_identity(root)
    try:
        candidates = tuple(root.iterdir())
    except OSError:
        _fail("cache_selection_conflict")
    for candidate in candidates:
        require_directory(candidate, "cache_selection_conflict")
        if candidate.name == "local" or higher(candidate.name, source):
            _fail("cache_selection_conflict")
    if secure_identity(root) != before:
        _fail("cache_selection_conflict")


def flush_tree(root: Path, paths: tuple[str, ...]) -> None:
    """Durably flush every file, then each directory from leaves to root."""
    for relative in paths:
        flush_path(root.joinpath(*relative.split("/")))
    directories = (
        root,
        *(root.joinpath(*path.split("/")) for path in expected_directories(paths)),
    )
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        flush_path(directory)


def flush_path(path: Path) -> None:
    """Flush one direct file or directory through the host durability primitive."""
    if os.name == "nt" and path.is_dir():
        flush_windows_directory(path)
        return
    is_file = path.is_file()
    access = os.O_WRONLY if os.name == "nt" and is_file else os.O_RDONLY
    flags = access | (getattr(os, "O_DIRECTORY", 0) if path.is_dir() else 0)
    descriptor = open_direct_file(path, flags) if is_file else os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_no_replace(source: Path, target: Path) -> None:
    """Rename one opened object without ever replacing the destination."""
    if os.name == "nt":
        descriptor = open_windows_path(
            source,
            0x00010000,
            attributes=0x02000000 | 0x00200000,
        )
        try:
            source_identity = identity(os.fstat(descriptor))
            rename_windows_file(descriptor, target, replace=False)
            if identity(os.fstat(descriptor)) != source_identity:
                message = "renamed cache identity changed"
                raise OSError(message)
        finally:
            os.close(descriptor)
        return
    source_parent = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    target_parent = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith("linux"):
            result = cast(
                "int",
                library.renameat2(
                    source_parent,
                    os.fsencode(source.name),
                    target_parent,
                    os.fsencode(target.name),
                    1,
                ),
            )
        elif sys.platform == "darwin":
            result = cast("int", library.renamex_np(os.fsencode(source), os.fsencode(target), 4))
        else:
            _fail("cache_publication_failed")
        if result:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), target)
    finally:
        os.close(target_parent)
        os.close(source_parent)


def same_path(path: Path, expected: CacheIdentity) -> bool:
    """Check that a direct directory still names one expected identity."""
    try:
        metadata = path.lstat()
        return identity(metadata) == expected and stat.S_ISDIR(metadata.st_mode)
    except OSError:
        return False


def remove_tree(root: Path, expected: CacheIdentity) -> None:
    """Quarantine only the expected root, then delete that validated object."""
    if not same_path(root, expected):
        _fail("cache_cleanup_failed")
    parent = root.parent
    try:
        parent_identity = secure_identity(parent)
        quarantine = parent / f".cmw-delete-{secrets.token_hex(16)}"
        rename_no_replace(root, quarantine)
        if not same_path(quarantine, expected) or secure_identity(parent) != parent_identity:
            _fail("cache_cleanup_failed")
        _delete_quarantined(quarantine, expected)
        flush_path(parent)
        if secure_identity(parent) != parent_identity:
            _fail("cache_cleanup_failed")
    except (OSError, UnsafeStatePathError, InstallPluginError):
        _fail("cache_cleanup_failed")


def _delete_quarantined(root: Path, expected: CacheIdentity) -> None:
    if os.name == "nt":
        _delete_windows_tree(root, expected)
        return
    if not same_path(root, expected):
        _fail("cache_cleanup_failed")
    for entry in tuple(os.scandir(root)):
        path = Path(entry.path)
        metadata = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & reparse:
            _fail("cache_cleanup_failed")
        if stat.S_ISDIR(metadata.st_mode):
            _delete_quarantined(path, identity(metadata))
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            opened = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            try:
                if identity(os.fstat(opened)) != identity(path.lstat()):
                    _fail("cache_cleanup_failed")
                path.unlink()
            finally:
                os.close(opened)
        else:
            _fail("cache_cleanup_failed")
    if not same_path(root, expected):
        _fail("cache_cleanup_failed")
    root.rmdir()


def _delete_windows_tree(root: Path, expected: CacheIdentity) -> None:
    descriptor = open_locked(root, delete_access=True)
    try:
        opened = os.fstat(descriptor)
        if identity(opened) != expected or not stat.S_ISDIR(opened.st_mode):
            _fail("cache_cleanup_failed")
        for entry in tuple(os.scandir(root)):
            path = Path(entry.path)
            metadata = path.lstat()
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if getattr(metadata, "st_file_attributes", 0) & reparse:
                _fail("cache_cleanup_failed")
            if stat.S_ISDIR(metadata.st_mode):
                _delete_windows_tree(path, identity(metadata))
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                _fail("cache_cleanup_failed")
            child = open_locked(path, delete_access=True)
            try:
                if identity(os.fstat(child)) != identity(metadata):
                    _fail("cache_cleanup_failed")
                mark_windows_delete(child)
            finally:
                os.close(child)
        if identity(os.fstat(descriptor)) != expected:
            _fail("cache_cleanup_failed")
        mark_windows_delete(descriptor)
    finally:
        os.close(descriptor)


def _ordinary_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        _fail("cache_path_invalid")
    require_directory(path, "cache_path_invalid")
    return path


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
