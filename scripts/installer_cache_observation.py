"""Observe trusted cache generations and identity-bound publications."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Protocol

from scripts.cache_semver import VersionKey, version_key
from scripts.hook_trust import read_plugin_manifest
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import validate_cache_publication
from scripts.marketplace_identity import MARKETPLACE_NAME, PLUGIN_NAME

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CachePublication
    from scripts.hook_trust import TrustedHookState


class ConfigObservationLike(Protocol):
    """Minimum observation shape required for publication matching."""

    @property
    def plugin_disabled(self) -> bool:
        """Return whether the observed plugin is disabled."""
        ...

    @property
    def source_root(self) -> Path | None:
        """Return the observed configured cache root."""
        ...

    @property
    def trusted_hooks(self) -> tuple[TrustedHookState, ...]:
        """Return the observed trusted hook projection."""
        ...


def selected_cache_root(codex_home: Path) -> Path | None:
    """Select the highest valid direct semver cache generation."""
    versions = codex_home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME
    try:
        metadata = versions.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or versions.resolve(strict=True) != versions
        ):
            return None
        candidates = tuple(versions.iterdir())
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    selected: tuple[VersionKey, Path] | None = None
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
            key = version_key(candidate.name)
            manifest = read_plugin_manifest(candidate)
            direct = (
                key is not None
                and manifest.name == PLUGIN_NAME
                and manifest.version == candidate.name
                and stat.S_ISDIR(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and candidate.resolve(strict=True) == candidate
            )
        except (InstallPluginError, OSError, RuntimeError):
            direct = False
            key = None
        if direct and key is not None and (selected is None or key > selected[0]):
            selected = key, candidate
    return None if selected is None else selected[1]


def cache_matches_observation(
    observed: ConfigObservationLike,
    publication: CachePublication,
    trusted_hooks: tuple[TrustedHookState, ...],
    source_root: Path,
) -> bool:
    """Check enabled trust against one identity-bound publication."""
    if observed.plugin_disabled or observed.source_root != publication.cache_path:
        return False
    try:
        actual, digest = validate_cache_publication(publication, source_root)
    except (OSError, InstallPluginError):
        return False
    return (
        actual == publication.identity
        and digest == publication.digest
        and observed.trusted_hooks == tuple(sorted(trusted_hooks, key=lambda item: item.key))
    )
