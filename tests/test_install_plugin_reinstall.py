from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_observation import PriorState
from tests.install_plugin_support import (
    compatibility_fixture,
    publication_fixture,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

pytest_plugins = ("tests.install_plugin_fixtures",)

@pytest.mark.parametrize("mode", ["publish-error", "identity", "digest", "deleted"])
def test_no_write_reinstall_race_never_leaves_an_enabled_cache_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    publications = 0
    retained_valid = True

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        nonlocal publications
        publications += 1
        return publication_fixture(home)

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), "a" * 64

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(
        installer_observation,
        "retained_cache_matches",
        lambda *_args: retained_valid,
    )
    monkeypatch.setattr(
        installer_observation,
        "trusted_hook_states_for_plugin",
        lambda path, _marketplace: trusted_states(path),
    )
    assert install(home.resolve(), source).install_ok
    target = (
        home
        / "plugins"
        / "cache"
        / "codex-must-work-local"
        / "codex-must-work"
        / "1.2.3"
    )
    if mode == "deleted":
        original_eligible = install_plugin.eligible_no_write

        def delete_after_classification(
            prior: PriorState,
            expected: Path,
            trust: tuple[TrustedHookState, ...],
        ) -> bool:
            eligible = original_eligible(prior, expected, trust)
            expected.rmdir()
            return eligible

        monkeypatch.setattr(install_plugin, "eligible_no_write", delete_after_classification)

    def raced_validation(
        publication: CachePublication, source_fixture: Path
    ) -> tuple[CacheIdentity, str]:
        nonlocal retained_valid
        retained_valid = False
        if mode in {"publish-error", "deleted"}:
            raise InstallPluginError("cache_same_version_mismatch")
        identity = publication.identity
        if mode == "identity":
            identity = CacheIdentity(identity.device, identity.inode + 1)
        digest = "b" * 64 if mode == "digest" else publication.digest
        return identity, digest

    monkeypatch.setattr(install_plugin, "validate_cache_publication", raced_validation)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.final_plugin_disabled is True
    assert publications == 1
    if mode == "deleted":
        assert result.created_cache_removed is False
        assert not target.exists()


def test_malformed_legacy_enabled_value_never_qualifies_for_no_write_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    config = home / "config.toml"
    _ = config.write_bytes(
        unsafe_prior_config(source, "zero")
        + b'\n[plugins."codex-must-work@simdorei"]\nenabled = "yes"\n'
    )
    compatibility = compatibility_fixture(home)

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.error_code == "codex_config_unsupported_syntax"


@pytest.mark.parametrize("case", ["wrong-name", "local", "unsafe", "higher-cache"])
def test_manifest_and_selection_preflight_do_not_mutate_config_or_cache(
    tmp_path: Path, case: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    original = b'user_marker = "unchanged"\n'
    _ = config.write_bytes(original)
    source = source_fixture(tmp_path)
    root = Path(__file__).resolve().parents[1]
    _ = (source / "hooks" / "hooks.json").write_bytes((root / "hooks" / "hooks.json").read_bytes())
    manifest = {"name": "codex-must-work", "version": "1.2.3"}
    if case == "wrong-name":
        manifest["name"] = "other-plugin"
    elif case == "local":
        manifest["version"] = "local"
    elif case == "unsafe":
        manifest["version"] = "../unsafe"
    _ = (source / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    if case == "higher-cache":
        higher = (
            home
            / "plugins"
            / "cache"
            / "codex-must-work-local"
            / "codex-must-work"
            / "9.0.0"
        )
        higher.mkdir(parents=True)
    before = tuple(
        sorted(
            (path.relative_to(home).as_posix(), path.is_dir(), path.read_bytes() if path.is_file() else b"")
            for path in home.rglob("*")
        )
    )

    result = install(home.resolve(), source)
    after = tuple(
        sorted(
            (path.relative_to(home).as_posix(), path.is_dir(), path.read_bytes() if path.is_file() else b"")
            for path in home.rglob("*")
        )
    )

    assert not result.install_ok
    assert config.read_bytes() == original
    assert after == before
