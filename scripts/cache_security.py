"""Source-safe copying and exact immutable-cache metadata."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Final, Never

from scripts.cache_package import expected_directories
from scripts.cache_types import CacheIdentity, Package, identity
from scripts.cache_windows import secure_windows_path
from scripts.install_errors import InstallPluginError
from scripts.state_io import UnsafeStatePathError, ensure_existing_components_are_direct
from scripts.state_io import open_direct_file as state_open_direct_file

_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
type DirectOpen = Callable[[Path, int], int]
type PackageLoader = Callable[[Path], Package]


def require_directory(path: Path, reason: str) -> None:
    """Require one absolute direct directory with no redirecting component."""
    if not path.is_absolute():
        _fail(reason)
    try:
        ensure_existing_components_are_direct(Path(path.anchor), path)
        metadata = path.lstat()
    except (OSError, UnsafeStatePathError):
        _fail(reason)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        _fail(reason)


def read_source(path: Path, reason: str, direct_open: DirectOpen) -> bytes:
    """Read a stable direct single-link source without imposing cache modes."""
    try:
        parents_before = _parent_identities(path)
        with os.fdopen(direct_open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)), "rb") as handle:
            data = handle.read()
            opened = os.fstat(handle.fileno())
            named = path.lstat()
            parents_after = _parent_identities(path)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        unsafe = (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or identity(opened) != identity(named)
            or parents_before != parents_after
            or bool(getattr(opened, "st_file_attributes", 0) & reparse)
        )
    except (OSError, UnsafeStatePathError):
        _fail(reason)
    if unsafe:
        _fail(reason)
    return data


def create_secure_directory(path: Path) -> Path:
    """Create one cache directory or require its pre-existing exact policy."""
    created = False
    try:
        path.mkdir(mode=_DIRECTORY_MODE)
        created = True
    except FileExistsError:
        pass
    if not secure_path(path, directory=True, apply=created):
        _fail("cache_path_invalid")
    return path


def write_package(root: Path, package: Package) -> None:
    """Create all manifest objects exclusively beneath a verified stage root."""
    for relative in expected_directories(package.paths):
        path = root.joinpath(*relative.split("/"))
        _ = create_secure_directory(path)
    for relative, data in package.files:
        path = root.joinpath(*relative.split("/"))
        parent_identity = secure_identity(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags, _FILE_MODE), "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), _FILE_MODE)
            _ = handle.write(data)
            handle.flush()
        if secure_identity(path.parent) != parent_identity:
            _fail("cache_security_invalid")
        if not secure_path(path, directory=False, apply=True):
            _fail("cache_security_invalid")


def validate_tree(root: Path, expected: Package, loader: PackageLoader) -> CacheIdentity:
    """Validate exact names, metadata, bytes, and root identity as one snapshot."""
    root_before = secure_identity(root)
    files, directories = scan_tree(root)
    if files != expected.paths or directories != expected_directories(expected.paths):
        _fail("cache_security_invalid")
    for relative in (*directories, *files):
        path = root.joinpath(*relative.split("/"))
        if not secure_path(path, directory=relative in directories, apply=False):
            _fail("cache_security_invalid")
    actual = loader(root)
    root_after = secure_identity(root)
    changed = (
        actual.paths != expected.paths
        or actual.digest != expected.digest
        or root_after != root_before
    )
    if changed:
        _fail("cache_security_invalid")
    return root_after


def scan_tree(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Enumerate a direct tree while fencing its root identity."""
    root_identity = secure_identity(root)
    files: list[str] = []
    directories: list[str] = []
    for current_text, names, filenames in os.walk(root, followlinks=False):
        current = Path(current_text)
        if not secure_path(current, directory=True, apply=False):
            _fail("cache_security_invalid")
        for name in names:
            path = current / name
            require_directory(path, "cache_security_invalid")
            directories.append(path.relative_to(root).as_posix())
        files.extend((current / name).relative_to(root).as_posix() for name in filenames)
    if secure_identity(root) != root_identity:
        _fail("cache_security_invalid")
    return tuple(sorted(files, key=str.encode)), tuple(sorted(directories, key=str.encode))


def secure_identity(path: Path) -> CacheIdentity:
    """Return the identity of one exact-policy cache directory."""
    if not secure_path(path, directory=True, apply=False):
        _fail("cache_security_invalid")
    return identity(path.lstat())


def secure_path(path: Path, *, directory: bool, apply: bool) -> bool:
    """Apply or verify the host's exact staged/final cache policy."""
    try:
        if os.name == "nt":
            return secure_windows_path(path, directory=directory, apply=apply)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(path, flags)
        else:
            descriptor = state_open_direct_file(path, flags)
        try:
            if apply:
                os.fchmod(descriptor, _DIRECTORY_MODE if directory else _FILE_MODE)
            opened, named = os.fstat(descriptor), path.lstat()
            expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
            mode = _DIRECTORY_MODE if directory else _FILE_MODE
            return (
                expected_kind(opened.st_mode)
                and (directory or opened.st_nlink == 1)
                and identity(opened) == identity(named)
                and opened.st_uid == os.geteuid()
                and stat.S_IMODE(opened.st_mode) == mode
                and not os.listxattr(descriptor)
            )
        finally:
            os.close(descriptor)
    except (OSError, UnsafeStatePathError):
        return False


def _parent_identities(path: Path) -> tuple[CacheIdentity, ...]:
    ensure_existing_components_are_direct(Path(path.anchor), path)
    return tuple(identity(parent.lstat()) for parent in path.parents)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
