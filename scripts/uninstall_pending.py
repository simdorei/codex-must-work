"""Persist and resume identity-bound uninstall quarantine cleanup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_types import CacheIdentity, identity
from scripts.install_errors import InstallPluginError
from scripts.protected_installer_state import (
    JsonObject,
    JsonValue,
    read_signed_record,
    remove_record,
    write_signed_record,
)
from scripts.uninstall_paths import delete_quarantined_root, restore_quarantined_root
from scripts.uninstall_types import OwnedRoot, QuarantinedRoot

_NAME: Final = "uninstall-pending-v1.json"
_MISSING: Final = "uninstall_pending_missing"
_INVALID: Final = "uninstall_pending_invalid"
_CONFIG_CONFLICT: Final = "uninstall_pending_config_conflict"
_SHA256_LENGTH: Final = 64

if TYPE_CHECKING:
    from scripts.installer_lock import InstallerLease


@dataclass(frozen=True, slots=True)
class PendingUninstall:
    """Bind a config transition to exact randomized quarantine objects."""

    before_digest: str
    after_digest: str
    roots: tuple[QuarantinedRoot, ...]
    cache_count: int
    runtime_count: int
    data_count: int
    phase: str
    purge_data: bool
    completion_data_roots: tuple[OwnedRoot, ...]


@dataclass(frozen=True, slots=True)
class PendingCounts:
    """Count exact root classes for the final machine-readable result."""

    cache: int
    runtime: int
    data: int


@dataclass(frozen=True, slots=True)
class PendingPlan:
    """Carry one complete signed intent across phase publications."""

    before: bytes
    after: bytes
    roots: tuple[QuarantinedRoot, ...]
    counts: PendingCounts
    purge_data: bool
    completion_data_roots: tuple[OwnedRoot, ...]


def write_pending_uninstall(
    lease: InstallerLease,
    plan: PendingPlan,
    phase: str,
) -> None:
    """Sign the complete cleanup plan before configuration publication."""
    payload: JsonObject = {
        "schema": 1,
        "before_digest": _sha256(plan.before),
        "after_digest": _sha256(plan.after),
        "cache_count": plan.counts.cache,
        "runtime_count": plan.counts.runtime,
        "data_count": plan.counts.data,
        "phase": phase,
        "purge_data": plan.purge_data,
        "roots": [
            {
                "original": str(root.original),
                "quarantine": str(root.quarantine),
                "device": root.identity.device,
                "inode": root.identity.inode,
            }
            for root in plan.roots
        ],
        "completion_data_roots": [
            {
                "path": str(root.path),
                "device": root.identity.device,
                "inode": root.identity.inode,
            }
            for root in plan.completion_data_roots
        ],
    }
    write_signed_record(lease, _NAME, payload)


def load_pending_uninstall(lease: InstallerLease) -> PendingUninstall | None:
    """Load and authenticate pending cleanup, returning None when absent."""
    try:
        raw = read_signed_record(lease, _NAME, missing_reason=_MISSING)
    except InstallPluginError as error:
        if error.reason_code in {_MISSING, "uninstall_receipt_reinstall_required"}:
            return None
        raise
    expected = {
        "schema",
        "before_digest",
        "after_digest",
        "cache_count",
        "runtime_count",
        "data_count",
        "phase",
        "purge_data",
        "roots",
        "completion_data_roots",
    }
    if set(raw) != expected or raw["schema"] != 1:
        _fail()
    roots_raw = raw["roots"]
    data_roots_raw = raw["completion_data_roots"]
    if (
        not isinstance(roots_raw, list)
        or not isinstance(data_roots_raw, list)
        or raw["phase"] not in {"prepared", "committed"}
        or not isinstance(raw["purge_data"], bool)
    ):
        _fail()
    return PendingUninstall(
        _digest(raw["before_digest"]),
        _digest(raw["after_digest"]),
        tuple(_root(item) for item in roots_raw),
        _integer(raw["cache_count"]),
        _integer(raw["runtime_count"]),
        _integer(raw["data_count"]),
        raw["phase"],
        raw["purge_data"],
        tuple(_data_root(item) for item in data_roots_raw),
    )


def remove_pending_uninstall(lease: InstallerLease) -> None:
    """Remove the signed pending record after rollback or cleanup completes."""
    remove_record(lease, _NAME)


def current_phase(pending: PendingUninstall, config: bytes) -> str:
    """Classify the exact config bytes as pre-edit or committed."""
    digest = _sha256(config)
    if pending.before_digest == pending.after_digest == digest:
        return "after" if pending.phase == "committed" else "before"
    if digest == pending.before_digest:
        if pending.phase != "prepared":
            raise InstallPluginError(_CONFIG_CONFLICT)
        return "before"
    if digest == pending.after_digest:
        return "after"
    raise InstallPluginError(_CONFIG_CONFLICT)


def rollback_pending(pending: PendingUninstall) -> None:
    """Restore every quarantine in reverse order without touching newcomers."""
    failure: InstallPluginError | None = None
    for root in reversed(pending.roots):
        try:
            state = _root_state(root)
            if state == "moved":
                restore_quarantined_root(root)
            elif state != "untouched":
                _fail()
        except InstallPluginError as error:
            failure = failure or error
    if failure is not None:
        raise failure


def cleanup_pending(pending: PendingUninstall) -> None:
    """Delete only authenticated quarantine identities."""
    for root in pending.roots:
        state = _root_state(root)
        if state in {"cleaned", "cleaned_with_newcomer"}:
            continue
        if state not in {"moved", "moved_with_newcomer"}:
            _fail()
        delete_quarantined_root(root)


def _root(value: JsonValue) -> QuarantinedRoot:
    if not isinstance(value, dict) or set(value) != {
        "original",
        "quarantine",
        "device",
        "inode",
    }:
        _fail()
    original = _path(value["original"])
    quarantine = _path(value["quarantine"])
    expected_identity = CacheIdentity(_integer(value["device"]), _integer(value["inode"]))
    if quarantine.parent != original.parent or not quarantine.name.startswith(".cmw-uninstall-"):
        _fail()
    return QuarantinedRoot(original, quarantine, expected_identity)


def _data_root(value: JsonValue) -> OwnedRoot:
    if not isinstance(value, dict) or set(value) != {"path", "device", "inode"}:
        _fail()
    return OwnedRoot(
        _path(value["path"]),
        CacheIdentity(_integer(value["device"]), _integer(value["inode"])),
    )


def _root_state(root: QuarantinedRoot) -> str:
    original = _optional_identity(root.original)
    quarantine = _optional_identity(root.quarantine)
    if quarantine not in {None, root.identity}:
        _fail()
    if original == root.identity and quarantine is None:
        return "untouched"
    if original is None and quarantine == root.identity:
        return "moved"
    if original not in {None, root.identity} and quarantine == root.identity:
        return "moved_with_newcomer"
    if original is None and quarantine is None:
        return "cleaned"
    if original not in {None, root.identity} and quarantine is None:
        return "cleaned_with_newcomer"
    return _fail()


def _optional_identity(path: Path) -> CacheIdentity | None:
    try:
        return identity(path.lstat())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallPluginError(_INVALID) from error


def _path(value: JsonValue) -> Path:
    if not isinstance(value, str):
        _fail()
    path = Path(value)
    if not path.is_absolute():
        _fail()
    return path


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail()
    return value


def _digest(value: JsonValue) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail()
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail() -> Never:
    raise InstallPluginError(_INVALID)
