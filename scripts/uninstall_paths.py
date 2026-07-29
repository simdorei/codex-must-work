"""Validate, quarantine, and delete only CMW-owned filesystem roots."""

from __future__ import annotations

import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_publication import rename_no_replace
from scripts.cache_types import identity
from scripts.hook_trust import read_plugin_manifest
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import snapshot_retained_cache
from scripts.installer_mcp_runtime import current_runtime_spec
from scripts.marketplace_identity import MARKETPLACE_NAME, PLUGIN_NAME
from scripts.runtime_cleanup import delete_runtime_tree
from scripts.runtime_tree import load_runtime_manifest, validate_runtime_tree
from scripts.state_io import UnsafeStatePathError, ensure_existing_components_are_direct
from scripts.uninstall_evidence import ValidatedInstallEvidence, receipt_install_evidence
from scripts.uninstall_types import OwnedRoot, QuarantinedRoot

if TYPE_CHECKING:
    from scripts.install_receipt import InstallReceipt

_OWNERSHIP_UNKNOWN: Final = "uninstall_cache_ownership_unknown"
_DELETE_RACE: Final = "uninstall_delete_race"
_RUNTIME_OWNERSHIP_UNKNOWN: Final = "uninstall_runtime_ownership_unknown"
_ROLLBACK_FAILED: Final = "uninstall_rollback_failed"


@dataclass(frozen=True, slots=True)
class CacheRemovalPlan:
    """Carry validated cache roots and the exact config evidence they prove."""

    roots: tuple[OwnedRoot, ...]
    evidence: tuple[ValidatedInstallEvidence, ...]


def quarantine_no_replace(source: Path, target: Path) -> None:
    """Move a validated root to an unused quarantine name."""
    rename_no_replace(source, target)


def planned_cache_generations(home: Path, receipt: InstallReceipt) -> CacheRemovalPlan:
    """Plan only the exact canonical cache authenticated by the protected receipt."""
    expected_parent = home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME
    if receipt.cache_path.parent != expected_parent:
        _fail(_OWNERSHIP_UNKNOWN)
    _require_direct_directory(receipt.cache_path, _OWNERSHIP_UNKNOWN)
    generation_identity, generation_digest = snapshot_retained_cache(receipt.cache_path)
    manifest = read_plugin_manifest(receipt.cache_path)
    if (
        manifest.name != PLUGIN_NAME
        or manifest.version != receipt.cache_version
        or generation_identity != receipt.cache_identity
        or generation_digest != receipt.package_digest
    ):
        _fail(_OWNERSHIP_UNKNOWN)
    root = OwnedRoot(receipt.cache_path, receipt.cache_identity)
    return CacheRemovalPlan((root,), (receipt_install_evidence(receipt),))


def planned_runtime_roots(
    home: Path,
    source_root: Path,
    receipt: InstallReceipt,
) -> tuple[OwnedRoot, ...]:
    """Plan only the exact runtime already authenticated by the receipt loader."""
    data_parent = home / "plugins" / "data"
    if receipt.runtime_path.parent.parent != data_parent:
        _fail(_RUNTIME_OWNERSHIP_UNKNOWN)
    _require_direct_directory(receipt.runtime_path, _RUNTIME_OWNERSHIP_UNKNOWN)
    spec = current_runtime_spec()
    try:
        manifest = load_runtime_manifest(
            source_root / "runtime" / "manifests" / spec.manifest_name,
            spec.manifest_sha256,
            source_root / "runtime" / "exclusions" / spec.exclusion_name,
            spec.exclusion_sha256,
            spec.exclusion_count,
        )
        runtime_identity = validate_runtime_tree(
            receipt.runtime_path,
            manifest,
            apply_permissions=False,
        )
    except InstallPluginError as error:
        raise InstallPluginError(_RUNTIME_OWNERSHIP_UNKNOWN) from error
    if runtime_identity != receipt.runtime_identity or receipt.runtime_generation != spec.version:
        _fail(_RUNTIME_OWNERSHIP_UNKNOWN)
    return (OwnedRoot(receipt.runtime_path, receipt.runtime_identity),)


