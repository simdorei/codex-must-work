from __future__ import annotations

# noqa: SIZE_OK - cohesive Windows, POSIX, and workflow entrypoint contract

import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

_ROOT: Final = Path(__file__).parents[1]


def _powershell() -> Path:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return Path(executable)


def _posix_shell() -> Path:
    if os.name != "nt":
        return Path("/bin/sh")
    git = shutil.which("git")
    git_bash = Path(git).parents[1] / "bin" / "bash.exe" if git is not None else Path()
    candidates = (git_bash, Path("C:/Program Files/Git/bin/bash.exe"), Path("D:/Git/bin/bash.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("Git Bash is unavailable")


def _poison_python(path: Path, marker: Path) -> None:
    path.mkdir()
    for name in ("python", "python3", "py", "python.exe", "py.exe"):
        command = path / name
        _ = command.write_text(
            f"#!/bin/sh\nprintf hit > '{marker.as_posix()}'\nexit 99\n", encoding="utf-8"
        )
        command.chmod(0o755)


def _shell_resolve(shell: Path, path: Path) -> str:
    return subprocess.run(  # noqa: S603
        [shell, "-c", 'cd -- "$1" && pwd -P', "resolve", path.as_posix()],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _powershell_layout(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source root & [quoted] $value"
    launcher = source / "runtime" / "launch-python.ps1"
    installer = source / "scripts" / "install_plugin.py"
    launcher.parent.mkdir(parents=True)
    installer.parent.mkdir()
    _ = installer.write_text("", encoding="utf-8")
    _ = shutil.copy2(_ROOT / "install.ps1", source / "install.ps1")
    _ = launcher.write_text(
        """param()
$values = @(
    [string]$args.Count,
    [string]$args[0],
    [string]$args[1],
    [string]$args[2],
    [string]$env:CODEX_HOME,
    [string]$env:PLUGIN_DATA,
    [string]$env:PYTHONPATH,
    [string](Test-Path -LiteralPath $env:PLUGIN_DATA -PathType Container),
    [string](Get-Process -Id $PID).Path
)
[IO.File]::WriteAllLines($env:CMW_RECORD, $values, [Text.UTF8Encoding]::new($false))
[Console]::Out.WriteLine('entrypoint-stdout')
[Console]::Error.WriteLine('entrypoint-stderr')
if ($env:CMW_REPLACE_BOOTSTRAP -eq '1') {
    Remove-Item -LiteralPath $env:PLUGIN_DATA -Recurse -Force
    [IO.File]::WriteAllText($env:PLUGIN_DATA, 'replacement')
}
exit [int]$env:CMW_FAKE_EXIT
""",
        encoding="utf-8",
    )
    return source, launcher


def _shell_layout(tmp_path: Path) -> Path:
    source = tmp_path / "source root & [quoted] $value"
    launcher = source / "runtime" / "launch-python.sh"
    installer = source / "scripts" / "install_plugin.py"
    launcher.parent.mkdir(parents=True)
    installer.parent.mkdir()
    _ = installer.write_text("", encoding="utf-8")
    _ = shutil.copy2(_ROOT / "install.sh", source / "install.sh")
    _ = launcher.write_text(
        """#!/bin/sh
{
    printf '%s\\n' "$#" "$1" "$2" "$3" "$CODEX_HOME" "$PLUGIN_DATA" "$PYTHONPATH"
    if [ -d "$PLUGIN_DATA" ]; then printf 'True\\n'; else printf 'False\\n'; fi
} > "$CMW_RECORD"
printf 'entrypoint-stdout\\n'
printf 'entrypoint-stderr\\n' >&2
if [ "${CMW_REPLACE_BOOTSTRAP:-0}" = 1 ]; then
    rmdir "$PLUGIN_DATA"
    printf replacement > "$PLUGIN_DATA"
fi
exit "$CMW_FAKE_EXIT"
""",
        encoding="utf-8",
    )
    return source


def _assert_record(lines: list[str], source: str, home: str, temp_root: str, *, posix: bool) -> str:
    assert lines[:5] == [
        "3",
        f"{source}/scripts/install_plugin.py"
        if posix
        else str(Path(source) / "scripts/install_plugin.py"),
        home,
        source,
        home,
    ]
    plugin_data = lines[5]
    assert lines[6:8] == [source, "True"]
    parent = str(PurePosixPath(plugin_data).parent) if posix else str(Path(plugin_data).parent)
    name = PurePosixPath(plugin_data).name if posix else Path(plugin_data).name
    assert parent == temp_root
    assert name.startswith("cmw-installer-bootstrap.")
    return plugin_data


@pytest.mark.skipif(os.name != "nt", reason="Windows entrypoint runs on Windows")
@pytest.mark.parametrize("exit_code", [0, 37])
def test_windows_entrypoint_forwards_exact_invocation_when_path_is_poisoned(
    tmp_path: Path, exit_code: int
) -> None:
    # Given: a copied entrypoint, metacharacter paths, and fake PATH Python commands.
    source, _ = _powershell_layout(tmp_path)
    powershell = _powershell()
    home = tmp_path / "home & [configured] $value"
    home.mkdir()
    record = tmp_path / "windows-record.txt"
    marker = tmp_path / "python-was-used"
    poison = tmp_path / "poison"
    _poison_python(poison, marker)
    environment = os.environ | {
        "CODEX_HOME": str(home),
        "PLUGIN_DATA": "caller-value-must-not-leak",
        "CMW_RECORD": str(record),
        "CMW_FAKE_EXIT": str(exit_code),
        "PATH": f"{poison}{os.pathsep}{os.environ['PATH']}",
    }

    # When: Windows invokes the copied root installer.
    result = subprocess.run(  # noqa: S603
        [powershell, "-NoProfile", "-NonInteractive", "-File", source / "install.ps1"],
        check=False,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    # Then: the fake bundled launcher receives the exact safe contract and real result.
    lines = record.read_text(encoding="utf-8").splitlines()
    plugin_data = _assert_record(
        lines,
        str(source.resolve()),
        str(home.resolve()),
        str(Path(os.environ["TEMP"]).resolve()),
        posix=False,
    )
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (
        exit_code,
        "entrypoint-stdout",
        "entrypoint-stderr",
    )
    assert not marker.exists()
    assert not Path(plugin_data).exists()
    assert Path(lines[8]).resolve() == powershell.resolve()


def test_posix_entrypoint_forwards_exact_invocation_when_path_is_poisoned(tmp_path: Path) -> None:
    # Given: a copied entrypoint and recording launcher under a hostile Python PATH.
    source = _shell_layout(tmp_path)
    home = tmp_path / "home & [configured] $value"
    home.mkdir()
    record = tmp_path / "posix-record.txt"
    marker = tmp_path / "python-was-used"
    poison = tmp_path / "poison"
    _poison_python(poison, marker)
    shell = _posix_shell()
    temp_root = tmp_path / "os temp root & [quoted]"
    temp_root.mkdir()
    source_text = _shell_resolve(shell, source)
    home_text = _shell_resolve(shell, home)
    temp_text = _shell_resolve(shell, temp_root)
    environment = os.environ | {
        "CODEX_HOME": home_text,
        "PLUGIN_DATA": "caller-value-must-not-leak",
        "CMW_RECORD": record.as_posix(),
        "CMW_FAKE_EXIT": "29",
        "PATH": f"{poison}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": temp_root.as_posix(),
    }

    # When: the available POSIX shell invokes it.
    result = subprocess.run(  # noqa: S603
        [shell, "install.sh"],
        check=False,
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
    )

    # Then: quoting, child environment, streams, status, and cleanup are exact.
    plugin_data = _assert_record(
        record.read_text(encoding="utf-8").splitlines(),
        source_text,
        home_text,
        temp_text,
        posix=True,
    )
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (
        29,
        "entrypoint-stdout",
        "entrypoint-stderr",
    )
    assert not marker.exists()
    absent = subprocess.run(  # noqa: S603
        [shell, "-c", 'test ! -e "$1"', "cleanup", plugin_data], check=False
    )
    assert absent.returncode == 0


@pytest.mark.parametrize("entrypoint", ["install.ps1", "install.sh"])
def test_entrypoint_cleanup_refuses_replaced_bootstrap(tmp_path: Path, entrypoint: str) -> None:
    # Given: a launcher that replaces its bootstrap directory with an unowned file.
    if entrypoint.endswith(".sh") and os.name == "nt":
        pytest.skip("cross-shell deletion is intentionally avoided on Windows")
    source = (
        _shell_layout(tmp_path) if entrypoint.endswith(".sh") else _powershell_layout(tmp_path)[0]
    )
    shell = _posix_shell() if entrypoint.endswith(".sh") else _powershell()
    record = tmp_path / "cleanup-record.txt"
    environment = os.environ | {
        "CODEX_HOME": str(tmp_path / "home"),
        "CMW_RECORD": record.as_posix(),
        "CMW_FAKE_EXIT": "41",
        "CMW_REPLACE_BOOTSTRAP": "1",
    }
    command = (
        [shell, entrypoint]
        if entrypoint.endswith(".sh")
        else [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            source / entrypoint,
        ]
    )

    # When: verified cleanup sees the replacement.
    result = subprocess.run(  # noqa: S603
        command, check=False, cwd=source, env=environment, capture_output=True, text=True
    )

    # Then: cleanup fails explicitly and leaves the replacement untouched.
    replacement = Path(record.read_text(encoding="utf-8").splitlines()[5])
    assert result.returncode == 70
    assert "installer bootstrap cleanup failed" in result.stderr
    assert replacement.read_text(encoding="utf-8").strip() == "replacement"
    replacement.unlink()


def test_posix_workflow_is_push_only_and_candidate_bound() -> None:
    # Given: the JSON-formatted YAML workflow and its complete approved structure.
    workflow = json.loads(
        (_ROOT / ".github" / "workflows" / "installer-posix.yml").read_text(encoding="utf-8")
    )
    expected = {
        "name": "Native POSIX installer",
        "on": {"push": {"branches": ["**"]}},
        "permissions": {"contents": "read"},
        "jobs": {
            "ubuntu-x64": {
                "runs-on": "ubuntu-latest",
                "steps": [
                    {
                        "name": "Checkout candidate",
                        "uses": "actions/checkout@v4",
                        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
                    },
                    {
                        "name": "Verify candidate SHA",
                        "shell": "bash",
                        "run": (
                            'test "$(git rev-parse HEAD)" = "${{ github.sha }}"\n'
                            'test "$(uname -s)" = "Linux"\n'
                            'test "$(uname -m)" = "x86_64"\n'
                            "sh -n install.sh\n"
                        ),
                    },
                    {
                        "name": "Run native installer smoke",
                        "shell": "bash",
                        "run": "python3.12 tests/native_posix_install_smoke.py\n",
                    },
                ],
            },
            "macos-arm64": {
                "runs-on": "macos-14",
                "steps": [
                    {
                        "name": "Checkout candidate",
                        "uses": "actions/checkout@v4",
                        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
                    },
                    {
                        "name": "Verify candidate SHA",
                        "shell": "bash",
                        "run": (
                            'test "$(git rev-parse HEAD)" = "${{ github.sha }}"\n'
                            'test "$(uname -s)" = "Darwin"\n'
                            'test "$(uname -m)" = "arm64"\n'
                            "sh -n install.sh\n"
                        ),
                    },
                    {
                        "name": "Run native installer smoke",
                        "shell": "bash",
                        "run": "python3.12 tests/native_posix_install_smoke.py\n",
                    },
                ],
            },
        },
    }

    # When / Then: parsing yields exactly the approved keys, values, and list order.
    assert workflow == expected
