"""Typed patchable operation bundle for the installer transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.cache_types import CachePublication
    from scripts.codex_compatibility import CompatibilityResult
    from scripts.codex_config import ConfigMutation
    from scripts.hook_trust import PluginManifest, TrustedHookState
    from scripts.install_receipt import ReceiptCommit
    from scripts.installed_generation import InstalledGeneration
    from scripts.installer_data_root import DataRootPublication
    from scripts.installer_mcp_runtime import McpRuntimePublication
    from scripts.installer_observation import ConfigObservation, PriorState
    from scripts.installer_recovery import RecoveryState
    from scripts.installer_result import InstallResult


class RecoveryContextLike(Protocol):
    """Describe the private recovery value without importing its private class."""

    @property
    def prior(self) -> PriorState:
        """Return the pre-install observation."""
        ...

    @property
    def state(self) -> RecoveryState:
        """Return transaction-owned mutable resources."""
        ...

    @property
    def reason(self) -> str:
        """Return the public-safe primary failure reason."""
        ...


@dataclass(frozen=True, slots=True)
class TransactionState:
    """Track exact resources owned by the active install transaction."""

    publication: CachePublication | None
    data_publication: DataRootPublication | None
    runtime_publication: McpRuntimePublication | None
    owned_data: bytes


@dataclass(frozen=True, slots=True)
class InstallerOperations:
    """Capture installer seams at call time so tests and callers can replace them."""

    marketplace: str
    plugin_name: str
    validate_compatibility: Callable[..., CompatibilityResult]
    validate_manifest: Callable[..., PluginManifest]
    trusted_states: Callable[..., tuple[TrustedHookState, ...]]
    classify_prior: Callable[..., PriorState]
    configured_generation: Callable[..., InstalledGeneration | None]
    scan_candidate: Callable[..., str]
    fail: Callable[[str], Never]
    prepare_data_root: Callable[..., DataRootPublication]
    prepare_control_key: Callable[..., bytes]
    bind_control_key: Callable[..., DataRootPublication]
    prepare_runtime: Callable[..., McpRuntimePublication]
    eligible_no_write: Callable[..., bool]
    prior_publication: Callable[..., CachePublication]
    no_write_reinstall: Callable[..., InstallResult]
    publish_receipt: Callable[..., ReceiptCommit]
    initial_disabled: Callable[..., ConfigObservation]
    observe_config: Callable[..., ConfigObservation]
    publish_cache: Callable[..., CachePublication]
    validate_publication: Callable[..., None]
    requested_generation: Callable[..., InstalledGeneration]
    select_generation: Callable[..., InstalledGeneration]
    write_config: Callable[..., bytes]
    require_final_generation: Callable[..., None]
    config_mutation: Callable[..., ConfigMutation]
    publish_state: Callable[..., bytes]
    observe_published: Callable[..., ConfigObservation]
    cache_matches: Callable[..., bool]
    install_success: Callable[..., InstallResult]
    recovery_state: Callable[..., RecoveryState]
    recovery_context: Callable[..., RecoveryContextLike]
    recover_install: Callable[..., InstallResult]
    recover_interrupted: Callable[..., None]
