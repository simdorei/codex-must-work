from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import hook_event as hook_event_module
from scripts import install_plugin
from scripts.cache_types import identity
from scripts.calibration import CalibrationRecommendation
from scripts.codex_compatibility import CompatibilityResult
from scripts.durations import Milliseconds
from scripts.hook_event import process_hook
from scripts.hook_payload import SessionLocator, serialize_locator
from scripts.hook_trust import read_plugin_manifest
from scripts.installer_mcp_runtime import McpRuntimePublication
from tests import live_discord_e2e_audit_runtime as runtime
from tests.hook_fixture import hook_event
from tests.live_discord_e2e_audit_preflight import (
    evaluate_preflight,
    load_mapping,
    load_preflight_locator,
)
from tests.live_discord_e2e_audit_records import decode_json
from tests.live_discord_e2e_audit_runtime import collect_preflight

if TYPE_CHECKING:
    from datetime import datetime

    import pytest

    from scripts.state_io import JsonValue

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
    plugin_data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
    rollout = home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir()

    def scan(_codex_home: Path, _now: datetime) -> CalibrationRecommendation:
        return CalibrationRecommendation(20, Milliseconds(60_000), Milliseconds(120_000))

    monkeypatch.setattr(
        hook_event_module,
        "_scan_history",
        scan,
    )
    located = process_hook(
        hook_event(
            "SessionStart",
            transcript_path=str(rollout),
            permission_mode="dontAsk",
        ),
        root=home / "codex-must-work",
        plugin_root=plugin_root,
        plugin_data=plugin_data,
    )
    assert isinstance(located, SessionLocator)
    _write_locator_rollout(rollout, located)
    parsed = load_preflight_locator(rollout)
    mapped = _mapped_thread(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))

    def read_app(_thread_id: str) -> tuple[str, None]:
        return "session-1", None

    monkeypatch.setattr(runtime, "read_app_server", read_app)

    snapshot = collect_preflight(parsed, THREAD, mapped, located.package_digest_sha256)
    result = evaluate_preflight(snapshot)

    assert result.ready is True
    assert snapshot.actual_package_digest_sha256 == located.package_digest_sha256
    assert snapshot.cmw_authenticated is True
    assert snapshot.cmw_active is False


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
                    "../../../../data/codex-must-work-codex-must-work-local",
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
    return home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / version


def _write_locator_rollout(path: Path, locator: SessionLocator) -> None:
    envelope = decode_json(serialize_locator(locator))
    assert isinstance(envelope, dict)
    output = envelope["hookSpecificOutput"]
    assert isinstance(output, dict)
    context = output["additionalContext"]
    assert isinstance(context, str)
    rows: list[dict[str, JsonValue]] = [
        {"type": "session_meta", "payload": {"id": locator.session_id}},
        {
            "type": "event_msg",
            "payload": {
                "type": "hook_completed",
                "run": {
                    "event_name": "session_start",
                    "entries": [{"kind": "context", "text": context}],
                },
            },
        },
    ]
    _ = path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _mapped_thread(tmp_path: Path) -> str:
    database = tmp_path / "discord_mirror.sqlite"
    with sqlite3.connect(database) as connection:
        _ = connection.execute(
            "CREATE TABLE mirror_threads (codex_thread_id TEXT, discord_thread_id INTEGER)"
        )
        _ = connection.execute(
            "INSERT INTO mirror_threads VALUES (?, ?)", ("session-1", int(THREAD))
        )
    return load_mapping(database, THREAD)
