"""Disable, clean, and conditionally restore failed installer transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts import cache_publication
from scripts.config_metadata import read_config_bytes
from scripts.config_publication import write_config_bytes
from scripts.hook_trust import trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.installer_data_root import DataRootPublication, remove_created_data_root
from scripts.installer_observation import (
    ConfigObservation,
    PriorState,
    cache_matches_observation,
    disable_local_plugin_only,
    observation_matches_prior,
    observe_config,
    prior_cache_still_valid,
)
from scripts.installer_result import InstallResult

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CachePublication
    from scripts.installer_lock import InstallerLease

_MARKETPLACE = "codex-must-work-local"
_EXTERNAL_CONFLICT = "external_config_conflict_after_failure"


@dataclass(frozen=True, slots=True)
class RecoveryState:
    """Bind mutable transaction artifacts needed by fail-closed recovery."""

    publication: CachePublication | None
    data_publication: DataRootPublication | None
    source_root: Path
    owned_data: bytes


@dataclass(frozen=True, slots=True)
class _RecoveryContext:
    prior: PriorState
    state: RecoveryState
    reason: str


def recover_install(
    lease: InstallerLease,
    context: _RecoveryContext,
) -> InstallResult:
    """Return exact final observations after ordered safe recovery."""
    conflict = context.reason == "codex_config_concurrent_change"
    removed = False
    secondary = context.reason if conflict else None
    prior, publication = context.prior, context.state.publication
    data_publication = context.state.data_publication
    try:
        current = observe_config(lease.home, lease)
        if current.snapshot.data != context.state.owned_data:
            conflict = True
            secondary = "codex_config_concurrent_change"
        if not current.plugin_disabled:
            current = disable_local_plugin_only(lease.home, lease)
        owned_disabled = current.snapshot
        removed, data_removed, cleanup_error = _cleanup_created(publication, data_publication)
        if cleanup_error is not None:
            conflict = True
            secondary = cleanup_error
        cleanup_complete = (
            publication is None or not publication.created_by_run or removed
        ) and data_removed
        prior_safe = prior.observation.plugin_disabled or (
            prior.restorable_enabled and prior_cache_still_valid(prior)
        )
        if not conflict and prior_safe and cleanup_complete:
            current_snapshot = read_config_bytes(lease.home, lease)
            if current_snapshot.state != owned_disabled.state:
                conflict = True
                secondary = "codex_config_concurrent_change"
            else:
                _ = write_config_bytes(lease, current_snapshot, prior.observation.snapshot.data)
    except InstallPluginError as error:
        conflict = True
        secondary = error.reason_code
    except OSError:
        conflict = True
        secondary = "installer_io_failure_after_failure"
    final, observation_error = _last_observation(lease)
    secondary = observation_error or secondary
    cache_match = _final_cache_match(final, publication, context.state.source_root, prior)
    return InstallResult(
        install_ok=False,
        error_code=_EXTERNAL_CONFLICT if conflict else context.reason,
        final_plugin_disabled=final.plugin_disabled if final is not None else False,
        final_cache_matches_enabled_trust=cache_match,
        created_cache_removed=removed,
        external_config_conflict_after_failure=conflict,
        secondary_error_code=secondary,
    )


def locked_failure(lease: InstallerLease, reason: str) -> InstallResult:
    """Observe a failure that occurred before transaction state existed."""
    observed, secondary = _last_observation(lease)
    return InstallResult(
        install_ok=False,
        error_code=reason,
        final_plugin_disabled=observed.plugin_disabled if observed is not None else False,
        final_cache_matches_enabled_trust=False,
        created_cache_removed=False,
        secondary_error_code=secondary,
    )


def _last_observation(lease: InstallerLease) -> tuple[ConfigObservation | None, str | None]:
    try:
        return observe_config(lease.home, lease), None
    except InstallPluginError as error:
        return None, error.reason_code
    except OSError:
        return None, "installer_final_state_read_failed"


def _final_cache_match(
    final: ConfigObservation | None,
    publication: CachePublication | None,
    source_root: Path,
    prior: PriorState,
) -> bool:
    if final is None:
        return False
    if publication is not None:
        try:
            trust = trusted_hook_states_for_plugin(publication.cache_path, _MARKETPLACE)
            if cache_matches_observation(final, publication, trust, source_root):
                return True
        except (InstallPluginError, OSError):
            pass
    return observation_matches_prior(final, prior)


def recovery_context(
    prior: PriorState,
    state: RecoveryState,
    reason: str,
) -> _RecoveryContext:
    """Bind recovery inputs without expanding the mutation callback boundary."""
    return _RecoveryContext(prior, state, reason)


def _cleanup_created(
    publication: CachePublication | None,
    data_publication: DataRootPublication | None,
) -> tuple[bool, bool, str | None]:
    removed = False
    data_removed = data_publication is None or not data_publication.created_by_run
    error_code: str | None = None
    if publication is not None and publication.created_by_run:
        try:
            cache_publication.remove_tree(publication.cache_path, publication.identity)
            removed = True
        except InstallPluginError as error:
            error_code = error.reason_code
    if data_publication is not None and data_publication.created_by_run:
        try:
            remove_created_data_root(data_publication)
            data_removed = True
        except InstallPluginError as error:
            error_code = error.reason_code
    return removed, data_removed, error_code
