from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
from typing import TYPE_CHECKING

import pytest

from scripts.control_capability import (
    ControlKeyError,
    derive_control_capability,
    load_control_key,
    provision_control_key,
    verify_control_capability,
)
from scripts.private_root import ensure_private_root

if TYPE_CHECKING:
    from pathlib import Path


def test_provision_control_key_creates_private_direct_random_key(tmp_path: Path) -> None:
    # Given
    plugin_data = tmp_path / "plugin-data"
    state_root = tmp_path / "state"

    # When
    key = provision_control_key(plugin_data, state_root)

    # Then
    key_path = plugin_data / "control.key"
    metadata = key_path.lstat()
    assert len(key) == 32
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    if os.name != "nt":
        assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert load_control_key(plugin_data) == key


def test_provision_control_key_preserves_valid_key(tmp_path: Path) -> None:
    # Given
    plugin_data = tmp_path / "plugin-data"
    state_root = tmp_path / "state"
    original = provision_control_key(plugin_data, state_root)
    identity = (plugin_data / "control.key").stat()

    # When
    loaded = provision_control_key(plugin_data, state_root)

    # Then
    preserved = (plugin_data / "control.key").stat()
    assert loaded == original
    assert (preserved.st_dev, preserved.st_ino) == (identity.st_dev, identity.st_ino)


@pytest.mark.parametrize("key_state", ["missing", "invalid"])
def test_provision_control_key_requires_recovery_when_active_state_exists(
    tmp_path: Path,
    key_state: str,
) -> None:
    # Given
    plugin_data = tmp_path / "plugin-data"
    state_root = tmp_path / "state"
    runtime = state_root / "runtime"
    runtime.mkdir(parents=True)
    _ = (runtime / "active.json").write_text("{}", encoding="utf-8")
    if key_state == "invalid":
        ensure_private_root(plugin_data)
        _ = (plugin_data / "control.key").write_bytes(b"invalid")
        before = (plugin_data / "control.key").read_bytes()
    else:
        before = None

    # When
    with pytest.raises(ControlKeyError) as raised:
        _ = provision_control_key(plugin_data, state_root)

    # Then
    assert raised.value.reason_code == "control_key_recovery_required"
    key_path = plugin_data / "control.key"
    assert (key_path.read_bytes() if key_path.exists() else None) == before


def test_provision_control_key_replaces_invalid_key_without_active_state(
    tmp_path: Path,
) -> None:
    # Given
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    invalid = b"invalid"
    _ = (plugin_data / "control.key").write_bytes(invalid)

    # When
    key = provision_control_key(plugin_data, tmp_path / "state")

    # Then
    assert len(key) == 32
    assert key != invalid
    assert load_control_key(plugin_data) == key


def test_capability_uses_exact_domain_and_canonical_base64url(tmp_path: Path) -> None:
    # Given
    key = provision_control_key(tmp_path / "plugin-data", tmp_path / "state")
    session_id = "session-a"
    message = (
        b"cmw-control-v1\\0"
        + session_id.encode("utf-8")
        + b"\\0codex-must-work@codex-must-work-local"
    )
    expected_digest = hmac.new(key, message, hashlib.sha256).digest()

    # When
    capability = derive_control_capability(key, session_id)

    # Then
    assert len(capability) == 43
    assert "=" not in capability
    assert hmac.compare_digest(
        capability,
        base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii"),
    )
    assert verify_control_capability(key, session_id, capability)
    assert not verify_control_capability(key, "session-b", capability)


@pytest.mark.parametrize(
    ("session_id", "capability"),
    [
        ("", ""),
        ("session-a", ""),
        ("session-a", "not-base64url"),
        ("session-a", "é" * 43),
        pytest.param("x" * 65_537, "x" * 43, id="overlong-session"),
    ],
)
def test_verify_control_capability_rejects_malformed_values(
    tmp_path: Path,
    session_id: str,
    capability: str,
) -> None:
    # Given
    key = provision_control_key(tmp_path / "plugin-data", tmp_path / "state")

    # When
    verified = verify_control_capability(key, session_id, capability)

    # Then
    assert verified is False
