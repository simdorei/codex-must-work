"""Validate data roots bound to uninstall completion trust."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final, Never

from scripts.cache_types import identity
from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import (
    DATA_ROOT_NAME,
    LEGACY_MARKETPLACE_NAME,
    PLUGIN_NAME,
)
from scripts.private_root import PrivateRootError, ensure_private_root
from scripts.state_io import UnsafeStatePathError, ensure_existing_components_are_direct
from scripts.uninstall_types import OwnedRoot

_LEGACY_DATA_ROOT: Final = f"{PLUGIN_NAME}-{LEGACY_MARKETPLACE_NAME}"
_DATA_ROOTS: Final = (DATA_ROOT_NAME, _LEGACY_DATA_ROOT)
_MARKER: Final = ".private-root-v1"
_MARKER_BYTES: Final = b"private-root-v1\n"
_UNKNOWN: Final = "uninstall_data_ownership_unknown"


def planned_data_roots(home: Path) -> tuple[OwnedRoot, ...]:
    """Validate exact private CMW data roots selected by explicit purge."""
    return tuple(_planned(path) for path in _candidates(home) if _exists(path))


def validate_bound_data_roots(
    home: Path,
    bound: tuple[OwnedRoot, ...],
) -> tuple[OwnedRoot, ...]:
    """Revalidate only completion-bound data identities for a later purge."""
    candidates = set(_candidates(home))
    if len(bound) != len({root.path for root in bound}) or any(
        root.path not in candidates for root in bound
    ):
        _fail()
    by_path = {root.path: root for root in bound}
    planned: list[OwnedRoot] = []
    for path in candidates:
        if not _exists(path):
            continue
        expected = by_path.get(path)
        if expected is None:
            _fail()
        current = _planned(path)
        if current.identity != expected.identity:
            _fail()
        planned.append(current)
    return tuple(planned)


def _candidates(home: Path) -> tuple[Path, ...]:
    parent = home / "plugins" / "data"
    return (*(parent / name for name in _DATA_ROOTS), home / PLUGIN_NAME)


def _planned(root: Path) -> OwnedRoot:
    _require_direct_directory(root)
    try:
        ensure_private_root(root)
        marker = root / _MARKER
        metadata = marker.lstat()
        contents = marker.read_bytes()
    except (OSError, PrivateRootError) as error:
        raise InstallPluginError(_UNKNOWN) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or getattr(metadata, "st_file_attributes", 0) & reparse
        or contents != _MARKER_BYTES
    ):
        _fail()
    _require_safe_tree(root)
    return OwnedRoot(root, identity(root.lstat()))


def _require_direct_directory(path: Path) -> None:
    try:
        ensure_existing_components_are_direct(Path(path.anchor), path)
        metadata = path.lstat()
        direct = path.resolve(strict=True) == path
    except (OSError, RuntimeError, UnsafeStatePathError) as error:
        raise InstallPluginError(_UNKNOWN) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not path.is_absolute()
        or not direct
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse
    ):
        _fail()


def _require_safe_tree(root: Path) -> None:
    expected = identity(root.lstat())
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            for name in directories:
                _require_direct_directory(Path(current) / name)
            for name in files:
                metadata = (Path(current) / name).lstat()
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or getattr(metadata, "st_file_attributes", 0) & reparse
                ):
                    _fail()
    except OSError as error:
        raise InstallPluginError(_UNKNOWN) from error
    if identity(root.lstat()) != expected:
        _fail()


def _exists(path: Path) -> bool:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise InstallPluginError(_UNKNOWN) from error
    return True


def _fail() -> Never:
    raise InstallPluginError(_UNKNOWN)
