from __future__ import annotations

from dataclasses import dataclass
from typing import Final

SUBJECTS: Final = (
    "feat(installer): compute exact platform hook trust",
    "feat(installer): isolate Codex config updates",
    "feat(installer): publish verified plugin cache",
    "feat(installer): install and trust CMW atomically",
    "feat(installer): add portable install entrypoints",
    "docs(installer): make trusted setup the default",
    "perf(runtime): reduce hook and watcher overhead",
    "fix(installer): accept normalized marketplace output",
)


def _paths(value: str) -> frozenset[str]:
    return frozenset(value.strip().splitlines())


@dataclass(frozen=True, slots=True)
class CommitRule:
    subject: str
    paths: frozenset[str]
    modified: frozenset[str] = frozenset()


RULES: Final = (
    CommitRule(
        SUBJECTS[0],
        _paths("""
hooks/hooks.json
scripts/install_errors.py
scripts/hook_trust.py
tests/test_portable_runtime.py
tests/test_hook_trust.py
"""),
        frozenset({"hooks/hooks.json", "tests/test_portable_runtime.py"}),
    ),
    CommitRule(
        SUBJECTS[1],
        _paths("""
scripts/codex_config.py
scripts/config_metadata.py
scripts/config_publication.py
scripts/installer_lock.py
scripts/windows_file.py
tests/test_codex_config.py
tests/test_config_metadata.py
tests/test_config_publication.py
tests/test_installer_lock.py
"""),
    ),
    CommitRule(
        SUBJECTS[2],
        _paths("""
runtime/package-files.json
scripts/cache_package.py
scripts/cache_publication.py
scripts/cache_security.py
scripts/cache_semver.py
scripts/cache_types.py
scripts/cache_windows.py
scripts/install_cache.py
tests/fixtures/package-files-base-0352.txt
tests/test_cache_cleanup.py
tests/test_cache_digest.py
tests/test_cache_metadata.py
tests/test_cache_semver.py
tests/test_cache_source.py
tests/test_install_cache.py
"""),
    ),
    CommitRule(
        SUBJECTS[3],
        _paths("""
scripts/codex_compatibility.py
scripts/codex_compatibility_policy.py
scripts/codex_compatibility_types.py
scripts/codex_managed_sources.py
scripts/codex_marketplace_probe.py
scripts/codex_runtime_discovery.py
scripts/install_plugin.py
scripts/install_plugin_cli.py
scripts/installer_cache_validation.py
scripts/installer_data_root.py
scripts/installer_observation.py
scripts/installer_preflight.py
scripts/installer_recovery.py
scripts/installer_result.py
tests/codex_compatibility_support.py
tests/install_plugin_fixtures.py
tests/install_plugin_support.py
tests/test_codex_compatibility.py
tests/test_codex_managed_policy.py
tests/test_codex_runtime_compatibility.py
tests/test_install_plugin.py
tests/test_install_plugin_concurrency.py
tests/test_install_plugin_entrypoint.py
tests/test_install_plugin_failures.py
tests/test_install_plugin_recovery.py
tests/test_install_plugin_recovery_guards.py
tests/test_install_plugin_reinstall.py
tests/test_install_plugin_transaction.py
"""),
        frozenset({"tests/test_hook_event.py"}),
    ),
    CommitRule(
        SUBJECTS[4],
        _paths("""
.github/workflows/installer-posix.yml
install.ps1
install.sh
tests/native_posix_install_smoke.py
tests/native_posix_smoke_support.py
tests/real_install_smoke.py
tests/real_install_smoke_fixtures.py
tests/real_install_smoke_ledger.py
tests/real_install_smoke_preflight.py
tests/real_install_smoke_support.py
tests/test_install_entrypoints.py
tests/test_real_install_smoke.py
"""),
    ),
    CommitRule(
        SUBJECTS[5],
        _paths("""
.agents/plugins/marketplace.json
.codex-plugin/plugin.json
README.md
tests/check_commit_contract.py
tests/commit_contract_paths.py
tests/commit_contract_rules.py
tests/fixtures/codex-marketplace-root-parser.json
tests/test_install_metadata.py
"""),
        frozenset(
            {
                ".agents/plugins/marketplace.json",
                ".codex-plugin/plugin.json",
                "README.md",
            }
        ),
    ),
    CommitRule(
        SUBJECTS[6],
        _paths("""
scripts/control.py
scripts/hook_event.py
scripts/hook_state.py
scripts/manager.py
scripts/setup_cli.py
scripts/watcher_events.py
tests/test_control.py
tests/test_hook_event.py
tests/test_setup_cli.py
tests/test_watcher_event_races.py
tests/test_watcher_restart_suppression.py
"""),
        _paths("""
scripts/control.py
scripts/hook_event.py
scripts/hook_state.py
scripts/manager.py
scripts/setup_cli.py
scripts/watcher_events.py
tests/test_control.py
tests/test_hook_event.py
tests/test_setup_cli.py
tests/test_watcher_event_races.py
tests/test_watcher_restart_suppression.py
"""),
    ),
    CommitRule(
        SUBJECTS[7],
        _paths("""
scripts/codex_marketplace_probe.py
tests/codex_compatibility_support.py
tests/check_commit_contract.py
tests/commit_contract_paths.py
"""),
        _paths("""
scripts/codex_marketplace_probe.py
tests/codex_compatibility_support.py
tests/check_commit_contract.py
tests/commit_contract_paths.py
"""),
    ),
)
