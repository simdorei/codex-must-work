from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import scripts.config_publication as publication
from scripts.config_metadata import (
    PosixMetadata,
    WindowsMetadata,
    capture_config_snapshot,
    capture_metadata,
)
from scripts.config_publication import ConfigSnapshot, read_config_bytes, write_config_bytes
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import FileIdentity, InstallerLease, installer_lock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock_temp = tmp_path / "temp"
    lock_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(lock_temp))
    value = tmp_path / "home"
    value.mkdir()
    return value


def _reason(caught: pytest.ExceptionInfo[InstallPluginError]) -> str:
    return caught.value.reason_code


def _temporary_files(home: Path) -> list[Path]:
    return list(home.glob(".config.toml.cmw.*"))


def test_publication_preserves_metadata_and_is_idempotent(home: Path) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    if os.name != "nt":
        path.chmod(0o640)
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        assert write_config_bytes(lease, before, b"after\n") == b"after\n"
        after = read_config_bytes(home, lease)
        assert after.metadata == before.metadata
        assert write_config_bytes(lease, after, after.data) == after.data
    assert path.read_bytes() == b"after\n"
    assert not _temporary_files(home)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_missing_config_is_created_from_private_lock_metadata(home: Path) -> None:
    with installer_lock(home) as lease:
        source = capture_metadata(lease.lock_path, lease.descriptor)
        before = read_config_bytes(home, lease)
        assert before.identity is None
        assert write_config_bytes(lease, before, b"created\n") == b"created\n"
        after = read_config_bytes(home, lease)
    assert after.data == b"created\n"
    assert after.identity is not None
    assert after.metadata is not None
    if os.name == "nt":
        assert isinstance(source, WindowsMetadata)
        assert isinstance(after.metadata, WindowsMetadata)
        expected_owner = source.base_security.partition("O:")[2].partition("G:")[0]
        actual = after.metadata.base_security
        actual_owner = actual.partition("O:")[2].partition("G:")[0]
        dacl = actual.partition("D:")[2]
        control, _, aces = dacl.partition("(")
        assert actual_owner == expected_owner
        assert control == "P"
        assert f"({aces}" == f"(A;;FA;;;{actual_owner})"
    else:
        assert isinstance(source, PosixMetadata)
        assert isinstance(after.metadata, PosixMetadata)
        assert after.metadata.owner == source.owner == os.geteuid()
        assert after.metadata.mode == 0o600
    assert not _temporary_files(home)


def test_concurrent_change_is_rejected_without_overwrite(home: Path) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        _ = path.write_bytes(b"attacker\n")
        with pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(lease, before, b"replacement\n")
    assert _reason(caught) == "codex_config_concurrent_change"
    assert path.read_bytes() == b"attacker\n"
    assert not _temporary_files(home)


@pytest.mark.parametrize("original", [None, b"before\n"])
@pytest.mark.parametrize("after_publish", [False, True])
def test_publication_window_external_write_is_preserved(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    original: bytes | None,
    *,
    after_publish: bool,
) -> None:
    path = home / "config.toml"
    if original is not None:
        _ = path.write_bytes(original)
    publish = publication.publish_snapshot

    def race(
        lease: InstallerLease,
        expected: ConfigSnapshot,
        temporary: tuple[Path, FileIdentity],
        descriptor: int,
        backup: Path,
    ) -> FileIdentity:
        if not after_publish:
            _ = path.write_bytes(b"external-writer\n")
        published = publish(lease, expected, temporary, descriptor, backup)
        if after_publish:
            _ = path.write_bytes(b"external-writer\n")
        return published

    monkeypatch.setattr(publication, "publish_snapshot", race)
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        with pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(lease, before, b"installer\n")
    actual = path.read_bytes() if path.exists() else None
    assert (_reason(caught), actual) == (
        "codex_config_concurrent_change",
        b"external-writer\n",
    )
    assert not _temporary_files(home)


