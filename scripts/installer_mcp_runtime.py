"""Prepare the direct portable-Python entrypoint used by the CMW MCP daemon."""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Never, assert_never

if TYPE_CHECKING:
    from pathlib import Path

from scripts.cache_publication import rename_no_replace
from scripts.cache_security import require_directory, secure_path
from scripts.cache_types import CacheIdentity, identity
from scripts.install_errors import InstallPluginError
from scripts.runtime_cleanup import delete_runtime_tree
from scripts.runtime_tree import (
    RuntimeTreeManifest,
    load_runtime_manifest,
    materialize_archive,
    validate_runtime_tree,
)


class RuntimePlatform(StrEnum):
    """Portable archive layout selected by the installer."""

    WINDOWS = "windows"
    POSIX = "posix"


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """Expected portable archive and direct daemon entrypoint layout."""

    version: str
    archive_name: str
    sha256: str
    platform: RuntimePlatform
    manifest_name: str
    manifest_sha256: str
    exclusion_name: str
    exclusion_sha256: str
    exclusion_count: int


@dataclass(frozen=True, slots=True)
class McpRuntimePublication:
    """Identity-bound portable runtime prepared by one installer run."""

    path: Path
    identity: CacheIdentity
    created_by_run: bool


@dataclass(frozen=True, slots=True)
class _CleanupTarget:
    path: Path
    identity: CacheIdentity
    parent: Path
    prefix: str


_VERSION: Final = "3.12.13+20260510"
_ARCHIVE_HASH_MISMATCH: Final = "portable_runtime_archive_hash_mismatch"
_ARCHIVE_MISSING: Final = "portable_runtime_archive_missing"
_CLEANUP_CONFLICT: Final = "portable_runtime_cleanup_conflict"
_INCOMPLETE: Final = "portable_runtime_incomplete"
_INVALID: Final = "portable_runtime_invalid"
_PUBLICATION_FAILED: Final = "portable_runtime_publication_failed"
_UNSUPPORTED_TARGET: Final = "unsupported_installer_target"
_WINDOWS: Final = RuntimeSpec(
    _VERSION,
    f"cpython-{_VERSION}-windows-x64.tar.gz",
    "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0",
    RuntimePlatform.WINDOWS,
    "windows-x64.json",
    "1781560966f6b86ce88e1ed802e047b4aada257a4f4da78520f991ffe79a9003",
    "windows-x64.json",
    "7bbebb687871c3de69b05de2988393ccc33ecdd209ec5f9cba4937384246ffd3",
    554,
)
_LINUX: Final = RuntimeSpec(
    _VERSION,
    f"cpython-{_VERSION}-linux-x64.tar.gz",
    "d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88",
    RuntimePlatform.POSIX,
    "linux-x64.json",
    "493d6a275d3ae04663fef68c759b2d9217432355c469a0cb5017de509832276f",
    "linux-x64.json",
    "8c5e78d11d454452253f94305563dc24a8082dc53142c9e8366c26080fcb8fa4",
    3,
)
_MACOS: Final = RuntimeSpec(
    _VERSION,
    f"cpython-{_VERSION}-macos-arm64.tar.gz",
    "55bc1a5edbc8ac4da0081f4f5731ed2d1ed10c57cb37a820b2a0dbc7cad742e9",
    RuntimePlatform.POSIX,
    "macos-arm64.json",
    "0fecfcc6f808f12e76cc6ad363645d92e2c13c169e4a074829e159efa7f8d3ac",
    "macos-arm64.json",
    "3f890402ef9d930f1b8176daf91677ddded491416281ee4ef71a88ab5a915f93",
    3,
)
_WRAPPER: Final = (
    '#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexec "$(dirname -- "$0")/bin/python3" -B "$@"\n'
)


