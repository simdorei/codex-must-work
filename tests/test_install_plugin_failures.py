from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import cache_publication, install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.codex_config import update_codex_config as real_update_codex_config
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_observation import ConfigObservation
from tests.install_plugin_support import (
    CACHE_CLEANUP_FAILED,
    CACHE_PUBLICATION_FAILED,
    CONFIG_PUBLICATION_FAILED,
    HOOKS_DISABLED,
    INJECTED_READ_FAILURE,
    LOCK_FAILED,
    PACKAGE_HOOKS_INVALID,
    UNSUPPORTED,
    assert_failed_without_success,
    compatibility_fixture,
    failure_case,
    publication_fixture,
    publisher,
    source_fixture,
    trusted_states,
)

if TYPE_CHECKING:

    from scripts.codex_config import ConfigMutation
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)

def test_cache_publication_failure_returns_privacy_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, source, compatibility_fixture = failure_case(tmp_path, monkeypatch)

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        raise InstallPluginError(CACHE_PUBLICATION_FAILED)

    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    result = install(home.resolve(), source)
    assert_failed_without_success(result, capsys.readouterr().out)


def test_cache_trust_failure_returns_privacy_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, source, compatibility_fixture = failure_case(tmp_path, monkeypatch)
    trust_calls = 0

    def trust(path: Path) -> tuple[TrustedHookState, ...]:
        nonlocal trust_calls
        trust_calls += 1
        if trust_calls == 2:
            raise InstallPluginError(PACKAGE_HOOKS_INVALID)
        return trusted_states(path)

    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trust)
    result = install(home.resolve(), source)
    assert_failed_without_success(result, capsys.readouterr().out)


def test_enabling_config_failure_returns_privacy_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, source, compatibility_fixture = failure_case(tmp_path, monkeypatch)

    def update(
        codex_home: Path,
        mutation: ConfigMutation,
        lease: InstallerLease | None = None,
    ) -> bytes:
        if mutation.plugin_enabled:
            raise InstallPluginError(CONFIG_PUBLICATION_FAILED)
        return real_update_codex_config(codex_home, mutation, lease)

    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "update_codex_config", update)
    result = install(home.resolve(), source)
    assert_failed_without_success(result, capsys.readouterr().out)


def test_final_observation_failure_returns_privacy_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, source, compatibility = failure_case(tmp_path, monkeypatch)
    compatibility_calls = 0
    final_phase = False

    def check(*args: object, **kwargs: object) -> CompatibilityResult:
        nonlocal compatibility_calls, final_phase
        _ = args, kwargs
        compatibility_calls += 1
        final_phase = compatibility_calls == 3
        return compatibility

    original_observe = installer_observation.observe_config

    def observe(path: Path, lease: InstallerLease) -> ConfigObservation:
        if final_phase:
            raise OSError(INJECTED_READ_FAILURE)
        return original_observe(path, lease)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "observe_config", observe)
    result = install(home.resolve(), source)
    assert_failed_without_success(result, capsys.readouterr().out)


def test_failure_never_prints_success_and_reports_privacy_safe_booleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> CompatibilityResult:
        raise InstallPluginError(UNSUPPORTED)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", fail)
    result = install(home.resolve(), source)
    captured = capsys.readouterr()
    assert not result.install_ok
    assert isinstance(result.final_plugin_disabled, bool)
    assert isinstance(result.final_cache_matches_enabled_trust, bool)
    assert isinstance(result.created_cache_removed, bool)
    assert "install=ok" not in captured.out
    assert "install=ok" not in captured.err


def test_invalid_paths_never_claim_unobserved_plugin_disabled(tmp_path: Path) -> None:
    result = install(Path("relative-home"), tmp_path.resolve())
    assert not result.install_ok
    assert result.error_code == "installer_path_not_absolute"
    assert result.final_plugin_disabled is False


def test_lock_failure_never_claims_unobserved_plugin_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    class FailingLock:
        def __enter__(self) -> InstallerLease:
            raise InstallPluginError(LOCK_FAILED)

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_lock(_home: Path) -> FailingLock:
        return FailingLock()

    monkeypatch.setattr(install_plugin, "installer_lock", fail_lock)
    result = install(home.resolve(), tmp_path.resolve())
    assert not result.install_ok
    assert result.error_code == "installer_lock_failed"
    assert result.final_plugin_disabled is False


def test_cleanup_failure_reports_observed_conflict_without_claiming_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    calls = 0

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    def fail_remove(_path: Path, _identity: CacheIdentity) -> None:
        raise InstallPluginError(CACHE_CLEANUP_FAILED)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(cache_publication, "remove_tree", fail_remove)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.error_code == "external_config_conflict_after_failure"
    assert result.created_cache_removed is False
    assert result.external_config_conflict_after_failure is True
    assert result.secondary_error_code == CACHE_CLEANUP_FAILED


@pytest.mark.skipif(os.name == "nt", reason="POSIX identity-swap fixture uses rename semantics")
def test_cleanup_conflict_never_deletes_replacement_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    calls = 0

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        publication = publication_fixture(home)
        moved = publication.cache_path.with_name("moved")
        _ = publication.cache_path.rename(moved)
        publication.cache_path.mkdir()
        _ = (publication.cache_path / "competitor").write_bytes(b"keep")
        return publication

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)
    replacement = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "1.2.3"
    assert not result.install_ok
    assert result.created_cache_removed is False
    assert result.external_config_conflict_after_failure is True
    assert result.secondary_error_code == CACHE_CLEANUP_FAILED
    assert (replacement / "competitor").read_bytes() == b"keep"
