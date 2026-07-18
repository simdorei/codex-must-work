from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.state import (
    CorruptReason,
    CorruptStateError,
    FutureSchemaError,
    StateDocument,
    StateLockTimeoutError,
    UnsafeStatePathError,
    config_path,
    load_state,
    runtime_path,
    save_state,
)
from scripts.state_io import ExclusiveWriteLock


def test_path_lookup_for_opted_out_session_creates_no_artifacts(tmp_path: Path) -> None:
    # Given / When
    path = runtime_path(tmp_path, "session-1")

    # Then
    assert path.parent == tmp_path / "runtime"
    assert list(tmp_path.iterdir()) == []


def test_state_filename_is_sha256_of_opaque_session_id(tmp_path: Path) -> None:
    # Given
    session_id = "private-session-id"

    # When
    path = runtime_path(tmp_path, session_id)

    # Then
    assert path.name == hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    assert session_id not in path.name


def test_save_state_round_trips_schema_v1_payload(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)
    document = StateDocument(values={"enabled": True, "count": 2})

    # When
    save_state(tmp_path, path, document)

    # Then
    assert load_state(tmp_path, path).values == {"enabled": True, "count": 2}


def test_atomic_save_leaves_persistent_kernel_lock_without_temporary_file(
    tmp_path: Path,
) -> None:
    # Given
    path = config_path(tmp_path)

    # When
    save_state(tmp_path, path, StateDocument(values={"enabled": True}))

    # Then
    assert list(tmp_path.rglob("*.tmp")) == []
    assert list(tmp_path.rglob("*.lock")) == [tmp_path / ".config.json.lock"]


def test_corrupt_state_is_an_explicit_error(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)
    _ = path.write_text("not-json", encoding="utf-8")

    # When / Then
    with pytest.raises(CorruptStateError) as caught:
        _ = load_state(tmp_path, path)
    assert caught.value.reason is CorruptReason.INVALID_JSON


def test_future_schema_is_an_explicit_error(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)
    _ = path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    # When / Then
    with pytest.raises(FutureSchemaError):
        _ = load_state(tmp_path, path)


def test_save_refuses_to_replace_corrupt_state(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)
    _ = path.write_text("not-json", encoding="utf-8")

    # When / Then
    with pytest.raises(CorruptStateError):
        save_state(tmp_path, path, StateDocument(values={"enabled": True}))
    assert path.read_text(encoding="utf-8") == "not-json"


def test_save_rejects_path_outside_state_root(tmp_path: Path) -> None:
    # Given
    escaped = tmp_path.parent / "escaped-state.json"

    # When / Then
    with pytest.raises(UnsafeStatePathError):
        save_state(tmp_path, escaped, StateDocument(values={"enabled": True}))


def test_save_rejects_symlink_redirect(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    redirect = root / "redirect"
    try:
        redirect.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    # When / Then
    with pytest.raises(UnsafeStatePathError):
        save_state(root, redirect / "state.json", StateDocument(values={"enabled": True}))


def test_existing_write_lock_fails_closed_without_replacing_state(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)
    save_state(tmp_path, path, StateDocument(values={"revision": 1}))
    # When / Then
    with ExclusiveWriteLock(path), pytest.raises(StateLockTimeoutError):
        save_state(tmp_path, path, StateDocument(values={"revision": 2}))
    assert load_state(tmp_path, path).values == {"revision": 1}


def test_abandoned_write_lock_file_does_not_block_future_writes(tmp_path: Path) -> None:
    path = config_path(tmp_path)
    save_state(tmp_path, path, StateDocument(values={"revision": 1}))
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.touch(exist_ok=True)

    save_state(tmp_path, path, StateDocument(values={"revision": 2}))

    assert load_state(tmp_path, path).values == {"revision": 2}


def test_write_lock_rejects_final_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "state.json"
    outside = tmp_path / "outside.lock"
    _ = outside.write_bytes(b"unchanged")
    lock_path = root / ".state.json.lock"
    try:
        lock_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    with pytest.raises(UnsafeStatePathError), ExclusiveWriteLock(path):
        pass

    assert outside.read_bytes() == b"unchanged"


def test_load_rejects_hard_linked_state(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = config_path(root)
    save_state(root, path, StateDocument(values={"revision": 1}))
    outside = tmp_path / "outside.json"
    try:
        os.link(path, outside)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(UnsafeStatePathError):
        _ = load_state(root, path)


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not exposed as POSIX mode bits")
def test_state_artifacts_are_current_user_only(tmp_path: Path) -> None:
    # Given
    path = config_path(tmp_path)

    # When
    save_state(tmp_path, path, StateDocument(values={"enabled": True}))

    # Then
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
