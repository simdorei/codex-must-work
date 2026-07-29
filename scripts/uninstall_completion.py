"""Signed completion tombstone for idempotent uninstall retries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_types import CacheIdentity
from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import PLUGIN_ID
from scripts.protected_installer_state import (
    JsonObject,
    JsonValue,
    read_signed_record,
    remove_record,
    write_signed_record,
)
from scripts.uninstall_types import OwnedRoot

_COMPLETION_NAME: Final = "uninstall-complete-v1.json"
_SCHEMA: Final = 2
_MISSING: Final = "uninstall_completion_missing"
_INVALID: Final = "uninstall_receipt_invalid"

if TYPE_CHECKING:
    from scripts.installer_lock import InstallerLease


@dataclass(frozen=True, slots=True)
class UninstallCompletion:
    """Authenticate preserved data identities after cache removal."""

    data_roots: tuple[OwnedRoot, ...]
    data_purged: bool


def clear_uninstall_complete(lease: InstallerLease) -> None:
    """Clear stale completion evidence when installation succeeds."""
    remove_record(lease, _COMPLETION_NAME)


def mark_uninstall_complete(
    lease: InstallerLease,
    source_root: Path,
    data_roots: tuple[OwnedRoot, ...],
    *,
    data_purged: bool,
) -> None:
    """Persist a signed tombstone for safe idempotent retries."""
    payload: JsonObject = {
        "schema": _SCHEMA,
        "plugin_id": PLUGIN_ID,
        "source_root": str(_resolved(source_root)),
        "data_purged": data_purged,
        "data_roots": [
            {
                "path": str(root.path),
                "device": root.identity.device,
                "inode": root.identity.inode,
            }
            for root in data_roots
        ],
    }
    write_signed_record(lease, _COMPLETION_NAME, payload)


def load_uninstall_completion(
    lease: InstallerLease,
    source_root: Path,
) -> UninstallCompletion | None:
    """Load exact preserved data identities from a signed completion record."""
    try:
        payload = read_signed_record(lease, _COMPLETION_NAME, missing_reason=_MISSING)
    except InstallPluginError as error:
        if error.reason_code in {_MISSING, "uninstall_receipt_reinstall_required"}:
            return None
        raise
    expected = {"schema", "plugin_id", "source_root", "data_purged", "data_roots"}
    if (
        set(payload) != expected
        or payload["schema"] != _SCHEMA
        or payload["plugin_id"] != PLUGIN_ID
        or payload["source_root"] != str(_resolved(source_root))
        or not isinstance(payload["data_purged"], bool)
        or not isinstance(payload["data_roots"], list)
    ):
        _fail()
    roots = tuple(_data_root(value) for value in payload["data_roots"])
    return UninstallCompletion(roots, payload["data_purged"])


def uninstall_already_complete(lease: InstallerLease, source_root: Path) -> bool:
    """Return whether an authenticated completion record exists."""
    return load_uninstall_completion(lease, source_root) is not None


def _data_root(value: JsonValue) -> OwnedRoot:
    if not isinstance(value, dict) or set(value) != {"path", "device", "inode"}:
        _fail()
    path = value["path"]
    device = value["device"]
    inode = value["inode"]
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
    ):
        _fail()
    return OwnedRoot(Path(path), CacheIdentity(device, inode))


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_INVALID) from error


def _fail() -> Never:
    raise InstallPluginError(_INVALID)
