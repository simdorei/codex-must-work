import os
from pathlib import Path

import pytest

import scripts.manager_lease as manager_lease_module
from scripts.manager_lease import acquire_manager_lease, manager_lease_owner
from scripts.state import UnsafeStatePathError


def test_manager_lease_rejects_final_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"
    lease_path = managers / f"{runtime_name}.lease"
    outside = tmp_path / "outside.lease"
    _ = outside.write_text("unchanged\n", encoding="ascii")
    try:
        lease_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    with pytest.raises(UnsafeStatePathError):
        _ = acquire_manager_lease(root, runtime_name)

    assert outside.read_text(encoding="ascii") == "unchanged\n"


def test_manager_lease_rejects_hard_link_without_truncating_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"
    lease_path = managers / f"{runtime_name}.lease"
    outside = tmp_path / "outside.lease"
    _ = outside.write_text("unchanged\n", encoding="ascii")
    try:
        os.link(outside, lease_path)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(UnsafeStatePathError):
        _ = acquire_manager_lease(root, runtime_name)

    assert outside.read_text(encoding="ascii") == "unchanged\n"


def test_manager_lease_probe_rejects_redirected_parent_without_outside_lock(
    tmp_path: Path,
) -> None:
    # Given: the manager directory redirects outside the private state root.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "managers").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    runtime_name = "a" * 64 + ".json"

    # When/Then: probing the lease rejects the redirect before creating a lock outside.
    with pytest.raises(UnsafeStatePathError):
        _ = manager_lease_owner(root, runtime_name)
    assert tuple(outside.iterdir()) == ()


def test_manager_lease_probe_does_not_create_missing_lock(tmp_path: Path) -> None:
    # Given: a direct manager directory has no lease marker or operating-system lock.
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"

    # When/Then: a read-only owner probe leaves the directory unchanged.
    assert manager_lease_owner(root, runtime_name) is None
    assert tuple(managers.iterdir()) == ()


def test_manager_lease_probe_rejects_parent_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the manager directory changes identity while an owner probe is running.
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"
    lock_path = managers / f".{runtime_name}.lease.lock"
    lock_path.touch()
    metadata = managers.lstat()
    actual = (metadata.st_dev, metadata.st_ino)
    identities = iter((actual, (actual[0], actual[1] + 1)))

    def next_identity(_root: Path, _path: Path) -> tuple[int, int]:
        return next(identities)

    monkeypatch.setattr(
        manager_lease_module,
        "_manager_directory_identity",
        next_identity,
    )

    # When/Then: the probe fails closed instead of trusting the raced path.
    with pytest.raises(UnsafeStatePathError):
        _ = manager_lease_owner(root, runtime_name)
