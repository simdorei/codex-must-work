from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.portable_runtime_native_support import clean_native_mcp_probe
from tests.test_portable_runtime import ROOT, JsonObject


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_forwards_warning_option_to_python(tmp_path: Path) -> None:
    # Given: a Python command containing the CLI option that overlaps PowerShell common parameters.
    launcher = ROOT / "runtime" / "launch-python.ps1"
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
        cwd=ROOT,
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
    launcher = ROOT / "runtime" / "launch-python.ps1"
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
        cwd=ROOT,
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
    _ = shutil.copy2(ROOT / "runtime/launch-python.exe", runtime / "launch-python.exe")
    _ = shutil.copy2(ROOT / "runtime/launch-python.ps1", runtime / "launch-python.ps1")
    archive_name = "cpython-3.12.13+20260510-windows-x64.tar.gz"
    _ = shutil.copy2(ROOT / "runtime/archives" / archive_name, archives / archive_name)
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
def test_native_windows_launcher_emits_utf8_bytes(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")
    environment["PATH"] = ""

    result = subprocess.run(  # noqa: S603
        [
            str(ROOT / "runtime/launch-python.exe"),
            "-c",
            r"print('\ud55c\uae00-UTF8')",
        ],
        check=False,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8").splitlines() == ["한글-UTF8"]


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
    initialize, tools = clean_native_mcp_probe(
        ROOT / "runtime/launch-python.exe",
        "scripts/mcp_bootstrap.py",
        cwd=ROOT,
        environment=environment,
        data_root=plugin_data,
        requests=requests,
    )

    assert "result" in initialize
    assert "result" in tools
    assert (plugin_data / ".private-root-v1").read_bytes() == b"private-root-v1\n"
    assert (plugin_data / "control.key").is_file()