def prepare_mcp_runtime(
    source_root: Path,
    data_root: Path,
    spec: RuntimeSpec | None = None,
) -> McpRuntimePublication:
    """Prepare or reuse the direct MCP runtime without a resident shell."""
    require_directory(source_root, "package_source_unsafe")
    require_directory(data_root, "plugin_data_root_invalid")
    selected = _current_spec() if spec is None else spec
    manifest = load_runtime_manifest(
        source_root / "runtime" / "manifests" / selected.manifest_name,
        selected.manifest_sha256,
        source_root / "runtime" / "exclusions" / selected.exclusion_name,
        selected.exclusion_sha256,
        selected.exclusion_count,
    )
    target = data_root / f"portable-python-{selected.version}"
    existing = _existing(target, manifest)
    if existing is not None:
        return existing
    archive = source_root / "runtime" / "archives" / selected.archive_name
    if _sha256(archive) != selected.sha256:
        raise InstallPluginError(_ARCHIVE_HASH_MISMATCH)
    stage = data_root / f".portable-python-stage-{secrets.token_hex(16)}"
    stage_cleanup: _CleanupTarget | None = None
    runtime_cleanup: _CleanupTarget | None = None
    try:
        stage.mkdir(mode=0o700)
        if not secure_path(stage, directory=True, apply=True):
            _fail(_PUBLICATION_FAILED)
        stage_cleanup = _CleanupTarget(
            stage,
            identity(stage.lstat()),
            data_root,
            ".portable-python-stage-",
        )
        materialize_archive(archive, stage, manifest)
        extracted = stage / "python"
        match selected.platform:
            case RuntimePlatform.WINDOWS:
                pass
            case RuntimePlatform.POSIX:
                wrapper = extracted / "python"
                _ = wrapper.write_text(_WRAPPER, encoding="utf-8", newline="\n")
                _ = wrapper.chmod(0o755)
            case _:
                assert_never(selected.platform)
        _ = validate_runtime_tree(extracted, manifest, apply_permissions=True)
        rename_no_replace(extracted, target)
        published_identity = validate_runtime_tree(target, manifest, apply_permissions=False)
        runtime_cleanup = _CleanupTarget(
            target,
            published_identity,
            data_root,
            "portable-python-",
        )
        _remove_tree(stage_cleanup)
        stage_cleanup = None
        return McpRuntimePublication(target, runtime_cleanup.identity, created_by_run=True)
    except InstallPluginError:
        _cleanup_failed_publication(runtime_cleanup, stage_cleanup)
        raise
    except OSError as error:
        _cleanup_failed_publication(runtime_cleanup, stage_cleanup)
        raise InstallPluginError(_PUBLICATION_FAILED) from error


def remove_created_mcp_runtime(
    data_root: Path,
    publication: McpRuntimePublication,
) -> None:
    """Remove only the exact runtime created by the failed transaction."""
    if not publication.created_by_run:
        return
    _remove_tree(
        _CleanupTarget(
            publication.path,
            publication.identity,
            data_root,
            "portable-python-",
        )
    )


def _current_spec() -> RuntimeSpec:
    machine = platform.machine().lower()
    if os.name == "nt" and machine in {"amd64", "x86_64"}:
        return _WINDOWS
    if sys.platform.startswith("linux") and machine == "x86_64":
        return _LINUX
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return _MACOS
    raise InstallPluginError(_UNSUPPORTED_TARGET)


def _existing(
    target: Path,
    manifest: RuntimeTreeManifest,
) -> McpRuntimePublication | None:
    try:
        _ = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallPluginError(_INVALID) from error
    runtime_identity = validate_runtime_tree(target, manifest, apply_permissions=False)
    return McpRuntimePublication(target, runtime_identity, created_by_run=False)


def _sha256(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as error:
        raise InstallPluginError(_ARCHIVE_MISSING) from error


def _remove_tree(target: _CleanupTarget) -> None:
    root = target.path
    if root.parent != target.parent or not root.name.startswith(target.prefix):
        raise InstallPluginError(_CLEANUP_CONFLICT)
    try:
        delete_runtime_tree(root, target.identity)
    except OSError as error:
        raise InstallPluginError(_CLEANUP_CONFLICT) from error


def _cleanup_failed_publication(
    runtime: _CleanupTarget | None,
    stage: _CleanupTarget | None,
) -> None:
    if runtime is not None:
        _remove_tree(runtime)
    if stage is not None:
        _remove_tree(stage)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
