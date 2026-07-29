from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import uninstall_paths, uninstall_plugin
from scripts.cache_types import identity
from scripts.install_errors import InstallPluginError
from scripts.uninstall_plugin import uninstall
from tests.uninstall_test_support import (
    authorize_install,
    cache_generation,
    config_bytes,
    secure_tree,
)


def test_default_uninstall_removes_only_owned_config_and_valid_cache(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    cache = cache_generation(home, "simdorei")
    legacy_cache = cache_generation(home, "codex-must-work-local")
    source = Path(__file__).parents[1]
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    preserved = data / "notification.json"
    _ = preserved.write_bytes(b'{"webhook":"preserve"}')

    # When
    receipt = uninstall(home, source, purge_data=False)

    # Then
    parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    final_bytes = (home / "config.toml").read_bytes()
    assert parsed["title"] == "keep"
    assert parsed["marketplaces"]["other"]["source"] == "keep-byte-for-byte"
    assert parsed["plugins"]["other@other"]["enabled"] is True
    assert "simdorei" not in parsed["marketplaces"]
    assert "codex-must-work@simdorei" not in parsed["plugins"]
    assert b'[plugins."other@other"]\nenabled = true # keep\n' in final_bytes
    assert not cache.exists()
    assert legacy_cache.exists()
    assert preserved.read_bytes() == b'{"webhook":"preserve"}'
    assert receipt.removed_cache_generations == 1
    assert receipt.purged_data_roots == 0


def test_install_reinstall_default_uninstall_repeat_and_explicit_purge(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    installed = cache_generation(home, "simdorei", "1.2.3")
    reinstalled = cache_generation(home, "simdorei", "2.0.0")
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    data.mkdir(parents=True)
    marker = data / ".private-root-v1"
    _ = marker.write_bytes(b"private-root-v1\n")
    _ = (data / "history.jsonl").write_bytes(b"owned history")
    secure_tree(data)
    _ = authorize_install(home, source, reinstalled)

    # When
    first = uninstall(home, source, purge_data=False)
    assert data.exists()
    repeated = uninstall(home, source, purge_data=False)
    final_cache = cache_generation(home, "simdorei", "3.0.0")
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, final_cache)
    purged = uninstall(home, source, purge_data=True)

    # Then
    assert installed.exists()
    assert not reinstalled.exists()
    assert not final_cache.exists()
    assert first.removed_cache_generations == 1
    assert repeated.removed_cache_generations == 0
    assert repeated.removed_runtime_roots == 0
    assert first.purged_data_roots == 0
    assert purged.purged_data_roots == 1
    assert not data.exists()


def test_uninstall_rejects_malformed_config_without_deleting_cache(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    raw = b"[broken\n"
    _ = (home / "config.toml").write_bytes(raw)
    cache = cache_generation(home, "simdorei")
    _ = authorize_install(home, Path(__file__).parents[1], cache)

    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, Path(__file__).parents[1], purge_data=False)
    assert caught.value.reason_code == "codex_config_malformed"
    assert (home / "config.toml").read_bytes() == raw
    assert cache.exists()


def test_uninstall_rejects_redirected_cache_generation(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    target = cache_generation(home, "elsewhere")
    versions = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    versions.mkdir(parents=True)
    link = versions / "1.2.3"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, Path(__file__).parents[1], purge_data=False)
    assert caught.value.reason_code == "uninstall_receipt_reinstall_required"
    assert target.exists()


def test_purge_rejects_redirected_child_without_partial_delete(tmp_path: Path) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, "simdorei")
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    preserved = data / "preserved.json"
    _ = preserved.write_bytes(b"preserve")
    secure_tree(data)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = data / "redirect"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    # When / Then
    with pytest.raises(InstallPluginError) as caught:
        _ = uninstall(home, source, purge_data=True)
    assert caught.value.reason_code == "uninstall_data_ownership_unknown"
    assert preserved.read_bytes() == b"preserve"
    assert outside.exists()


def test_uninstall_rechecks_quarantined_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    cache = cache_generation(home, "simdorei")
    source = Path(__file__).parents[1]
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    original_identity = identity(cache.lstat())
    original_sentinel = cache / ".codex-plugin" / "plugin.json"
    original_bytes = original_sentinel.read_bytes()
    original = uninstall_paths.quarantine_no_replace
    quarantined: list[Path] = []

    def race(source: Path, target: Path) -> None:
        original(source, target)
        quarantined.append(target)
        source.mkdir()
        _ = (source / "replacement.txt").write_bytes(b"newcomer")

    monkeypatch.setattr(uninstall_paths, "quarantine_no_replace", race)

    # When / Then
    receipt = uninstall(home, source, purge_data=False)
    assert receipt.removed_cache_generations == 1
    assert receipt.removed_runtime_roots == 1
    assert len(quarantined) == 2
    quarantine = next(path for path in quarantined if path.parent == cache.parent)
    replacement_identity = identity(cache.lstat())
    assert replacement_identity != original_identity
    assert (cache / "replacement.txt").read_bytes() == b"newcomer"
    assert not quarantine.exists()
    assert original_bytes


def test_cli_emits_machine_readable_cleanup_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()

    # When
    exit_code = uninstall_plugin.run_cli([str(home), str(Path(__file__).parents[1])])

    # Then
    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.startswith("{")
    assert output.rstrip().endswith("}")
    assert '"removed_cache_generations": 0' in output
    assert '"purged_data_roots": 0' in output
