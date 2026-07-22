from __future__ import annotations

import ctypes
import os
import struct
import tempfile
from pathlib import Path

import pytest

import scripts.config_metadata as metadata_module
from scripts.config_metadata import (
    ATTRIBUTE_SECURITY_INFORMATION,
    BASE_SECURITY_INFORMATION,
    LABEL_SECURITY_INFORMATION,
    WindowsMetadataApi,
)
from scripts.config_publication import read_config_bytes, write_config_bytes
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import installer_lock
from scripts.windows_file import windows_handle

_UNSUPPORTED = "codex_config_metadata_unsupported"


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


def _set_windows_ea(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    name, value = b"cmw", b"present"
    raw = struct.pack("<IBBH", 0, 0, len(name), len(value)) + name + b"\0" + value
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    status = (ctypes.c_void_p * 2)()
    try:
        result = WindowsMetadataApi().set_ea(
            windows_handle(descriptor),
            ctypes.c_void_p(ctypes.addressof(status)),
            ctypes.c_void_p(ctypes.addressof(buffer)),
            len(raw),
        )
    finally:
        os.close(descriptor)
    assert result == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows special metadata")
@pytest.mark.parametrize("kind", ["stream", "extended_attribute"])
def test_windows_special_metadata_is_rejected_without_loss(home: Path, kind: str) -> None:
    path = home / "config.toml"
    raw = b"before\n"
    _ = path.write_bytes(raw)
    if kind == "stream":
        _ = Path(f"{path}:cmw").write_bytes(b"named")
    else:
        _set_windows_ea(path)
    with installer_lock(home) as lease, pytest.raises(InstallPluginError) as caught:
        _ = read_config_bytes(home, lease)
    assert _reason(caught) == _UNSUPPORTED
    assert path.read_bytes() == raw


@pytest.mark.skipif(os.name != "nt", reason="Windows supported security")
@pytest.mark.parametrize(
    "unreadable",
    [BASE_SECURITY_INFORMATION, LABEL_SECURITY_INFORMATION, ATTRIBUTE_SECURITY_INFORMATION],
)
def test_unreadable_windows_security_fails_before_replacement(
    home: Path, monkeypatch: pytest.MonkeyPatch, unreadable: int
) -> None:
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    read_sddl = metadata_module.read_windows_sddl

    def reject(api: WindowsMetadataApi, handle: ctypes.c_void_p, flags: int) -> str:
        if flags == unreadable:
            raise InstallPluginError(_UNSUPPORTED)
        return read_sddl(api, handle, flags)

    monkeypatch.setattr(metadata_module, "read_windows_sddl", reject)
    with installer_lock(home) as lease, pytest.raises(InstallPluginError) as caught:
        _ = read_config_bytes(home, lease)
    assert _reason(caught) == _UNSUPPORTED
    assert path.read_bytes() == b"before\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata policy")
def test_windows_security_masks_exclude_audit_sacl(home: Path) -> None:
    assert BASE_SECURITY_INFORMATION & 0x08 == 0
    assert LABEL_SECURITY_INFORMATION & 0x08 == 0
    assert ATTRIBUTE_SECURITY_INFORMATION & 0x08 == 0
    path = home / "config.toml"
    _ = path.write_bytes(b"before\n")
    with installer_lock(home) as lease:
        before = read_config_bytes(home, lease)
        _ = write_config_bytes(lease, before, b"after\n")
        after = read_config_bytes(home, lease)
    assert after.metadata == before.metadata
