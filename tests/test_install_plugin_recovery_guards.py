from __future__ import annotations

from pathlib import Path

import pytest

from scripts import cache_publication, install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from tests.install_plugin_support import (
    CACHE_PUBLICATION_FAILED,
    HOOKS_DISABLED,
    compatibility_fixture,
    publication_fixture,
    publisher,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_recovery_never_overwrites_external_writer_when_prior_is_restorable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    old_source = source_fixture(tmp_path, "1.0.0", "source-old")
    new_source = source_fixture(tmp_path, "2.0.0", "source-new")
    compatibility = compatibility_fixture(home)

    def ok(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), f"digest:{path.name}"

    def retained(path: Path, expected: CacheIdentity, digest: str) -> bool:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino) == expected and digest == (
            f"digest:{path.name}"
        )

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", ok)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(installer_observation, "retained_cache_matches", retained)
    monkeypatch.setattr(
        installer_observation,
        "trusted_hook_states_for_plugin",
        lambda path, _marketplace: trusted_states(path),
    )
    assert install(home.resolve(), old_source).install_ok
    config = home / "config.toml"

    calls = 0

    def fail_upgrade(*_args: object, **_kwargs: object) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    def external_then_remove(path: Path, expected: CacheIdentity) -> None:
        _ = config.write_bytes(b'external_marker = "keep"\n' + config.read_bytes())
        assert CacheIdentity(path.stat().st_dev, path.stat().st_ino) == expected
        path.rmdir()

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", fail_upgrade)
    monkeypatch.setattr(cache_publication, "remove_tree", external_then_remove)
    result = install(home.resolve(), new_source)

    assert not result.install_ok
    assert result.external_config_conflict_after_failure is True
    assert b'external_marker = "keep"' in config.read_bytes()


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_malformed_or_unreadable_enabled_prior_cache_is_safely_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    prior = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "0.9.0"
    prior.mkdir(parents=True)
    raw = unsafe_prior_config(prior.resolve(), "incomplete")
    last = trusted_states(prior)[-1]
    raw += (
        f'\n[hooks.state."{last.key}"]\nenabled = true\ntrusted_hash = "{last.trusted_hash}"\n'
    ).encode()
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    compatibility = compatibility_fixture(home)

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def fail_publish(*_args: object, **_kwargs: object) -> CachePublication:
        raise InstallPluginError(CACHE_PUBLICATION_FAILED)

    if failure == "unreadable":

        def unreadable(*_args: object, **_kwargs: object) -> tuple[TrustedHookState, ...]:
            raise OSError("injected unreadable prior cache")

        monkeypatch.setattr(installer_observation, "trusted_hook_states_for_plugin", unreadable)
    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", fail_publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.final_plugin_disabled is True
    assert b"enabled = false # target" in config.read_bytes()


def test_valid_reinstall_with_legacy_enabled_runs_transaction_and_disables_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    publications = 0

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        nonlocal publications
        publications += 1
        created = publication_fixture(home)
        if publications == 1:
            return created
        return CachePublication(
            created.cache_path,
            created.digest,
            created_by_run=False,
            identity=created.identity,
        )

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), "digest"

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(installer_observation, "retained_cache_matches", lambda *_args: True)
    monkeypatch.setattr(
        installer_observation,
        "trusted_hook_states_for_plugin",
        lambda path, _marketplace: trusted_states(path),
    )
    assert install(home.resolve(), source).install_ok
    config = home / "config.toml"
    _ = config.write_bytes(
        config.read_bytes() + b'\n[plugins."codex-must-work@simdorei"]\nenabled = true # legacy\n'
    )

    result = install(home.resolve(), source)

    assert result.install_ok
    assert publications == 2
    assert b"enabled = false # legacy" in config.read_bytes()
