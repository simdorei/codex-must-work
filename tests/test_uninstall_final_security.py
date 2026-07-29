from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.config_publication import ConfigSnapshot
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import snapshot_retained_cache
from scripts.marketplace_identity import MARKETPLACE_NAME
from scripts.private_root import ensure_private_root
from scripts.uninstall_config import render_config_removal
from scripts.uninstall_evidence import validated_install_evidence
from scripts.uninstall_paths import delete_quarantined_root, quarantine_owned_root
from scripts.uninstall_plugin import uninstall
from tests.uninstall_test_support import authorize_install, cache_generation, config_bytes

if TYPE_CHECKING:
    from scripts.installer_lock import InstallerLease
    from scripts.uninstall_types import QuarantinedRoot


def test_forged_cache_without_protected_receipt_fails_without_mutation(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    config = home / "config.toml"
    original = config_bytes(source, include_legacy=False)
    _ = config.write_bytes(original)

    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)
    assert caught.value.reason_code == "uninstall_receipt_reinstall_required"
    assert config.read_bytes() == original
    assert cache.is_dir()


def test_shared_marketplace_survives_when_another_plugin_references_it(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _, digest = snapshot_retained_cache(cache)
    evidence = validated_install_evidence(cache, MARKETPLACE_NAME, source, digest)
    original = config_bytes(source, include_legacy=False)
    shared = original + b'[plugins."other@simdorei"]\nenabled = true\n'
    snapshot = ConfigSnapshot(shared, None, None, home / "config.toml", (1, 1))

    # When
    rendered = render_config_removal(snapshot, (evidence,))

    # Then
    assert b"[marketplaces.simdorei]\n" in rendered
    assert b'[plugins."codex-must-work@simdorei"]' not in rendered
    assert b'[plugins."other@simdorei"]\nenabled = true\n' in rendered


def test_valid_protected_receipt_authorizes_exact_cache_uninstall(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)

    # When
    result = uninstall(home, source, purge_data=False)
    repeated = uninstall(home, source, purge_data=False)

    # Then
    assert result.removed_cache_generations == 1
    assert repeated.removed_cache_generations == 0
    assert not cache.exists()
    assert not (home / ".cmw-installer-state" / "install-receipt-v1.json").exists()


def test_mutated_protected_receipt_fails_before_any_mutation(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    config = home / "config.toml"
    original = config_bytes(source, include_legacy=False)
    _ = config.write_bytes(original)
    receipt = authorize_install(home, source, cache)
    encoded = receipt.read_bytes()
    _ = receipt.write_bytes(encoded.replace(b'"hmac_sha256":"', b'"hmac_sha256":"0', 1))

    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)
    assert caught.value.reason_code == "uninstall_receipt_invalid"
    assert config.read_bytes() == original
    assert cache.is_dir()


def test_purge_removes_real_state_root_but_not_outside_sentinel(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    state = home / "codex-must-work"
    ensure_private_root(state)
    _ = (state / "calibration.json").write_bytes(b"owned")
    outside = tmp_path / "outside.txt"
    _ = outside.write_bytes(b"preserve")

    # When
    result = uninstall(home, source, purge_data=True)

    # Then
    assert result.purged_data_roots == 2
    assert not state.exists()
    assert outside.read_bytes() == b"preserve"


def test_config_failure_rolls_back_all_quarantines_and_preserves_config_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    config = home / "config.toml"
    original = config_bytes(source, include_legacy=False)
    _ = config.write_bytes(original)
    _ = authorize_install(home, source, cache)

    def fail_write(
        _lease: InstallerLease,
        _snapshot: ConfigSnapshot,
        _replacement: bytes,
    ) -> bytes:
        reason = "injected_config_failure"
        raise InstallPluginError(reason)

    monkeypatch.setattr("scripts.uninstall_plugin.write_config_bytes", fail_write)

    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)
    assert caught.value.reason_code == "injected_config_failure"
    assert config.read_bytes() == original
    assert cache.is_dir()
    assert not tuple(home.rglob(".cmw-uninstall-*"))


def test_post_config_cleanup_retry_never_deletes_newcomer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    original_delete = delete_quarantined_root

    def fail_cleanup(_root: QuarantinedRoot) -> None:
        reason = "injected_cleanup_failure"
        raise InstallPluginError(reason)

    monkeypatch.setattr("scripts.uninstall_plugin.delete_quarantined_root", fail_cleanup)
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=False)
    assert caught.value.reason_code == "injected_cleanup_failure"
    cache.mkdir()
    newcomer = cache / "newcomer.txt"
    _ = newcomer.write_bytes(b"preserve")
    monkeypatch.setattr(
        "scripts.uninstall_plugin.delete_quarantined_root",
        original_delete,
    )

    # When
    result = uninstall(home, source, purge_data=False)
    repeated = uninstall(home, source, purge_data=False)

    # Then
    assert result.removed_cache_generations == 1
    assert repeated.removed_cache_generations == 0
    assert newcomer.read_bytes() == b"preserve"
    assert not tuple(home.rglob(".cmw-uninstall-*"))


def test_signed_wal_exists_before_first_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    observed: list[bool] = []

    def interrupt_before_rename(_root: QuarantinedRoot) -> QuarantinedRoot:
        observed.append((home / ".cmw-installer-state" / "uninstall-pending-v1.json").is_file())
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "scripts.uninstall_plugin.quarantine_owned_root",
        interrupt_before_rename,
    )

    with pytest.raises(KeyboardInterrupt):
        _ = uninstall(home, source, purge_data=False)

    assert observed == [True]
    assert cache.is_dir()


def test_retry_restores_preconfig_move_interrupted_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    original_quarantine = quarantine_owned_root

    def interrupt_after_rename(root: QuarantinedRoot) -> QuarantinedRoot:
        _ = original_quarantine(root)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "scripts.uninstall_plugin.quarantine_owned_root",
        interrupt_after_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        _ = uninstall(home, source, purge_data=False)
    monkeypatch.setattr(
        "scripts.uninstall_plugin.quarantine_owned_root",
        original_quarantine,
    )

    result = uninstall(home, source, purge_data=False)

    assert result.removed_cache_generations == 1
    assert not cache.exists()
    assert not tuple(home.rglob(".cmw-uninstall-*"))


def test_default_uninstall_authorizes_later_purge_without_reinstall(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    state = home / "codex-must-work"
    ensure_private_root(state)
    _ = (state / "owned.txt").write_bytes(b"owned")

    default = uninstall(home, source, purge_data=False)
    purged = uninstall(home, source, purge_data=True)
    repeated = uninstall(home, source, purge_data=True)

    assert default.purged_data_roots == 0
    assert purged.purged_data_roots == 2
    assert repeated.purged_data_roots == 0
    assert not state.exists()


def test_later_purge_rejects_replacement_data_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"

    _ = uninstall(home, source, purge_data=False)
    original = data.rename(data.with_name("original-data"))
    ensure_private_root(data)
    newcomer = data / "newcomer.txt"
    _ = newcomer.write_bytes(b"preserve")

    with pytest.raises(InstallPluginError):
        _ = uninstall(home, source, purge_data=True)

    assert newcomer.read_bytes() == b"preserve"
    assert original.is_dir()
