from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cmw_process_probe_io import (
    LocatorError,
    OutputPathError,
    load_session_locator,
    parse_output_path,
)


def _installed_root(tmp_path: Path) -> Path:
    root = tmp_path / "installed"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "scripts").mkdir()
    _ = (root / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    _ = (root / ".mcp.json").write_text("{}", encoding="utf-8")
    _ = (root / "scripts" / "mcp_server.py").write_text("# installed", encoding="utf-8")
    return root.resolve()


def _write_rollout(
    path: Path,
    root: Path,
    capability: str = "S" * 43,
    *,
    transcript_path: Path | None = None,
) -> None:
    locator = json.dumps(
        {
            "codex_must_work_locator": {
                "session_id": "session-a",
                "transcript_path": str((transcript_path or path).resolve()),
                "plugin_root": str(root),
                "plugin_data": str(root.parent / "data"),
                "control_capability": capability,
                "permission_mode": "never",
            }
        }
    )
    record = {
        "type": "event_msg",
        "payload": {
            "type": "hook_completed",
            "run": {
                "event_name": "session_start",
                "entries": [{"kind": "context", "text": locator}],
            },
        },
    }
    _ = path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_locator_reads_exact_session_and_rollout_without_exposing_capability(
    tmp_path: Path,
) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    root = _installed_root(tmp_path)
    secret = "Q" * 43
    _write_rollout(rollout, root, secret)

    # When
    locator = load_session_locator(rollout, "session-a")

    # Then
    assert locator.plugin_root == root
    assert locator.control_capability == secret
    assert secret not in repr(locator)


@pytest.mark.parametrize("mutation", ["session", "rollout", "root"])
def test_locator_rejects_stale_or_indirect_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    root = _installed_root(tmp_path)
    _write_rollout(rollout, root)
    session = "session-a"
    if mutation == "session":
        session = "session-b"
    elif mutation == "rollout":
        _write_rollout(rollout, root, transcript_path=tmp_path / "other.jsonl")
    else:
        (root / "scripts" / "mcp_server.py").unlink()

    # When / Then
    with pytest.raises(LocatorError) as raised:
        _ = load_session_locator(rollout, session)
    assert "Q" * 43 not in str(raised.value)


@pytest.mark.parametrize(
    "name",
    ["relative.json", "result.txt", "missing/result.json"],
)
def test_output_path_rejects_unvalidated_targets(tmp_path: Path, name: str) -> None:
    # Given
    path = Path(name) if name == "relative.json" else tmp_path / name

    # When / Then
    with pytest.raises(OutputPathError):
        _ = parse_output_path(path)
