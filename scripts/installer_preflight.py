"""Validate immutable installer inputs without creating Codex state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from scripts.cache_security import require_directory
from scripts.cache_semver import higher, safe_name
from scripts.cache_types import CachePublication
from scripts.hook_trust import read_plugin_manifest
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.hook_trust import PluginManifest, TrustedHookState
    from scripts.installer_observation import PriorState

_MARKETPLACE: Final = "codex-must-work-local"
_PLUGIN: Final = "codex-must-work"
_SELECTION_CONFLICT: Final = "cache_selection_conflict"


def validate_install_preflight(codex_home: Path, source_root: Path) -> PluginManifest:
    """Validate the exact package identity and read-only cache selection."""
    manifest = read_plugin_manifest(source_root)
    if (
        manifest.name != _PLUGIN
        or manifest.version == "local"
        or not safe_name(manifest.version)
    ):
        _fail("plugin_manifest_identity_invalid")
    _validate_existing_selection(codex_home, manifest.version)
    return manifest


def _validate_existing_selection(codex_home: Path, version: str) -> None:
    current = codex_home
    for part in ("plugins", "cache", _MARKETPLACE, _PLUGIN):
        current /= part
        try:
            _ = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise InstallPluginError(_SELECTION_CONFLICT) from error
        require_directory(current, _SELECTION_CONFLICT)
    try:
        candidates = tuple(current.iterdir())
    except OSError as error:
        raise InstallPluginError(_SELECTION_CONFLICT) from error
    for candidate in candidates:
        require_directory(candidate, _SELECTION_CONFLICT)
        if candidate.name == "local" or higher(candidate.name, version):
            _fail(_SELECTION_CONFLICT)


def eligible_no_write(
    prior: PriorState,
    target: Path,
    source_trust: tuple[TrustedHookState, ...],
) -> bool:
    """Qualify a byte-identical reinstall without invoking a mutating publisher."""
    return (
        prior.restorable_enabled
        and prior.observation.plugin_present
        and prior.observation.legacy_enabled is not True
        and prior.observation.source_root == target
        and prior.observation.trusted_hooks
        == tuple(sorted(source_trust, key=lambda item: item.key))
    )


def prior_publication(prior: PriorState) -> CachePublication:
    """Materialize the retained cache proof without touching the filesystem."""
    source = prior.observation.source_root
    identity = prior.cache_identity
    digest = prior.cache_digest
    if source is None or identity is None or digest is None:
        _fail("prior_cache_proof_missing")
    return CachePublication(source, digest, created_by_run=False, identity=identity)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
