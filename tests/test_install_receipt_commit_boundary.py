from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import install_plugin, install_receipt
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.install_receipt import load_install_receipt
from scripts.installer_lock import installer_lock
from scripts.protected_installer_state import JsonObject, write_signed_record
from scripts.uninstall_completion import (
    clear_uninstall_complete,
    load_uninstall_completion,
    mark_uninstall_complete,
)
from scripts.uninstall_plugin import uninstall
from tests.install_plugin_support import (
    InstallerCallValue,
    compatibility_fixture,
)

if TYPE_CHECKING:
    from scripts.codex_compatibility import CompatibilityResult
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)

_WARNING = "install_completion_cleanup_pending"


class ForcedTermination(BaseException):
    """Model process termination at the protected-state commit boundary."""


def _cache_generations(home: Path) -> tuple[Path, ...]:
    parent = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    try:
        return tuple(path for path in parent.iterdir() if path.is_dir())
    except FileNotFoundError:
        return ()


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    compatibility = compatibility_fixture(home)

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    with installer_lock(home.resolve()) as lease:
        mark_uninstall_complete(lease, source, (), data_purged=False)
    return home, source


def test_completion_clear_failure_is_post_commit_success_warning_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_generation_validation: None,
) -> None:
    _ = real_generation_validation
    home, source = _case(tmp_path, monkeypatch)
    original_clear = clear_uninstall_complete
    reason = "injected_completion_clear_failure"

    def fail_clear(_lease: InstallerLease) -> None:
        raise InstallPluginError(reason)

    monkeypatch.setattr(install_receipt, "clear_uninstall_complete", fail_clear)
    first = install(home.resolve(), source)

    assert first.install_ok
    assert first.warning_code == _WARNING
    assert (home / ".cmw-installer-state" / "install-receipt-v1.json").is_file()
    assert (home / ".cmw-installer-state" / "uninstall-complete-v1.json").is_file()
    assert _cache_generations(home)

    monkeypatch.setattr(install_receipt, "clear_uninstall_complete", original_clear)
    retried = install(home.resolve(), source)
    with installer_lock(home.resolve()) as lease:
        assert load_uninstall_completion(lease, source) is None
        receipt = load_install_receipt(lease, source)
        assert receipt.cache_path in _cache_generations(home)
    assert retried.install_ok
    assert retried.warning_code is None
    assert retried.final_cache_matches_enabled_trust

    with installer_lock(home.resolve()) as lease:
        mark_uninstall_complete(lease, source, (), data_purged=False)
    removed = uninstall(home.resolve(), source, purge_data=False)

    assert (home / ".cmw-installer-state" / "uninstall-complete-v1.json").is_file()
    assert removed.removed_cache_generations == 1


def test_forced_termination_before_receipt_replace_rolls_back_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_generation_validation: None,
) -> None:
    _ = real_generation_validation
    home, source = _case(tmp_path, monkeypatch)
    original_write = write_signed_record

    def interrupt_before(
        lease: InstallerLease,
        name: str,
        payload: JsonObject,
    ) -> None:
        if name == "install-receipt-v1.json":
            raise ForcedTermination
        original_write(lease, name, payload)

    monkeypatch.setattr(install_receipt, "write_signed_record", interrupt_before)
    with pytest.raises(ForcedTermination):
        _ = install(home.resolve(), source)

    assert not (home / ".cmw-installer-state" / "install-receipt-v1.json").exists()
    assert not _cache_generations(home)


def test_forced_termination_after_receipt_replace_keeps_committed_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_generation_validation: None,
) -> None:
    _ = real_generation_validation
    home, source = _case(tmp_path, monkeypatch)
    original_write = write_signed_record

    def interrupt_after(
        lease: InstallerLease,
        name: str,
        payload: JsonObject,
    ) -> None:
        original_write(lease, name, payload)
        if name == "install-receipt-v1.json":
            raise ForcedTermination

    monkeypatch.setattr(install_receipt, "write_signed_record", interrupt_after)
    with pytest.raises(ForcedTermination):
        _ = install(home.resolve(), source)

    receipt = home / ".cmw-installer-state" / "install-receipt-v1.json"
    assert receipt.is_file()
    assert _cache_generations(home)


@pytest.mark.parametrize("position", ["before", "after"])
def test_forced_termination_at_completion_clear_keeps_receipt_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_generation_validation: None,
    position: str,
) -> None:
    _ = real_generation_validation
    home, source = _case(tmp_path, monkeypatch)
    original_clear = clear_uninstall_complete

    def interrupt(lease: InstallerLease) -> None:
        if position == "before":
            raise ForcedTermination
        original_clear(lease)
        raise ForcedTermination

    monkeypatch.setattr(install_receipt, "clear_uninstall_complete", interrupt)
    with pytest.raises(ForcedTermination):
        _ = install(home.resolve(), source)

    assert (home / ".cmw-installer-state" / "install-receipt-v1.json").is_file()
    assert _cache_generations(home)
