from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_portable_runtime import ROOT


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_bootstraps_embedded_python_without_path(
    tmp_path: Path,
) -> None:
    # Given: no PATH lookup and an empty writable plugin-data directory.
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
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Then: CPython 3.12 runs without a system interpreter on PATH.
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [3, 12]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_ignores_hostile_pythonpath_sitecustomize(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    _ = (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")
    environment["PYTHONPATH"] = str(hostile)

    result = subprocess.run(  # noqa: S603
        [
            str(ROOT / "runtime" / "launch-python.exe"),
            "-c",
            "import json,sys;print(json.dumps({'isolated':sys.flags.isolated}))",
        ],
        check=False,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"isolated": 1}
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher E2E runs only on Windows")
def test_windows_launcher_blocks_preloaded_allowlisted_mcp_module(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    _ = (hostile / "sitecustomize.py").write_text(
        """import sys, types
module = types.ModuleType('scripts.mcp_server')
def main(argv=None):
    print('IMPORT_POLICY_BYPASSED')
    return 0
module.main = main
sys.modules['scripts.mcp_server'] = module
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(tmp_path / "codex-home")
    environment["PLUGIN_DATA"] = str(tmp_path / "plugin-data")
    environment["PYTHONPATH"] = str(hostile)
    (tmp_path / "codex-home").mkdir()

    result = subprocess.run(  # noqa: S603
        [str(ROOT / "runtime" / "launch-python.exe"), "scripts/mcp_bootstrap.py"],
        check=False,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "IMPORT_POLICY_BYPASSED" not in result.stdout
