from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

_ROOT: Final = Path(__file__).parents[1]


@pytest.mark.skipif(os.name != "nt", reason="PowerShell entrypoint runs on Windows")
def test_uninstall_entrypoint_uses_isolated_utf8_runtime_and_forwards_purge(
    tmp_path: Path,
) -> None:
    # Given
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    source = tmp_path / "source root & [literal] $value"
    runtime = source / "runtime"
    scripts = source / "scripts"
    runtime.mkdir(parents=True)
    scripts.mkdir()
    _ = shutil.copy2(_ROOT / "uninstall.ps1", source / "uninstall.ps1")
    _ = (scripts / "uninstall_plugin.py").write_text("", encoding="utf-8")
    record = tmp_path / "uninstall-entrypoint.txt"
    _ = (runtime / "launch-python.ps1").write_text(
        """
[IO.Directory]::CreateDirectory($env:PLUGIN_DATA) | Out-Null
$values = @(
    [string]$args.Count,
    [string]$args[0],
    [string]$args[1],
    [string]$args[2],
    [string]$args[3],
    [string]$env:PYTHONUTF8,
    [string]$env:PLUGIN_DATA
)
[IO.File]::WriteAllLines($env:CMW_RECORD, $values, [Text.UTF8Encoding]::new($false))
exit 0
""",
        encoding="utf-8",
    )
    home = tmp_path / "home & [literal]"
    home.mkdir()
    environment = os.environ | {"CODEX_HOME": str(home), "CMW_RECORD": str(record)}

    # When
    result = subprocess.run(  # noqa: S603
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            source / "uninstall.ps1",
            "-PurgeData",
        ],
        check=False,
        env=environment,
        capture_output=True,
        text=True,
    )

    # Then
    lines = record.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 0
    assert lines[:6] == [
        "4",
        str((scripts / "uninstall_plugin.py").resolve()),
        str(home.resolve()),
        str(source.resolve()),
        "--purge-data",
        "1",
    ]
    assert not Path(lines[6]).exists()


def test_posix_uninstall_entrypoint_forwards_purge_in_temp_home(tmp_path: Path) -> None:
    # Given
    shell = shutil.which("sh")
    wsl = shutil.which("wsl") if shell is None else None
    if shell is None and wsl is None:
        pytest.skip("POSIX shell is unavailable")
    if wsl is not None:
        probe = subprocess.run(  # noqa: S603
            [wsl, "sh", "-c", "true"],
            check=False,
            capture_output=True,
        )
        if probe.returncode != 0:
            pytest.skip("WSL has no runnable POSIX distribution")
    source = tmp_path / "source"
    runtime = source / "runtime"
    scripts = source / "scripts"
    runtime.mkdir(parents=True)
    scripts.mkdir()
    _ = shutil.copy2(_ROOT / "uninstall.sh", source / "uninstall.sh")
    _ = (scripts / "uninstall_plugin.py").write_text("", encoding="utf-8")
    record = tmp_path / "posix-uninstall.txt"
    _ = (runtime / "launch-python.sh").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$#" "$1" "$2" "$3" "$4" "$PLUGIN_DATA" > "$CMW_RECORD"\n',
        encoding="utf-8",
        newline="\n",
    )
    (runtime / "launch-python.sh").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ | {"CODEX_HOME": str(home), "CMW_RECORD": str(record)}

    # When
    if shell is not None:
        command = [shell, source / "uninstall.sh", "--purge-data"]
    else:
        assert wsl is not None

        def wsl_path(path: Path) -> str:
            converted = subprocess.run(  # noqa: S603
                [wsl, "wslpath", "-a", str(path)],
                check=True,
                capture_output=True,
            )
            return converted.stdout.decode("utf-8", errors="strict").strip()

        command = [
            wsl,
            "env",
            f"CODEX_HOME={wsl_path(home)}",
            f"CMW_RECORD={wsl_path(record)}",
            "TMPDIR=/tmp",
            "sh",
            wsl_path(source / "uninstall.sh"),
            "--purge-data",
        ]
    result = subprocess.run(  # noqa: S603
        command,
        check=False,
        env=environment,
        capture_output=True,
        text=True,
    )

    # Then
    assert result.returncode == 0, result.stderr
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "4"
    assert lines[4] == "--purge-data"
    assert not Path(lines[5]).exists()
