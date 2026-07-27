from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import cast

import pytest

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_ROOT = Path(__file__).parents[1]
_ARCHIVES = {
    "cpython-3.12.13+20260510-windows-x64.tar.gz": (
        "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0"
    ),
    "cpython-3.12.13+20260510-linux-x64.tar.gz": (
        "d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88"
    ),
    "cpython-3.12.13+20260510-macos-arm64.tar.gz": (
        "55bc1a5edbc8ac4da0081f4f5731ed2d1ed10c57cb37a820b2a0dbc7cad742e9"
    ),
}


def test_runtime_bundle_contains_all_pinned_archives() -> None:
    # Given / When: the packaged runtime archives are hashed.
    actual = {
        name: hashlib.sha256((_ROOT / "runtime" / "archives" / name).read_bytes()).hexdigest()
        for name in _ARCHIVES
    }

    # Then: every approved target is present with its release digest.
    assert actual == _ARCHIVES


def test_runtime_archives_contain_executables_and_licenses() -> None:
    expected = {
        "cpython-3.12.13+20260510-windows-x64.tar.gz": (
            "python/python.exe",
            "python/LICENSE.txt",
        ),
        "cpython-3.12.13+20260510-linux-x64.tar.gz": (
            "python/bin/python3.12",
            "python/lib/python3.12/LICENSE.txt",
        ),
        "cpython-3.12.13+20260510-macos-arm64.tar.gz": (
            "python/bin/python3.12",
            "python/lib/python3.12/LICENSE.txt",
        ),
    }

    for archive_name, (executable, license_file) in expected.items():
        with tarfile.open(_ROOT / "runtime" / "archives" / archive_name, "r:gz") as archive:
            executable_info = archive.getmember(executable)
            assert archive.getmember(license_file).isfile()
            assert executable_info.isfile()
            if "/bin/" in executable:
                assert executable_info.mode & 0o111


def test_hooks_use_only_the_portable_runtime_launcher() -> None:
    # Given: the installed hook command configuration.
    hooks = (_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")

    # When / Then: no system Python command remains at the bootstrap boundary.
    assert "python3 " not in hooks
    assert "py -3 " not in hooks
    assert hooks.count("launch-python") == 2
    assert hooks.count("-ForwardStdin") == 1


def test_hooks_register_only_low_frequency_lifecycle_events() -> None:
    hooks = cast(
        "dict[str, object]",
        json.loads((_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")),
    )
    registered = cast("dict[str, object]", hooks["hooks"])

    assert set(registered) == {"SessionStart"}


def test_skills_use_only_the_portable_runtime_launcher() -> None:
    skill_root = _ROOT / "skills"
    instructions = "\n".join(
        (skill_root / name / "SKILL.md").read_text(encoding="utf-8")
        for name in ("work-on", "work-off", "work-calibration")
    )

    assert "py -3 " not in instructions
    assert "python3 " not in instructions
    assert instructions.count("launch-python") >= 5
    assert "PLUGIN_DATA" in instructions


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_bootstraps_embedded_python_without_path(
    tmp_path: Path,
) -> None:
    # Given: no PATH lookup and an empty writable plugin-data directory.
    launcher = _ROOT / "runtime" / "launch-python.ps1"
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = os.environ.copy()
    environment["PATH"] = ""
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")

    # When: the launcher starts Python from the bundled archive.
    result = subprocess.run(  # noqa: S603
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-c",
            "import json,sys;print(json.dumps(list(sys.version_info[:2])))",
        ],
        check=False,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Then: CPython 3.12 runs without a system interpreter on PATH.
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [3, 12]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_forwards_warning_option_to_python(tmp_path: Path) -> None:
    # Given: a Python command containing the CLI option that overlaps PowerShell common parameters.
    launcher = _ROOT / "runtime" / "launch-python.ps1"
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = os.environ.copy()
    environment["PATH"] = ""
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")

    # When: the portable launcher forwards the command.
    result = subprocess.run(  # noqa: S603
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-c",
            "import json,sys;print(json.dumps(sys.argv[1:]))",
            "--warning",
            "90s",
            "--restart",
            "5m",
        ],
        check=False,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Then: Python receives every option literally and in order.
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["--warning", "90s", "--restart", "5m"]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_forwards_hook_stdin(tmp_path: Path) -> None:
    launcher = _ROOT / "runtime" / "launch-python.ps1"
    probe = tmp_path / "stdin_probe.py"
    _ = probe.write_text(
        "import sys\n_ = sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = os.environ.copy()
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")
    payload = json.dumps(
        {
            "session_id": "qa-session",
            "hook_event_name": "SessionStart",
            "transcript_path": str(tmp_path / "rollout.jsonl"),
            "permission_mode": "dontAsk",
        }
    )

    result = subprocess.run(  # noqa: S603
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-ForwardStdin",
            str(probe),
        ],
        input=payload,
        check=False,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == payload


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_native_windows_launcher_prepares_once_and_derives_simdorei_data_root(
    tmp_path: Path,
) -> None:
    version = "0.2.0+codex.20260728010101"
    plugin_root = tmp_path / "plugins" / "cache" / "simdorei" / "codex-must-work" / version
    runtime = plugin_root / "runtime"
    archives = runtime / "archives"
    archives.mkdir(parents=True)
    _ = shutil.copy2(_ROOT / "runtime/launch-python.exe", runtime / "launch-python.exe")
    _ = shutil.copy2(_ROOT / "runtime/launch-python.ps1", runtime / "launch-python.ps1")
    archive_name = "cpython-3.12.13+20260510-windows-x64.tar.gz"
    _ = shutil.copy2(_ROOT / "runtime/archives" / archive_name, archives / archive_name)
    environment = os.environ.copy()
    _ = environment.pop("PLUGIN_DATA", None)
    environment["PATH"] = ""
    launcher = runtime / "launch-python.exe"
    data_root = tmp_path / "plugins" / "data" / "codex-must-work-simdorei"
    data_root.parent.mkdir(parents=True)

    first = subprocess.run(  # noqa: S603
        [str(launcher), "-c", "print('prepared')"],
        check=False,
        cwd=plugin_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (runtime / "launch-python.ps1").unlink()
    second = subprocess.run(  # noqa: S603
        [str(launcher), "-c", "print('reused')"],
        check=False,
        cwd=plugin_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "prepared"
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "reused"
    assert (data_root / ".private-root-v1").read_bytes() == b"private-root-v1\n"
    assert (
        data_root / "portable-python" / "3.12.13+20260510" / "windows-x64" / "python/python.exe"
    ).is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_clean_native_launcher_provisions_and_initializes_mcp(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    plugin_data = tmp_path / "plugin-data"
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["PLUGIN_DATA"] = str(plugin_data)
    environment["PATH"] = ""
    requests: tuple[JsonObject, ...] = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "clean-install-qa", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    stdin = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)

    result = subprocess.run(  # noqa: S603
        [str(_ROOT / "runtime/launch-python.exe"), "scripts/mcp_server.py"],
        input=stdin,
        check=False,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    responses = [cast("JsonObject", json.loads(line)) for line in result.stdout.splitlines()]
    assert result.returncode == 0, result.stderr
    assert any(response.get("id") == 1 and "result" in response for response in responses)
    assert any(response.get("id") == 2 and "result" in response for response in responses)
    assert (plugin_data / ".private-root-v1").read_bytes() == b"private-root-v1\n"
    assert (plugin_data / "control.key").is_file()
