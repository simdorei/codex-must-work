"""Execute the ordered, rollback-capable CMW install transaction."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from scripts.install_errors import InstallPluginError
from scripts.installer_transaction_types import InstallerOperations, TransactionState

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.installer_lock import InstallerLease
    from scripts.installer_result import InstallResult


def run_install_transaction(  # noqa: C901, PLR0912, PLR0915
    lease: InstallerLease,
    source_root: Path,
    candidate_digest: str,
    ops: InstallerOperations,
) -> InstallResult:
    """Run one mutation transaction through the patchable installer operations."""
    baseline = ops.validate_compatibility(
        lease.home,
        source_root,
        require_plugins=False,
    )
    manifest = ops.validate_manifest(source_root)
    source_trust = ops.trusted_states(source_root)
    prior = ops.classify_prior(lease.home, lease)
    configured = ops.configured_generation(prior)
    target = lease.home / "plugins" / "cache" / ops.marketplace / ops.plugin_name / manifest.version
    if ops.scan_candidate(source_root) != candidate_digest:
        ops.fail("package_candidate_changed")
    transaction = TransactionState(
        publication=None,
        data_publication=None,
        runtime_publication=None,
        owned_data=prior.observation.snapshot.data,
    )
    try:
        data_publication = ops.prepare_data_root(lease.home)
        transaction = replace(transaction, data_publication=data_publication)
        control_key = ops.prepare_control_key(lease, data_publication)
        data_publication = ops.bind_control_key(data_publication, control_key)
        transaction = replace(transaction, data_publication=data_publication)
        runtime_publication = ops.prepare_runtime(source_root, data_publication.path)
        transaction = replace(transaction, runtime_publication=runtime_publication)
        if ops.eligible_no_write(prior, target, source_trust):
            publication = ops.prior_publication(prior)
            transaction = replace(transaction, publication=publication)
            result = ops.no_write_reinstall(
                lease,
                source_root,
                baseline,
                prior,
                publication,
            )
            commit = ops.publish_receipt(
                lease,
                source_root,
                publication,
                runtime_publication,
            )
            return replace(result, warning_code=commit.warning_code)
        disabled = ops.initial_disabled(lease, prior)
        transaction = replace(transaction, owned_data=disabled.snapshot.data)
        if not disabled.plugin_disabled:
            ops.fail("plugin_disable_verification_failed")
        fenced = ops.observe_config(lease.home, lease)
        if not fenced.plugin_disabled or fenced.snapshot.state != disabled.snapshot.state:
            ops.fail("codex_config_concurrent_change")
        publication = ops.publish_cache(source_root, lease.home, manifest.version)
        transaction = replace(
            transaction,
            owned_data=fenced.snapshot.data,
            publication=publication,
        )
        ops.validate_publication(publication, source_root)
        requested = ops.requested_generation(publication)
        selected = ops.select_generation(configured, requested)
        if selected is configured:
            current = ops.configured_generation(prior)
            if current != selected:
                ops.fail("installed_generation_revalidation_failed")
            _ = ops.write_config(
                lease,
                fenced.snapshot,
                prior.observation.snapshot.data,
            )
            _ = ops.validate_compatibility(
                lease.home,
                source_root,
                require_plugins=True,
                expected=baseline,
            )
            ops.require_final_generation(lease, selected)
            commit = ops.publish_receipt(
                lease,
                source_root,
                publication,
                runtime_publication,
            )
            return ops.install_success(commit.warning_code)
        final_trust = ops.trusted_states(publication.cache_path)
        mutation = ops.config_mutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=False,
            disable_legacy=False,
        )
        owned_data = ops.publish_state(lease, mutation)
        transaction = replace(transaction, owned_data=owned_data)
        disabled = ops.observe_published(lease, mutation)
        if not disabled.plugin_disabled:
            ops.fail("plugin_disable_verification_failed")
        _ = ops.validate_compatibility(
            lease.home,
            source_root,
            require_plugins=True,
            expected=baseline,
        )
        ops.validate_publication(publication, source_root)
        mutation = ops.config_mutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=True,
            disable_legacy=False,
        )
        owned_data = ops.publish_state(lease, mutation)
        transaction = replace(transaction, owned_data=owned_data)
        enabled = ops.observe_published(lease, mutation)
        if enabled.plugin_disabled or not ops.cache_matches(
            enabled,
            publication,
            final_trust,
            source_root,
        ):
            ops.fail("enabled_trust_verification_failed")
        _ = ops.validate_compatibility(
            lease.home,
            source_root,
            require_plugins=True,
            expected=baseline,
        )
        checked = ops.observe_config(lease.home, lease)
        if checked.snapshot.state != enabled.snapshot.state:
            ops.fail("codex_config_concurrent_change")
        ops.validate_publication(publication, source_root)
        mutation = ops.config_mutation(
            publication.cache_path,
            final_trust,
            plugin_enabled=True,
            disable_legacy=True,
        )
        owned_data = ops.publish_state(lease, mutation)
        transaction = replace(transaction, owned_data=owned_data)
        final = ops.observe_published(lease, mutation)
        if final.legacy_enabled is True or not ops.cache_matches(
            final,
            publication,
            final_trust,
            source_root,
        ):
            ops.fail("final_install_verification_failed")
        ops.require_final_generation(lease, requested)
        commit = ops.publish_receipt(
            lease,
            source_root,
            publication,
            runtime_publication,
        )
        return ops.install_success(commit.warning_code)
    except InstallPluginError as error:
        return ops.recover_install(
            lease,
            ops.recovery_context(
                prior,
                ops.recovery_state(
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
        return ops.recover_install(
            lease,
            ops.recovery_context(
                prior,
                ops.recovery_state(
                    publication=transaction.publication,
                    data_publication=transaction.data_publication,
                    runtime_publication=transaction.runtime_publication,
                    source_root=source_root,
                    owned_data=transaction.owned_data,
                ),
                "installer_io_failure",
            ),
        )
    except BaseException:  # noqa: BLE001, RUF100  # noqa: BROAD_EXCEPT_OK
        ops.recover_interrupted(lease, source_root, prior, transaction)
        raise