def delete_owned_root(root: OwnedRoot) -> None:
    """Quarantine one validated root, recheck identity, then delete it."""
    path = root.path
    parent = path.parent
    quarantine = parent / f".cmw-uninstall-{secrets.token_hex(16)}"
    try:
        _require_direct_directory(parent, _DELETE_RACE)
        parent_identity = identity(parent.lstat())
        if identity(path.lstat()) != root.identity:
            _fail(_DELETE_RACE)
        quarantine_no_replace(path, quarantine)
        if (
            identity(quarantine.lstat()) != root.identity
            or identity(parent.lstat()) != parent_identity
            or _exists(path)
        ):
            _fail(_DELETE_RACE)
        delete_runtime_tree(quarantine, root.identity)
    except InstallPluginError:
        raise
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_DELETE_RACE) from error


def plan_quarantine(root: OwnedRoot) -> QuarantinedRoot:
    """Reserve an unused quarantine name before the signed WAL is published."""
    path = root.path
    parent = path.parent
    quarantine = parent / f".cmw-uninstall-{secrets.token_hex(16)}"
    try:
        _require_direct_directory(parent, _DELETE_RACE)
        if identity(path.lstat()) != root.identity:
            _fail(_DELETE_RACE)
        if _exists(quarantine):
            _fail(_DELETE_RACE)
        return QuarantinedRoot(path, quarantine, root.identity)
    except InstallPluginError:
        raise
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_DELETE_RACE) from error


def quarantine_owned_root(root: QuarantinedRoot) -> QuarantinedRoot:
    """Execute one identity-bound rename already authorized by the signed WAL."""
    try:
        _require_direct_directory(root.original.parent, _DELETE_RACE)
        parent_identity = identity(root.original.parent.lstat())
        if identity(root.original.lstat()) != root.identity or _exists(root.quarantine):
            _fail(_DELETE_RACE)
        quarantine_no_replace(root.original, root.quarantine)
        if (
            identity(root.quarantine.lstat()) != root.identity
            or identity(root.original.parent.lstat()) != parent_identity
        ):
            _fail(_DELETE_RACE)
    except InstallPluginError:
        raise
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_DELETE_RACE) from error
    else:
        return root


def restore_quarantined_root(root: QuarantinedRoot) -> None:
    """Restore one quarantine only when its original name remains unused."""
    try:
        if _exists(root.original):
            _fail("uninstall_rollback_conflict")
        if identity(root.quarantine.lstat()) != root.identity:
            _fail("uninstall_rollback_conflict")
        quarantine_no_replace(root.quarantine, root.original)
        if identity(root.original.lstat()) != root.identity:
            _fail("uninstall_rollback_conflict")
    except InstallPluginError:
        raise
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_ROLLBACK_FAILED) from error


def delete_quarantined_root(root: QuarantinedRoot) -> None:
    """Delete only the identity still bound to its randomized quarantine name."""
    try:
        if identity(root.quarantine.lstat()) != root.identity:
            _fail(_DELETE_RACE)
        delete_runtime_tree(root.quarantine, root.identity)
    except InstallPluginError:
        raise
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_DELETE_RACE) from error


def _require_direct_directory(path: Path, reason: str) -> None:
    if not path.is_absolute():
        _fail(reason)
    try:
        ensure_existing_components_are_direct(Path(path.anchor), path)
        metadata = path.lstat()
        direct = path.resolve(strict=True) == path
    except (OSError, RuntimeError, UnsafeStatePathError) as error:
        raise InstallPluginError(reason) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not direct
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse
    ):
        _fail(reason)


def _exists(path: Path) -> bool:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise InstallPluginError(_DELETE_RACE) from error
    return True


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
