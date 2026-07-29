"""Persist and validate the protected trust anchor for one CMW installation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_package import load_package
from scripts.cache_security import read_source
from scripts.cache_types import CachePublication, identity
from scripts.hook_commands import trusted_hook_commands_for_plugin
from scripts.hook_trust import read_plugin_manifest, trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.install_receipt_codec import (
    InstallReceipt,
    ReceiptHook,
    install_receipt_payload,
    parse_install_receipt,
)
from scripts.installer_cache_validation import snapshot_retained_cache
from scripts.installer_mcp_runtime import McpRuntimePublication, current_runtime_spec
from scripts.marketplace_identity import (
    MARKETPLACE_NAME,
    PLUGIN_NAME,
)
from scripts.protected_installer_state import (
    read_signed_record,
    remove_record,
    write_signed_record,
)
from scripts.state_io import open_direct_file
from scripts.uninstall_completion import clear_uninstall_complete

__all__ = ["InstallReceipt", "ReceiptHook"]

_RECEIPT_NAME: Final = "install-receipt-v1.json"
_INVALID: Final = "uninstall_receipt_invalid"
_WRITE_FAILED: Final = "install_receipt_publication_failed"
_CLEANUP_WARNING: Final = "install_completion_cleanup_pending"
if TYPE_CHECKING:
    from pathlib import Path

    from scripts.installer_lock import InstallerLease


@dataclass(frozen=True, slots=True)
class ReceiptCommit:
    """Report the durable commit and any post-commit cleanup warning."""

    warning_code: str | None


def publish_install_receipt(
    lease: InstallerLease,
    source_root: Path,
    publication: CachePublication,
    runtime: McpRuntimePublication,
) -> ReceiptCommit:
    """Commit a signed receipt, then best-effort clear stale completion state."""
    receipt = _receipt_from_live(lease, source_root, publication, runtime)
    try:
        existing = load_install_receipt(lease, source_root)
    except InstallPluginError:
        existing = None
    if existing != receipt:
        try:
            write_signed_record(lease, _RECEIPT_NAME, install_receipt_payload(receipt))
        except (InstallPluginError, OSError):
            if not install_receipt_is_committed(
                lease,
                source_root,
                publication,
                runtime,
            ):
                raise
        committed = load_install_receipt(lease, source_root)
        if committed != receipt:
            _fail(_WRITE_FAILED)
    try:
        clear_uninstall_complete(lease)
    except (InstallPluginError, OSError):
        return ReceiptCommit(_CLEANUP_WARNING)
    return ReceiptCommit(None)


def install_receipt_is_committed(
    lease: InstallerLease,
    source_root: Path,
    publication: CachePublication,
    runtime: McpRuntimePublication,
) -> bool:
    """Probe whether the exact live transaction crossed the receipt commit boundary."""
    try:
        receipt = load_install_receipt(lease, source_root)
    except (InstallPluginError, OSError):
        return False
    return (
        receipt.cache_path == publication.cache_path
        and receipt.cache_identity == publication.identity
        and receipt.package_digest == publication.digest
        and receipt.runtime_path == runtime.path
        and receipt.runtime_identity == runtime.identity
    )


def load_install_receipt(lease: InstallerLease, source_root: Path) -> InstallReceipt:
    """Authenticate the protected receipt, then revalidate every bound live object."""
    receipt = parse_install_receipt(read_signed_record(lease, _RECEIPT_NAME))
    if (
        receipt.source_root != _resolved(source_root)
        or receipt.cache_path.parent.name != PLUGIN_NAME
    ):
        _fail(_INVALID)
    current_identity, current_digest = snapshot_retained_cache(receipt.cache_path)
    manifest = read_plugin_manifest(receipt.cache_path)
    live_hooks = trusted_hook_states_for_plugin(receipt.cache_path, MARKETPLACE_NAME)
    commands = trusted_hook_commands_for_plugin(receipt.cache_path, MARKETPLACE_NAME)
    if (
        current_identity != receipt.cache_identity
        or current_digest != receipt.package_digest
        or manifest.name != PLUGIN_NAME
        or manifest.version != receipt.cache_version
        or receipt.trusted_hooks != live_hooks
        or tuple(item.command for item in receipt.hooks) != tuple(item.command for item in commands)
        or identity(receipt.runtime_path.lstat()) != receipt.runtime_identity
        or receipt.runtime_generation != current_runtime_spec().version
    ):
        _fail(_INVALID)
    return receipt


def remove_install_receipt(lease: InstallerLease) -> None:
    """Remove only the authenticated receipt after cleanup is complete."""
    remove_record(lease, _RECEIPT_NAME)


def _receipt_from_live(
    lease: InstallerLease,
    source_root: Path,
    publication: CachePublication,
    runtime: McpRuntimePublication,
) -> InstallReceipt:
    source = _resolved(source_root)
    cache = _resolved(publication.cache_path)
    expected = lease.home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME
    if cache.parent != expected or runtime.path.parent.parent.name != "data":
        _fail(_WRITE_FAILED)
    cache_identity, digest = snapshot_retained_cache(cache)
    package = load_package(source, _read_direct)
    manifest = read_plugin_manifest(cache)
    states = trusted_hook_states_for_plugin(cache, MARKETPLACE_NAME)
    commands = trusted_hook_commands_for_plugin(cache, MARKETPLACE_NAME)
    if (
        cache_identity != publication.identity
        or digest != publication.digest
        or package.digest != digest
        or manifest.name != PLUGIN_NAME
        or manifest.version != cache.name
        or len(states) != len(commands)
        or identity(runtime.path.lstat()) != runtime.identity
    ):
        _fail(_WRITE_FAILED)
    hooks = tuple(
        ReceiptHook(state.key, command.command, state.trusted_hash)
        for state, command in zip(states, commands, strict=True)
    )
    return InstallReceipt(
        cache,
        manifest.version,
        cache_identity,
        digest,
        source,
        hooks,
        _resolved(runtime.path),
        runtime.identity,
        current_runtime_spec().version,
    )


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InstallPluginError(_INVALID) from error


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
