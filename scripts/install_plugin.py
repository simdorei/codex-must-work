"""Install and trust CMW as one fail-closed transaction.

# noqa: SIZE_OK — one cohesive installer transaction with ordered rollback.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.codex_compatibility import CompatibilityResult, validate_codex_compatibility
from scripts.codex_config import ConfigMutation, update_codex_config
from scripts.config_publication import write_config_bytes
from scripts.control_capability import ControlKeyError, provision_control_key
from scripts.hook_trust import (
    TRUSTED_HOOK_COUNT,
    TrustedHookState,
    trusted_hook_states_for_plugin,
)
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError
from scripts.install_plugin_cli import run_cli
from scripts.installed_generation import (
    InstalledGeneration,
    configured_generation,
    requested_generation,
    select_generation,
    validate_requested_manifest,
)
from scripts.installer_cache_validation import validate_cache_publication
from scripts.installer_data_root import (
    DataRootPublication,
    bind_created_control_key,
    prepare_data_root,
)
from scripts.installer_lock import InstallerLease, installer_lock
from scripts.installer_mcp_runtime import McpRuntimePublication, prepare_mcp_runtime
from scripts.installer_observation import (
    ConfigObservation,
    PriorState,
    cache_matches_observation,
    classify_prior,
    disable_local_plugin_only,
    observe_config,
)
from scripts.installer_preflight import eligible_no_write, prior_publication
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


@dataclass(frozen=True, slots=True)
class _TransactionState:
    publication: CachePublication | None
    data_publication: DataRootPublication | None
    runtime_publication: McpRuntimePublication | None
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


def _install_locked(  # noqa: C901, PLR0915 - transaction order remains visibly fail-closed.
    lease: InstallerLease,
    source_root: Path,
) -> InstallResult:
    baseline = validate_codex_compatibility(lease.home, source_root, require_plugins=False)
    manifest = validate_requested_manifest(source_root)
    source_trust = trusted_states(source_root)
    prior = classify_prior(lease.home, lease)
    configured = configured_generation(prior)
    target = lease.home / "plugins" / "cache" / _MARKETPLACE / "codex-must-work" / manifest.version
    transaction = _TransactionState(
        publication=None,
        data_publication=None,
        runtime_publication=None,
        owned_data=prior.observation.snapshot.data,
    )
    try:
        data_publication = prepare_data_root(lease.home)
        transaction = replace(transaction, data_publication=data_publication)
        control_key = _prepare_control_key(lease, data_publication)
        data_publication = bind_created_control_key(data_publication, control_key)
        transaction = replace(transaction, data_publication=data_publication)
        runtime_publication = prepare_mcp_runtime(
            source_root,
            data_publication.path,
        )
        transaction = replace(
            transaction,
            runtime_publication=runtime_publication,
        )
        if eligible_no_write(prior, target, source_trust):
            publication = prior_publication(prior)
            transaction = replace(transaction, publication=publication)
            return _no_write_reinstall(
                lease,
                source_root,
                baseline,
                prior,
                publication,
            )
        disabled = initial_disabled_observation(lease, prior)
        transaction = replace(transaction, owned_data=disabled.snapshot.data)
        if not disabled.plugin_disabled:
            _fail("plugin_disable_verification_failed")
        fenced = observe_config(lease.home, lease)
        if not fenced.plugin_disabled or fenced.snapshot.state != disabled.snapshot.state:
            _fail("codex_config_concurrent_change")
        publication = publish_cache(source_root, lease.home, manifest.version)
        transaction = replace(transaction, owned_data=fenced.snapshot.data, publication=publication)
        _validate_publication(publication, source_root)
        requested = requested_generation(publication)
        selected = select_generation(configured, requested)
        if selected is configured:
            current = configured_generation(prior)
            if current != selected:
                _fail("installed_generation_revalidation_failed")
            _ = write_config_bytes(
                lease,
                fenced.snapshot,
                prior.observation.snapshot.data,
            )
            _ = validate_codex_compatibility(
                lease.home,
                source_root,
                require_plugins=True,
                expected=baseline,
            )
            _require_final_generation(lease, selected)
            return install_success()
        final_trust = trusted_states(publication.cache_path)
        mutation = ConfigMutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=False,
            disable_legacy=False,
        )
        owned_data = _publish_state(
            lease,
            mutation,
        )
        transaction = replace(transaction, owned_data=owned_data)
        disabled = _observe_published_state(lease, mutation)
        if not disabled.plugin_disabled:
            _fail("plugin_disable_verification_failed")
        _ = validate_codex_compatibility(
            lease.home,
            source_root,
            require_plugins=True,
            expected=baseline,
        )
        _validate_publication(publication, source_root)
        mutation = ConfigMutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=True,
            disable_legacy=False,
        )
        owned_data = _publish_state(
            lease,
            mutation,
        )
        transaction = replace(transaction, owned_data=owned_data)
        enabled = _observe_published_state(lease, mutation)
        if enabled.plugin_disabled or not cache_matches_observation(
            enabled, publication, final_trust, source_root
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
        _validate_publication(publication, source_root)
        mutation = ConfigMutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=True,
            disable_legacy=True,
        )
        owned_data = _publish_state(
            lease,
            mutation,
        )
        transaction = replace(transaction, owned_data=owned_data)
        final = _observe_published_state(lease, mutation)
        if final.legacy_enabled is True or not cache_matches_observation(
            final, publication, final_trust, source_root
        ):
            _fail("final_install_verification_failed")
        _require_final_generation(lease, requested)
        return install_success()
    except InstallPluginError as error:
        return recover_install(
            lease,
            recovery_context(
                prior,
                RecoveryState(
                    publication=transaction.publication,
                    data_publication=transaction.data_publication,
                    runtime_publication=transaction.runtime_publication,
                    source_root=source_root,
                    owned_data=transaction.owned_data,
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
                    publication=transaction.publication,
                    data_publication=transaction.data_publication,
                    runtime_publication=transaction.runtime_publication,
                    source_root=source_root,
                    owned_data=transaction.owned_data,
                ),
                "installer_io_failure",
            ),
        )


def initial_disabled_observation(lease: InstallerLease, prior: PriorState) -> ConfigObservation:
    """Return a freshly fenced disabled observation for one transaction."""
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


def _require_final_generation(lease: InstallerLease, expected: InstalledGeneration) -> None:
    final = configured_generation(classify_prior(lease.home, lease))
    if final != expected:
        _fail("installed_generation_revalidation_failed")


def _publish_state(
    lease: InstallerLease,
    mutation: ConfigMutation,
) -> bytes:
    return update_codex_config(
        lease.home,
        mutation,
        lease,
    )


def _observe_published_state(
    lease: InstallerLease,
    mutation: ConfigMutation,
) -> ConfigObservation:
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
    return observed


def _prepare_control_key(
    lease: InstallerLease,
    publication: DataRootPublication,
) -> bytes:
    try:
        return provision_control_key(
            publication.path,
            lease.home / "codex-must-work",
        )
    except ControlKeyError as error:
        _fail(error.reason_code)


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
