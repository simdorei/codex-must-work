"""Install and trust CMW as one fail-closed transaction."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.codex_compatibility import CompatibilityResult, validate_codex_compatibility
from scripts.codex_config import ConfigMutation, update_codex_config
from scripts.hook_trust import (
    TRUSTED_HOOK_COUNT,
    TrustedHookState,
    trusted_hook_states_for_plugin,
)
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError
from scripts.install_plugin_cli import run_cli
from scripts.installer_cache_validation import validate_cache_publication
from scripts.installer_data_root import DataRootPublication, prepare_data_root
from scripts.installer_lock import InstallerLease, installer_lock
from scripts.installer_observation import (
    ConfigObservation,
    PriorState,
    cache_matches_observation,
    classify_prior,
    disable_local_plugin_only,
    observe_config,
)
from scripts.installer_preflight import (
    eligible_no_write,
    prior_publication,
    validate_install_preflight,
)
from scripts.installer_recovery import (
    RecoveryState,
    locked_failure,
    recover_install,
    recovery_context,
)
from scripts.installer_result import InstallResult, install_success, unobserved_failure

if TYPE_CHECKING:
    from scripts.cache_types import CachePublication

_MARKETPLACE: Final = "codex-must-work-local"


@dataclass(slots=True)
class _TransactionState:
    publication: CachePublication | None
    data_publication: DataRootPublication | None
    owned_data: bytes


def install(codex_home: Path, source_root: Path) -> InstallResult:
    """Run one installer transaction under one outer lease."""
    if not codex_home.is_absolute() or not source_root.is_absolute():
        return unobserved_failure("installer_path_not_absolute")
    try:
        with installer_lock(codex_home) as lease:
            try:
                return _install_locked(lease, source_root)
            except InstallPluginError as error:
                return locked_failure(lease, error.reason_code)
            except OSError:
                return locked_failure(lease, "installer_io_failure")
    except InstallPluginError as error:
        return unobserved_failure(error.reason_code)
    except OSError:
        return unobserved_failure("installer_io_failure")


def _install_locked(lease: InstallerLease, source_root: Path) -> InstallResult:
    baseline = validate_codex_compatibility(lease.home, source_root, require_plugins=False)
    manifest = validate_install_preflight(lease.home, source_root)
    source_trust = trusted_states(source_root)
    prior = classify_prior(lease.home, lease)
    target = lease.home / "plugins" / "cache" / _MARKETPLACE / "codex-must-work" / manifest.version
    transaction = _TransactionState(None, None, prior.observation.snapshot.data)
    try:
        transaction.data_publication = prepare_data_root(lease.home)
        if eligible_no_write(prior, target, source_trust):
            transaction.publication = prior_publication(prior)
            return _no_write_reinstall(
                lease,
                source_root,
                baseline,
                prior,
                transaction.publication,
            )
        disabled = _initial_disabled_observation(lease, prior)
        transaction.owned_data = disabled.snapshot.data
        if not disabled.plugin_disabled:
            _fail("plugin_disable_verification_failed")
        fenced = observe_config(lease.home, lease)
        if not fenced.plugin_disabled or fenced.snapshot.state != disabled.snapshot.state:
            _fail("codex_config_concurrent_change")
        transaction.owned_data = fenced.snapshot.data
        transaction.publication = publish_cache(source_root, lease.home, manifest.version)
        _validate_publication(transaction.publication, source_root)
        final_trust = trusted_states(transaction.publication.cache_path)
        disabled = _publish_state(
            lease,
            transaction,
            ConfigMutation(
                transaction.publication.cache_path,
                final_trust,
                plugin_enabled=False,
                disable_legacy=False,
            ),
        )
        if not disabled.plugin_disabled:
            _fail("plugin_disable_verification_failed")
        _ = validate_codex_compatibility(
            lease.home,
            source_root,
            require_plugins=True,
            expected=baseline,
        )
        _validate_publication(transaction.publication, source_root)
        enabled = _publish_state(
            lease,
            transaction,
            ConfigMutation(
                transaction.publication.cache_path,
                final_trust,
                plugin_enabled=True,
                disable_legacy=False,
            ),
        )
        if enabled.plugin_disabled or not cache_matches_observation(
            enabled, transaction.publication, final_trust, source_root
        ):
            _fail("enabled_trust_verification_failed")
        _ = validate_codex_compatibility(
            lease.home,
            source_root,
            require_plugins=True,
            expected=baseline,
        )
        checked = observe_config(lease.home, lease)
        if checked.snapshot.state != enabled.snapshot.state:
            _fail("codex_config_concurrent_change")
        _validate_publication(transaction.publication, source_root)
        final = _publish_state(
            lease,
            transaction,
            ConfigMutation(
                transaction.publication.cache_path,
                final_trust,
                plugin_enabled=True,
                disable_legacy=True,
            ),
        )
        if final.legacy_enabled is True or not cache_matches_observation(
            final, transaction.publication, final_trust, source_root
        ):
            _fail("final_install_verification_failed")
        return install_success()
    except InstallPluginError as error:
        return recover_install(
            lease,
            recovery_context(
                prior,
                RecoveryState(
                    transaction.publication,
                    transaction.data_publication,
                    source_root,
                    transaction.owned_data,
                ),
                error.reason_code,
            ),
        )
    except OSError:
        return recover_install(
            lease,
            recovery_context(
                prior,
                RecoveryState(
                    transaction.publication,
                    transaction.data_publication,
                    source_root,
                    transaction.owned_data,
                ),
                "installer_io_failure",
            ),
        )


def _initial_disabled_observation(lease: InstallerLease, prior: PriorState) -> ConfigObservation:
    if prior.observation.plugin_disabled:
        disabled = observe_config(lease.home, lease)
        if disabled.snapshot.state != prior.observation.snapshot.state:
            _fail("codex_config_concurrent_change")
        return disabled
    return disable_local_plugin_only(lease.home, lease)


def _no_write_reinstall(
    lease: InstallerLease,
    source_root: Path,
    baseline: CompatibilityResult,
    prior: PriorState,
    publication: CachePublication,
) -> InstallResult:
    _validate_publication(publication, source_root)
    for _ in range(2):
        _ = validate_codex_compatibility(
            lease.home, source_root, require_plugins=True, expected=baseline
        )
    final = observe_config(lease.home, lease)
    trust = trusted_states(publication.cache_path)
    if not cache_matches_observation(final, publication, trust, source_root):
        _fail("final_install_verification_failed")
    if final.snapshot.state != prior.observation.snapshot.state:
        _fail("codex_config_concurrent_change")
    if final.legacy_enabled is True:
        _fail("final_install_verification_failed")
    return install_success()


def _publish_state(
    lease: InstallerLease,
    transaction: _TransactionState,
    mutation: ConfigMutation,
) -> ConfigObservation:
    transaction.owned_data = update_codex_config(
        lease.home,
        mutation,
        lease,
    )
    observed = observe_config(lease.home, lease)
    expected = tuple(sorted(mutation.trusted_hooks, key=lambda item: item.key))
    if (
        not observed.plugin_present
        or observed.source_root != mutation.source_root
        or observed.plugin_disabled is mutation.plugin_enabled
        or observed.trusted_hooks != expected
        or len(expected) != TRUSTED_HOOK_COUNT
    ):
        _fail("codex_config_publication_failed")
    transaction.owned_data = observed.snapshot.data
    return observed


def _validate_publication(publication: CachePublication, source_root: Path) -> None:
    identity, digest = validate_cache_publication(publication, source_root)
    if identity != publication.identity or digest != publication.digest:
        _fail("cache_publication_revalidation_failed")


def trusted_states(source: Path) -> tuple[TrustedHookState, ...]:
    """Build the exact trust set persisted by the installer."""
    return trusted_hook_states_for_plugin(source, _MARKETPLACE)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def main(argv: list[str] | None = None) -> int:
    """Run the two-path installer command."""
    return run_cli(install, argv)


if __name__ == "__main__":
    raise SystemExit(main())
