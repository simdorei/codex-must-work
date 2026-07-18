from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from scripts import private_root
from scripts.private_root import PrivateRootError, PrivateRootReason, ensure_private_root

_TAMPER_ACL_SOURCE: Final = r"""
$ErrorActionPreference='Stop'
$path=[Environment]::GetEnvironmentVariable('CODEX_MUST_WORK_PRIVATE_ROOT','Process')
$security=[IO.Directory]::GetAccessControl($path)
$sid=New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$inheritance=[Security.AccessControl.InheritanceFlags]::ContainerInherit `
  -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
$rule=New-Object Security.AccessControl.FileSystemAccessRule(`
  $sid,`
  [Security.AccessControl.FileSystemRights]::ReadAndExecute,`
  $inheritance,`
  [Security.AccessControl.PropagationFlags]::None,`
  [Security.AccessControl.AccessControlType]::Allow)
$security.AddAccessRule($rule)
[IO.Directory]::SetAccessControl($path,$security)
""".strip()


def test_new_root_is_initialized_with_version_marker(tmp_path: Path) -> None:
    root = tmp_path / "state"

    ensure_private_root(root)

    assert root.is_dir()
    assert (root / ".private-root-v1").is_file()


def test_existing_root_without_marker_requires_explicit_migration(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    with pytest.raises(PrivateRootError) as captured:
        ensure_private_root(root)

    assert captured.value.reason is PrivateRootReason.MIGRATION_REQUIRED
    assert not (root / ".private-root-v1").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration")
def test_path_lookup_cannot_replace_trusted_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    execution_marker = tmp_path / "fake-powershell-ran"
    _ = (fake_bin / "powershell.bat").write_text(
        f'@echo exploited>"{execution_marker}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(fake_bin))

    ensure_private_root(tmp_path / "state")

    assert not execution_marker.exists()


def test_root_replaced_during_initialization_is_never_marked_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"

    def replace_root(target: Path, _mode: str) -> None:
        target.rmdir()
        target.mkdir()

    monkeypatch.setattr(private_root, "_secure_root", replace_root)

    with pytest.raises(PrivateRootError) as captured:
        ensure_private_root(root)

    assert captured.value.reason is PrivateRootReason.PATH_UNSAFE
    assert root.is_dir()
    assert not (root / ".private-root-v1").exists()


def test_failed_security_setup_removes_a_new_empty_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"

    def fail_security(target: Path, _mode: str) -> None:
        raise PrivateRootError(target, PrivateRootReason.ACL_APPLY_FAILED, 99)

    monkeypatch.setattr(private_root, "_secure_root", fail_security)

    with pytest.raises(PrivateRootError):
        ensure_private_root(root)

    assert not root.exists()


def test_failed_security_setup_never_deletes_foreign_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"

    def fail_after_foreign_write(target: Path, _mode: str) -> None:
        _ = (target / "foreign.txt").write_text("keep", encoding="utf-8")
        raise PrivateRootError(target, PrivateRootReason.ACL_APPLY_FAILED, 99)

    monkeypatch.setattr(private_root, "_secure_root", fail_after_foreign_write)

    with pytest.raises(PrivateRootError):
        ensure_private_root(root)

    assert (root / "foreign.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration")
def test_existing_root_with_extra_access_rule_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    ensure_private_root(root)
    _add_everyone_access_rule(root)

    with pytest.raises(PrivateRootError) as captured:
        ensure_private_root(root)

    assert captured.value.reason is PrivateRootReason.ACL_VERIFY_FAILED
    assert captured.value.detail_code == 13
    assert (root / ".private-root-v1").is_file()


def _add_everyone_access_rule(root: Path) -> None:
    system_directory = Path(os.environ["SYSTEMROOT"]) / "System32"
    powershell = system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    environment = os.environ.copy()
    environment["CODEX_MUST_WORK_PRIVATE_ROOT"] = str(root)
    encoded = base64.b64encode(_TAMPER_ACL_SOURCE.encode("utf-16le")).decode("ascii")
    result = subprocess.run(  # noqa: S603
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        check=False,
        cwd=system_directory,
        env=environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15.0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
