from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import install_plugin
from scripts import private_root as private_root_module
from scripts.cache_types import identity
from scripts.codex_compatibility import CompatibilityResult
from scripts.hook_trust import read_plugin_manifest
from scripts.installer_mcp_runtime import McpRuntimePublication
from scripts.session_hook import process_session_start
from tests.hook_fixture import hook_event
from tests.live_discord_e2e_audit_records import decode_json

if TYPE_CHECKING:
    import pytest


THREAD = "1528639615592828980"


def test_isolated_trusted_install_locator_and_read_only_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _lightweight_candidate(tmp_path)
    home = (tmp_path / "codex-home").resolve()
    home.mkdir()
    compatibility = CompatibilityResult.for_tests(home / "fake-codex")

    def compatible(
        _codex_home: Path,
        _source_root: Path,
        *,
        require_plugins: bool = False,
        expected: CompatibilityResult | None = None,
    ) -> CompatibilityResult:
        _ = require_plugins, expected
        return compatibility

    def prepare_runtime(_source: Path, data_root: Path) -> McpRuntimePublication:
        runtime_root = data_root / "test-runtime"
        runtime_root.mkdir()
        return McpRuntimePublication(
            runtime_root, identity(runtime_root.stat()), created_by_run=True
        )

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", compatible)
    monkeypatch.setattr(install_plugin, "prepare_mcp_runtime", prepare_runtime)
    installed = install_plugin.install(home, source)
    assert installed.install_ok
    plugin_root = _installed_root(home, source)
    plugin_data = home / "plugins" / "data" / "codex-must-work-simdorei"
    active_state_root = home / "codex-must-work"
    private_root_module.ensure_private_root(active_state_root)
    assert (active_state_root / ".private-root-v1").read_bytes() == b"private-root-v1\n"
    rollout = home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir()

    located = process_session_start(
        hook_event(
            "SessionStart",
            transcript_path=str(rollout),
            permission_mode="dontAsk",
        ),
        root=home / "codex-must-work",
        plugin_root=plugin_root,
        plugin_data=plugin_data,
    )
    assert located is None
    assert not rollout.exists()


def _lightweight_candidate(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "candidate"
    manifest_path = source / "runtime" / "package-files.json"
    raw_paths = decode_json(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(raw_paths, list)
    listed = {
        value
        for value in raw_paths
        if isinstance(value, str) and not value.startswith("runtime/archives/")
    }
    listed.update(path.relative_to(source).as_posix() for path in (source / "scripts").glob("*.py"))
    paths = tuple(sorted(listed, key=str.encode))
    for relative in paths:
        source_file = source.joinpath(*relative.split("/"))
        destination = candidate.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(source_file, destination)
    mcp = {
        "mcpServers": {
            "codex-must-work": {
                "command": sys.executable,
                "args": [
                    "-B",
                    "scripts/mcp_server.py",
                    "--plugin-data",
                    "../../../../data/codex-must-work-simdorei",
                ],
                "env": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            }
        }
    }
    _ = (candidate / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    _ = (candidate / "runtime" / "package-files.json").write_text(
        json.dumps(sorted(paths, key=str.encode)), encoding="utf-8"
    )
    return candidate.resolve()


def _installed_root(home: Path, source: Path) -> Path:
    version = read_plugin_manifest(source).version
    return home / "plugins" / "cache" / "simdorei" / "codex-must-work" / version
