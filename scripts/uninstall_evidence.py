"""Typed configuration ownership evidence derived from validated cache generations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_package import load_package
from scripts.cache_security import read_source
from scripts.hook_trust import trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import (
    LEGACY_MARKETPLACE_NAME,
    LEGACY_PLUGIN_ID,
    MARKETPLACE_NAME,
    MARKETPLACE_REF,
    MARKETPLACE_SOURCE,
    PLUGIN_ID,
)
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.hook_trust import TrustedHookState
    from scripts.install_receipt import InstallReceipt

_OWNERSHIP_UNKNOWN: Final = "uninstall_cache_ownership_unknown"


@dataclass(frozen=True, slots=True)
class ValidatedInstallEvidence:
    """Bind one marketplace identity to its exact source and hook trust values."""

    marketplace_name: str
    plugin_id: str
    source_type: str
    source: str
    reference: str | None
    trusted_hooks: tuple[TrustedHookState, ...]


def receipt_install_evidence(receipt: InstallReceipt) -> ValidatedInstallEvidence:
    """Project protected receipt evidence into exact config ownership values."""
    return ValidatedInstallEvidence(
        MARKETPLACE_NAME,
        PLUGIN_ID,
        "git",
        MARKETPLACE_SOURCE,
        MARKETPLACE_REF,
        receipt.trusted_hooks,
    )


def validated_install_evidence(
    cache_root: Path,
    marketplace: str,
    source_root: Path,
    cache_digest: str,
) -> ValidatedInstallEvidence:
    """Derive exact config values from one already-validated cache generation."""
    if marketplace not in {MARKETPLACE_NAME, LEGACY_MARKETPLACE_NAME}:
        _fail()
    try:
        trusted = trusted_hook_states_for_plugin(cache_root, marketplace)
        known_source = str(source_root.resolve(strict=True))
        source_digest = (
            load_package(source_root, _read_direct).digest
            if marketplace == LEGACY_MARKETPLACE_NAME
            else None
        )
    except (OSError, RuntimeError, InstallPluginError) as error:
        raise InstallPluginError(_OWNERSHIP_UNKNOWN) from error
    if marketplace == MARKETPLACE_NAME:
        return ValidatedInstallEvidence(
            marketplace,
            PLUGIN_ID,
            "git",
            MARKETPLACE_SOURCE,
            MARKETPLACE_REF,
            trusted,
        )
    if source_digest != cache_digest:
        _fail()
    return ValidatedInstallEvidence(
        marketplace,
        LEGACY_PLUGIN_ID,
        "local",
        known_source,
        None,
        trusted,
    )


def _fail() -> Never:
    raise InstallPluginError(_OWNERSHIP_UNKNOWN)


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)
