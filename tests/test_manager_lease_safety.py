import os
from pathlib import Path

import pytest

import scripts.manager_lease as manager_lease_module
from scripts.manager_lease import (
    acquire_manager_lease,
    manager_lease_owner,
    release_manager_lease,
)
from scripts.state import StateDocument, UnsafeStatePathError, load_state, runtime_path, save_state


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


def test_recovery_lease_claim_advances_only_the_expected_identity(tmp_path: Path) -> None:
    # Given: one persisted activation generation and its exact public identities.
    root = tmp_path / "root"
    session_id = "session-1"
    path = runtime_path(root, session_id)
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": session_id,
                "transcript_path": "sessions/rollout.jsonl",
                "revision": 7,
            }
        ),
    )
    expected = manager_lease_module.RecoveryLeaseIdentity(
        7,
        session_id,
        "sessions/rollout.jsonl",
    )

    # When: the first claimant wins, then a stale claimant retries after release.
    claim = manager_lease_module.acquire_recovery_manager_lease(root, path.name, expected)
    assert claim is not None
    release_manager_lease(claim.lease)
    winner_bytes = path.read_bytes()
    stale = manager_lease_module.acquire_recovery_manager_lease(root, path.name, expected)

    # Then: only the winner advances the generation and the loser writes zero bytes.
    assert stale is None
    assert load_state(root, path).values["revision"] == 8
    assert path.read_bytes() == winner_bytes


def test_recovery_lease_rejects_transcript_swap_without_state_write(tmp_path: Path) -> None:
    # Given: discovery captured one transcript identity before another activation replaced it.
    root = tmp_path / "root"
    session_id = "session-1"
    path = runtime_path(root, session_id)
    expected = manager_lease_module.RecoveryLeaseIdentity(
        7,
        session_id,
        "sessions/original.jsonl",
    )
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": session_id,
                "transcript_path": "sessions/replacement.jsonl",
                "revision": 7,
            }
        ),
    )
    before = path.read_bytes()

    # When: stale recovery acquires the OS lease and rereads the replaced state.
    claim = manager_lease_module.acquire_recovery_manager_lease(root, path.name, expected)

    # Then: identity mismatch releases the lease and writes no runtime bytes.
    assert claim is None
    assert path.read_bytes() == before
    assert manager_lease_owner(root, path.name) is None
