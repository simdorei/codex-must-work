from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.config_publication import write_config_bytes
from scripts.marketplace_identity import MARKETPLACE_NAME
from scripts.private_root import ensure_private_root
from scripts.uninstall_paths import delete_quarantined_root, quarantine_owned_root
from scripts.uninstall_pending import write_pending_uninstall
from scripts.uninstall_plugin import uninstall
from tests.uninstall_test_support import authorize_install, cache_generation, config_bytes

if TYPE_CHECKING:
    from scripts.config_publication import ConfigSnapshot
    from scripts.installer_lock import InstallerLease
    from scripts.uninstall_pending import PendingPlan
    from scripts.uninstall_types import QuarantinedRoot


class ForcedTermination(BaseException):
    """Model process death that normal exception rollback cannot catch."""


def _case(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).parents[1]
    cache = cache_generation(home, MARKETPLACE_NAME)
    _ = (home / "config.toml").write_bytes(config_bytes(source, include_legacy=False))
    _ = authorize_install(home, source, cache)
    state = home / "codex-must-work"
    ensure_private_root(state)
    _ = (state / "owned.txt").write_bytes(b"owned")
    return home, source, cache


def _retry_and_assert(home: Path, source: Path, cache: Path) -> None:
    result = uninstall(home, source, purge_data=True)
    assert result.removed_cache_generations == 1
    assert result.purged_data_roots == 2
    assert not cache.exists()
    assert not (home / "codex-must-work").exists()
    assert not tuple(home.rglob(".cmw-uninstall-*"))


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("target_index", range(3))
def test_retry_survives_forced_termination_at_every_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    target_index: int,
) -> None:
    home, source, cache = _case(tmp_path)
    original = quarantine_owned_root
    calls = 0

    def interrupt(root: QuarantinedRoot) -> QuarantinedRoot:
        nonlocal calls
        current = calls
        calls += 1
        if current == target_index and position == "before":
            raise ForcedTermination
        moved = original(root)
        if current == target_index and position == "after":
            raise ForcedTermination
        return moved

    monkeypatch.setattr("scripts.uninstall_plugin.quarantine_owned_root", interrupt)
    with pytest.raises(ForcedTermination):
        _ = uninstall(home, source, purge_data=True)
    monkeypatch.setattr("scripts.uninstall_plugin.quarantine_owned_root", original)

    _retry_and_assert(home, source, cache)


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("write_index", range(2))
def test_retry_survives_forced_termination_at_every_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    write_index: int,
) -> None:
    home, source, cache = _case(tmp_path)
    original = write_pending_uninstall
    calls = 0

    def interrupt(
        lease: InstallerLease,
        plan: PendingPlan,
        phase: str,
    ) -> None:
        nonlocal calls
        current = calls
        calls += 1
        if current == write_index and position == "before":
            raise ForcedTermination
        original(lease, plan, phase)
        if current == write_index and position == "after":
            raise ForcedTermination

    monkeypatch.setattr("scripts.uninstall_plugin.write_pending_uninstall", interrupt)
    with pytest.raises(ForcedTermination):
        _ = uninstall(home, source, purge_data=True)
    monkeypatch.setattr("scripts.uninstall_plugin.write_pending_uninstall", original)

    _retry_and_assert(home, source, cache)


@pytest.mark.parametrize("position", ["before", "after"])
def test_retry_survives_forced_termination_at_config_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
) -> None:
    home, source, cache = _case(tmp_path)
    original = write_config_bytes

    def interrupt(
        lease: InstallerLease,
        snapshot: ConfigSnapshot,
        replacement: bytes,
    ) -> bytes:
        if position == "before":
            raise ForcedTermination
        _ = original(lease, snapshot, replacement)
        raise ForcedTermination

    monkeypatch.setattr("scripts.uninstall_plugin.write_config_bytes", interrupt)
    with pytest.raises(ForcedTermination):
        _ = uninstall(home, source, purge_data=True)
    monkeypatch.setattr("scripts.uninstall_plugin.write_config_bytes", original)

    _retry_and_assert(home, source, cache)


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("target_index", range(3))
def test_retry_survives_forced_termination_at_every_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    position: str,
    target_index: int,
) -> None:
    home, source, cache = _case(tmp_path)
    original = delete_quarantined_root
    calls = 0

    def interrupt(root: QuarantinedRoot) -> None:
        nonlocal calls
        current = calls
        calls += 1
        if current == target_index and position == "before":
            raise ForcedTermination
        original(root)
        if current == target_index and position == "after":
            raise ForcedTermination

    monkeypatch.setattr("scripts.uninstall_plugin.delete_quarantined_root", interrupt)
    with pytest.raises(ForcedTermination):
        _ = uninstall(home, source, purge_data=True)
    monkeypatch.setattr("scripts.uninstall_plugin.delete_quarantined_root", original)

    _retry_and_assert(home, source, cache)


def test_later_purge_resumes_cleanup_when_config_digest_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source, _cache = _case(tmp_path)
    _ = uninstall(home, source, purge_data=False)
    original = delete_quarantined_root
    calls = 0

    def interrupt(root: QuarantinedRoot) -> None:
        nonlocal calls
        original(root)
        calls += 1
        if calls == 1:
            raise ForcedTermination

    monkeypatch.setattr("scripts.uninstall_plugin.delete_quarantined_root", interrupt)
    with pytest.raises(ForcedTermination):
        _ = uninstall(home, source, purge_data=True)
    monkeypatch.setattr("scripts.uninstall_plugin.delete_quarantined_root", original)

    resumed = uninstall(home, source, purge_data=True)
    repeated = uninstall(home, source, purge_data=True)

    assert resumed.purged_data_roots == 2
    assert repeated.purged_data_roots == 0
    assert not tuple(home.rglob(".cmw-uninstall-*"))
