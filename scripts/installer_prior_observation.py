"""Classify, validate, and compare an installer's prior trusted state."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.hook_trust import trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import (
    retained_cache_matches,
    snapshot_retained_cache,
)
from scripts.installer_observation_config import ConfigObservation, observe_config
from scripts.marketplace_identity import MARKETPLACE_NAME, PLUGIN_NAME

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CacheIdentity
    from scripts.installer_lock import InstallerLease


@dataclass(frozen=True, slots=True)
class PriorState:
    """Carry a prior snapshot and its qualified restore proof."""

    observation: ConfigObservation
    restorable_enabled: bool
    cache_identity: CacheIdentity | None
    cache_digest: str | None


def classify_prior(codex_home: Path, lease: InstallerLease) -> PriorState:
    """Qualify an enabled prior cache for exact later restoration."""
    observed = observe_config(codex_home, lease)
    if observed.plugin_disabled or observed.source_root is None:
        return _unrestorable(observed)
    source = observed.source_root
    expected_parent = (codex_home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME).resolve(
        strict=False
    )
    try:
        named = source.lstat()
        direct = (
            stat.S_ISDIR(named.st_mode)
            and not stat.S_ISLNK(named.st_mode)
            and source.parent.resolve(strict=True) == expected_parent
            and source.resolve(strict=True) == source
        )
    except (OSError, RuntimeError):
        return _unrestorable(observed)
    if not direct:
        return _unrestorable(observed)
    try:
        expected = trusted_hook_states_for_plugin(source, MARKETPLACE_NAME)
        restorable = observed.trusted_hooks == tuple(sorted(expected, key=lambda item: item.key))
        identity, digest = snapshot_retained_cache(source) if restorable else (None, None)
    except (InstallPluginError, OSError):
        identity, digest, restorable = None, None, False
    return PriorState(
        observation=observed,
        restorable_enabled=restorable,
        cache_identity=identity,
        cache_digest=digest,
    )


def prior_cache_still_valid(prior: PriorState) -> bool:
    """Revalidate the complete prior restore proof."""
    source = prior.observation.source_root
    expected = prior.cache_identity
    digest = prior.cache_digest
    if not prior.restorable_enabled or source is None or expected is None or digest is None:
        return False
    if not retained_cache_matches(source, expected, digest):
        return False
    try:
        trusted = trusted_hook_states_for_plugin(source, MARKETPLACE_NAME)
    except InstallPluginError:
        return False
    return prior.observation.trusted_hooks == tuple(sorted(trusted, key=lambda item: item.key))


def observation_matches_prior(observed: ConfigObservation, prior: PriorState) -> bool:
    """Check whether final enabled state exactly matches the prior proof."""
    return (
        not observed.plugin_disabled
        and observed.source_root == prior.observation.source_root
        and observed.trusted_hooks == prior.observation.trusted_hooks
        and prior_cache_still_valid(prior)
    )


def _unrestorable(observed: ConfigObservation) -> PriorState:
    return PriorState(
        observation=observed,
        restorable_enabled=False,
        cache_identity=None,
        cache_digest=None,
    )
