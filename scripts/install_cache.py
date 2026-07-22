"""Publish one deterministic, verified Codex plugin cache."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Never

from scripts import cache_publication
from scripts.cache_package import expected_directories as _expected_directories
from scripts.cache_package import load_package
from scripts.cache_publication import check_selection as _check_selection
from scripts.cache_publication import flush_path as _flush_path
from scripts.cache_publication import remove_tree as _remove_tree
from scripts.cache_publication import rename_no_replace as _rename_no_replace
from scripts.cache_security import read_source, require_directory, secure_identity, secure_path
from scripts.cache_security import validate_tree as _security_validate_tree
from scripts.cache_security import write_package as _security_write_package
from scripts.cache_semver import safe_name as _safe_name
from scripts.cache_types import CachePublication
from scripts.cache_types import identity as _identity
from scripts.install_errors import InstallPluginError
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CacheIdentity
    from scripts.cache_types import Package as _Package

__all__ = ["CachePublication", "publish_cache"]


def publish_cache(source_root: Path, codex_home: Path, version: str) -> CachePublication:
    """Stage, verify, and atomically publish one immutable cache."""
    _require_directory(source_root, "package_source_unsafe")
    _require_directory(codex_home, "cache_path_invalid")
    if version == "local" or not _safe_name(version):
        _fail("package_version_invalid")
    package = _package(source_root)
    stage_root, versions = _roots(codex_home)
    target = versions / version
    _check_selection(versions, version)
    existing = _existing_publication(target, package)
    if existing is not None:
        return existing
    staged = _create_stage(stage_root)
    try:
        return _publish_new(staged, target, versions, version, package)
    except (InstallPluginError, OSError) as error:
        _rollback(staged[0], target, staged[1], error)


def _existing_publication(target: Path, package: _Package) -> CachePublication | None:
    if not _path_exists(target):
        return None
    try:
        final_identity = _validate_tree(target, package)
    except InstallPluginError:
        _fail("cache_same_version_mismatch")
    return CachePublication(target, package.digest, created_by_run=False, identity=final_identity)


def _create_stage(stage_root: Path) -> tuple[Path, CacheIdentity]:
    stage_parent_identity = secure_identity(stage_root)
    stage = stage_root / secrets.token_hex(16)
    try:
        stage.mkdir(mode=0o700)
        stage_identity = _identity(stage.lstat())
    except OSError:
        _fail("cache_publication_failed")
    try:
        if not secure_path(stage, directory=True, apply=True):
            _fail("cache_security_invalid")
        if secure_identity(stage_root) != stage_parent_identity:
            _fail("cache_security_invalid")
    except (InstallPluginError, OSError):
        _remove_tree(stage, stage_identity)
        _fail("cache_publication_failed")
    return stage, stage_identity


def _publish_new(
    staged: tuple[Path, CacheIdentity],
    target: Path,
    versions: Path,
    version: str,
    package: _Package,
) -> CachePublication:
    stage, stage_identity = staged
    _write_package(stage, package)
    if _validate_tree(stage, package) != stage_identity:
        _fail("cache_security_invalid")
    _flush_tree(stage, package.paths)
    parent_identity = secure_identity(versions)
    _rename_no_replace(stage, target)
    if not _same_path(target, stage_identity) or _path_exists(stage):
        _fail("cache_publication_failed")
    if secure_identity(versions) != parent_identity:
        _fail("cache_publication_failed")
    final = _validate_tree(target, package)
    if final != stage_identity:
        _fail("cache_security_invalid")
    _check_selection(versions, version)
    _flush_path(versions)
    final = _validate_tree(target, package)
    if final != stage_identity or secure_identity(versions) != parent_identity:
        _fail("cache_security_invalid")
    return CachePublication(target, package.digest, created_by_run=True, identity=final)


def _rollback(
    stage: Path,
    target: Path,
    expected: CacheIdentity,
    error: InstallPluginError | OSError,
) -> Never:
    cleanup = target if _same_path(target, expected) else stage
    if not _same_path(cleanup, expected):
        _fail("cache_cleanup_failed")
    _remove_tree(cleanup, expected)
    if isinstance(error, InstallPluginError):
        raise error
    _fail("cache_publication_failed")


def _package(root: Path) -> _Package:
    return load_package(root, _read_direct)


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)


def _roots(home: Path) -> tuple[Path, Path]:
    return cache_publication.cache_roots(home)


def _write_package(root: Path, package: _Package) -> None:
    _security_write_package(root, package)


def _validate_tree(root: Path, expected: _Package) -> CacheIdentity:
    return _security_validate_tree(root, expected, _package)


def _require_directory(path: Path, reason: str) -> None:
    require_directory(path, reason)


def _flush_tree(root: Path, paths: tuple[str, ...]) -> None:
    for relative in paths:
        _flush_path(root.joinpath(*relative.split("/")))
    directories = (
        root,
        *(root.joinpath(*path.split("/")) for path in _expected_directories(paths)),
    )
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _flush_path(directory)


def _same_path(path: Path, expected: CacheIdentity) -> bool:
    return cache_publication.same_path(path, expected)


def _path_exists(path: Path) -> bool:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
