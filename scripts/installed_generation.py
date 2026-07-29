"""Select only verified configured and requested plugin generations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_package import load_package
from scripts.cache_security import read_source, require_directory
from scripts.cache_semver import VersionKey, version_key
from scripts.cache_types import identity
from scripts.hook_trust import read_plugin_manifest, trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import retained_cache_matches
from scripts.installer_lock import installer_lock
from scripts.installer_observation import PriorState, classify_prior
from scripts.marketplace_identity import MARKETPLACE_NAME, PLUGIN_NAME
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CacheIdentity, CachePublication
    from scripts.hook_trust import PluginManifest

_MARKETPLACE: Final = MARKETPLACE_NAME
_PUBLIC_MARKETPLACE: Final = MARKETPLACE_NAME
_PLUGIN: Final = PLUGIN_NAME


@dataclass(frozen=True, slots=True)
class InstalledGeneration:
    """Bind the immutable identity used to select one installed generation."""

    version: str
    root: Path
    digest: str
    identity: CacheIdentity


def validate_requested_manifest(source_root: Path) -> PluginManifest:
    """Require the requested package identity and one canonical semantic version."""
    manifest = read_plugin_manifest(source_root)
    if manifest.name != _PLUGIN:
        _fail("plugin_manifest_identity_invalid")
    _ = _canonical_key(manifest.version)
    return manifest


def requested_generation(publication: CachePublication) -> InstalledGeneration:
    """Bind a fully revalidated requested cache to its canonical manifest version."""
    manifest = read_plugin_manifest(publication.cache_path)
    _require_manifest_identity(manifest.name, manifest.version, publication.cache_path)
    return InstalledGeneration(
        manifest.version,
        publication.cache_path,
        publication.digest,
        publication.identity,
    )


def configured_generation(prior: PriorState) -> InstalledGeneration | None:
    """Qualify only a current enabled config receipt backed by a valid direct cache."""
    source = prior.observation.source_root
    identity = prior.cache_identity
    digest = prior.cache_digest
    if (
        not prior.restorable_enabled
        or prior.observation.legacy_enabled is True
        or source is None
        or identity is None
        or digest is None
    ):
        return None
    if not retained_cache_matches(source, identity, digest):
        return None
    manifest = read_plugin_manifest(source)
    _require_manifest_identity(manifest.name, manifest.version, source)
    expected_trust = tuple(
        sorted(
            trusted_hook_states_for_plugin(source, _MARKETPLACE),
            key=lambda item: item.key,
        )
    )
    if prior.observation.trusted_hooks != expected_trust:
        return None
    return InstalledGeneration(manifest.version, source, digest, identity)


def require_session_generation(codex_home: Path, plugin_root: Path) -> InstalledGeneration:
    """Reject SessionStart unless the hook root is the configured successful generation."""
    public_generation = _public_marketplace_generation(codex_home, plugin_root)
    if public_generation is not None:
        return public_generation
    with installer_lock(codex_home) as lease:
        generation = configured_generation(classify_prior(codex_home, lease))
    try:
        active_root = plugin_root.resolve(strict=True)
    except (OSError, RuntimeError):
        _fail("installed_generation_mismatch")
    if generation is None or generation.root != active_root:
        _fail("installed_generation_mismatch")
    return generation


def _public_marketplace_generation(
    codex_home: Path,
    plugin_root: Path,
) -> InstalledGeneration | None:
    expected_parent = codex_home.resolve() / "plugins" / "cache" / _PUBLIC_MARKETPLACE / _PLUGIN
    try:
        active_root = plugin_root.resolve(strict=True)
    except (OSError, RuntimeError):
        _fail("installed_generation_mismatch")
    if active_root.parent != expected_parent:
        return None
    require_directory(active_root, "installed_generation_mismatch")
    manifest = read_plugin_manifest(active_root)
    _require_manifest_identity(manifest.name, manifest.version, active_root)
    package = load_package(active_root, _read_direct)
    return InstalledGeneration(
        manifest.version,
        active_root,
        package.digest,
        identity(active_root.lstat()),
    )


def select_generation(
    configured: InstalledGeneration | None,
    requested: InstalledGeneration,
) -> InstalledGeneration:
    """Select one verified generation without consulting ambient cache entries."""
    requested_key = _canonical_key(requested.version)
    if configured is None:
        return requested
    configured_key = _canonical_key(configured.version)
    if configured_key > requested_key:
        return configured
    if configured_key < requested_key:
        return requested
    if configured.digest != requested.digest:
        _fail("installed_generation_conflict")
    return configured


def _require_manifest_identity(name: str, version: str, root: Path) -> None:
    if name != _PLUGIN or version != root.name:
        _fail("installed_generation_manifest_invalid")
    _ = _canonical_key(version)


def _canonical_key(version: str) -> VersionKey:
    parsed = version_key(version)
    if parsed is None:
        _fail("installed_generation_version_invalid")
    return parsed


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
