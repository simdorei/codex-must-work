"""Install and trust CMW as one fail-closed transaction."""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.codex_compatibility import CompatibilityResult, validate_codex_compatibility
from scripts.codex_config import ConfigMutation, update_codex_config
from scripts.config_publication import write_config_bytes
from scripts.hook_trust import (
    TRUSTED_HOOK_COUNT,
    TrustedHookState,
    trusted_hook_states_for_plugin,
)
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError
from scripts.install_plugin_cli import run_cli
from scripts.install_receipt import install_receipt_is_committed, publish_install_receipt
from scripts.installed_generation import (
    InstalledGeneration,
    configured_generation,
    requested_generation,
    select_generation,
    validate_requested_manifest,
)
from scripts.installer_cache_validation import validate_cache_publication
from scripts.installer_control_key import prepare_control_key
from scripts.installer_data_root import (
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
    disable_plugin_only,
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
from scripts.installer_transaction import run_install_transaction
from scripts.installer_transaction_types import InstallerOperations, TransactionState
from scripts.marketplace_identity import MARKETPLACE_NAME, PLUGIN_NAME
from scripts.package_secret_scan import scan_package_candidate
from scripts.package_snapshot import package_candidate_snapshot

if TYPE_CHECKING:
    from scripts.cache_types import CachePublication
    from scripts.install_receipt import ReceiptCommit

_MARKETPLACE: Final = MARKETPLACE_NAME


def install(codex_home: Path, source_root: Path) -> InstallResult:
    """Run one installer transaction under one outer lease."""
    if not codex_home.is_absolute() or not source_root.is_absolute():
        return unobserved_failure("installer_path_not_absolute")
    try:
        with package_candidate_snapshot(source_root) as candidate_root:
            candidate_digest = scan_package_candidate(candidate_root)
            return _install_candidate(codex_home, candidate_root, candidate_digest, source_root)
    except InstallPluginError as error:
        return unobserved_failure(error.reason_code)
    except OSError:
        return unobserved_failure("installer_io_failure")


def _install_candidate(
    codex_home: Path,
    candidate_root: Path,
    candidate_digest: str,
    receipt_source_root: Path,
) -> InstallResult:
    try:
        with installer_lock(codex_home) as lease:
            try:
                return _install_locked(lease, candidate_root, candidate_digest, receipt_source_root)
            except InstallPluginError as error:
                return locked_failure(lease, error.reason_code)
            except OSError:
                return locked_failure(lease, "installer_io_failure")
    except InstallPluginError as error:
        return unobserved_failure(error.reason_code)
    except OSError:
        return unobserved_failure("installer_io_failure")


def _install_locked(
    lease: InstallerLease,
    source_root: Path,
    candidate_digest: str,
    receipt_source_root: Path,
) -> InstallResult:
    def publish_receipt(
        active_lease: InstallerLease,
        _candidate_root: Path,
        publication: CachePublication,
        runtime: McpRuntimePublication,
    ) -> ReceiptCommit:
        return publish_install_receipt(active_lease, receipt_source_root, publication, runtime)

    operations = InstallerOperations(
        marketplace=_MARKETPLACE,
        plugin_name=PLUGIN_NAME,
        validate_compatibility=validate_codex_compatibility,
        validate_manifest=validate_requested_manifest,
        trusted_states=trusted_states,
        classify_prior=classify_prior,
        configured_generation=configured_generation,
        scan_candidate=scan_package_candidate,
        fail=_fail,
        prepare_data_root=prepare_data_root,
        prepare_control_key=prepare_control_key,
        bind_control_key=bind_created_control_key,
        prepare_runtime=prepare_mcp_runtime,
        eligible_no_write=eligible_no_write,
        prior_publication=prior_publication,
        no_write_reinstall=_no_write_reinstall,
        publish_receipt=publish_receipt,
        initial_disabled=initial_disabled_observation,
        observe_config=observe_config,
        publish_cache=publish_cache,
        validate_publication=_validate_publication,
        requested_generation=requested_generation,
        select_generation=select_generation,
        write_config=write_config_bytes,
        require_final_generation=_require_final_generation,
        config_mutation=ConfigMutation,
        publish_state=_publish_state,
        observe_published=_observe_published_state,
        cache_matches=cache_matches_observation,
        install_success=install_success,
        recovery_state=RecoveryState,
        recovery_context=recovery_context,
        recover_install=recover_install,
        recover_interrupted=partial(
            _recover_interrupted_install,
            receipt_source_root=receipt_source_root,
        ),
    )
    return run_install_transaction(lease, source_root, candidate_digest, operations)


def _recover_interrupted_install(
    lease: InstallerLease,
    source_root: Path,
    prior: PriorState,
    transaction: TransactionState,
    *,
    receipt_source_root: Path,
) -> None:
    publication = transaction.publication
    runtime = transaction.runtime_publication
    if (
        publication is not None
        and runtime is not None
        and install_receipt_is_committed(
            lease,
            receipt_source_root,
            publication,
            runtime,
        )
    ):
        return
    _ = recover_install(
        lease,
        recovery_context(
            prior,
            RecoveryState(
                publication=publication,
                data_publication=transaction.data_publication,
                runtime_publication=runtime,
                source_root=source_root,
                owned_data=transaction.owned_data,
            ),
            "install_interrupted_before_receipt_commit",
        ),
    )


def initial_disabled_observation(lease: InstallerLease, prior: PriorState) -> ConfigObservation:
    """Return a freshly fenced disabled observation for one transaction."""
    if prior.observation.plugin_disabled:
        disabled = observe_config(lease.home, lease)
        if disabled.snapshot.state != prior.observation.snapshot.state:
            _fail("codex_config_concurrent_change")
        return disabled
    return disable_plugin_only(lease.home, lease)


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