def test_post_check_temporary_swap_is_rejected_and_original_restored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    replace = publication.replace_windows_file
    displaced = home / ".config.toml.cmw.displaced"

    def swap(target: Path, temporary: Path, backup: Path) -> None:
        _ = temporary.replace(displaced)
        _ = temporary.write_bytes(b"attacker\n")
        replace(target, temporary, backup)

    monkeypatch.setattr(publication, "replace_windows_file", swap)
    try:
        with installer_lock(home) as lease:
            before = read_config_bytes(home, lease)
            with pytest.raises(InstallPluginError) as caught:
                _ = write_config_bytes(lease, before, b"installer\n")
            after = read_config_bytes(home, lease)
        assert _reason(caught) == "codex_config_concurrent_change"
        assert after == before
        assert displaced.read_bytes() == b"installer\n"
    finally:
        displaced.unlink(missing_ok=True)
    assert not _temporary_files(home)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_rejects_unsafe_config_path(home: Path, tmp_path: Path, kind: str) -> None:
    path = home / "config.toml"
    outside = tmp_path / "outside"
    _ = outside.write_bytes(b"outside\n")
    if kind == "symlink":
        path.symlink_to(outside)
    else:
        os.link(outside, path)
    with installer_lock(home) as lease, pytest.raises(InstallPluginError) as caught:
        _ = read_config_bytes(home, lease)
    assert _reason(caught) == "codex_config_unsafe_path"
    assert outside.read_bytes() == b"outside\n"


def test_rejects_snapshot_from_another_lease(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _ = (home / "config.toml").write_bytes(b"before\n")
    with installer_lock(home) as first:
        snapshot = read_config_bytes(home, first)
        with installer_lock(other) as second, pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(second, snapshot, b"replacement\n")
    assert _reason(caught) == "installer_lock_lease_mismatch"


def test_flush_failure_rolls_back_exact_original(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "config.toml"
    raw = b"before\n"
    _ = path.write_bytes(raw)
    original_flush = publication.flush_directory
    calls = 0

    def fail_once(directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError
        original_flush(directory)

    monkeypatch.setattr(publication, "flush_directory", fail_once)
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        with pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(lease, before, b"replacement\n")
    assert _reason(caught) == "codex_config_publication_failed"
    assert path.read_bytes() == raw
    assert not _temporary_files(home)


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW merge errors")
@pytest.mark.parametrize("number", [1175, 1176, 1177])
def test_replace_file_merge_errors_leave_original(
    home: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")

    def reject(_target: Path, _temporary: Path, _backup: Path) -> None:
        raise OSError(number, "ReplaceFileW")

    monkeypatch.setattr(publication, "replace_windows_file", reject)
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        with pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(lease, before, b"replacement\n")
    assert _reason(caught) == "codex_config_publication_failed"
    assert path.read_bytes() == b"before\n"
    assert not _temporary_files(home)


def test_metadata_revalidation_failure_rolls_back(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "config.toml"
    raw = b"before\n"
    _ = path.write_bytes(raw)
    original_capture = capture_config_snapshot
    injected = False

    def mismatch(target: Path, lease: InstallerLease) -> ConfigSnapshot:
        nonlocal injected
        snapshot = original_capture(target, lease)
        if injected or snapshot.data != b"replacement\n":
            return snapshot
        injected = True
        metadata = snapshot.metadata
        assert metadata is not None
        if isinstance(metadata, PosixMetadata):
            changed = replace(metadata, mode=metadata.mode ^ stat.S_IXUSR)
        else:
            changed = replace(metadata, file_attributes=metadata.file_attributes ^ 0x20)
        return replace(snapshot, metadata=changed)

    monkeypatch.setattr("scripts.config_publication.capture_config_snapshot", mismatch)
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        with pytest.raises(InstallPluginError) as caught:
            _ = write_config_bytes(lease, before, b"replacement\n")
    assert _reason(caught) == "codex_config_metadata_revalidation_failed"
    assert path.read_bytes() == raw
    assert not _temporary_files(home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX extended attributes")
def test_posix_extended_attributes_are_rejected(home: Path) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    os.setxattr(path, b"user.cmw", b"present")
    with installer_lock(home) as lease, pytest.raises(InstallPluginError) as caught:
        _ = read_config_bytes(home, lease)
    assert _reason(caught) == "codex_config_metadata_unsupported"
