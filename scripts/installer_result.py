"""Privacy-safe installer result values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Expose the primary install outcome and any separate recovery failure."""

    install_ok: bool
    error_code: str | None
    primary_error_code: str | None
    cleanup_error_code: str | None
    final_plugin_disabled: bool
    final_cache_matches_enabled_trust: bool
    created_cache_removed: bool
    external_config_conflict_after_failure: bool = False


def unobserved_failure(reason: str) -> InstallResult:
    """Return a failure without claiming state that was never observed."""
    return InstallResult(
        install_ok=False,
        error_code=reason,
        primary_error_code=reason,
        cleanup_error_code=None,
        final_plugin_disabled=False,
        final_cache_matches_enabled_trust=False,
        created_cache_removed=False,
    )


def install_success() -> InstallResult:
    """Return the exact verified enabled-install success state."""
    return InstallResult(
        install_ok=True,
        error_code=None,
        primary_error_code=None,
        cleanup_error_code=None,
        final_plugin_disabled=False,
        final_cache_matches_enabled_trust=True,
        created_cache_removed=False,
    )
