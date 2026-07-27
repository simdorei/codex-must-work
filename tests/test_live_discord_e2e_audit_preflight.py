from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING, Never, Self

import pytest

from scripts import installed_generation
from scripts.app_server_client import AppServerError
from tests import live_discord_e2e_audit_runtime as runtime
from tests.live_discord_e2e_audit import parse_args
from tests.live_discord_e2e_audit_preflight import (
    PreflightError,
    PreflightLocator,
    PreflightSnapshot,
    evaluate_preflight,
    load_mapping,
    load_preflight_locator,
)
from tests.live_discord_e2e_audit_runtime import (
    read_app_server,
    read_cmw_status,
    validate_installed_generation,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

THREAD = "1528639615592828980"
SESSION = "session-plan"
CODEX_THREAD = "codex-thread-plan"
SHA = "a" * 40
PACKAGE_DIGEST = "b" * 64


def test_cli_keeps_git_sha_separate_from_package_digest() -> None:
    arguments = parse_args(
        [
            "--rollout",
            "rollout.jsonl",
            "--discord-log",
            "discord.log",
            "--thread-id",
            THREAD,
            "--expected-sha",
            SHA,
            "--expected-package-digest-sha256",
            PACKAGE_DIGEST,
        ]
    )

    assert arguments.expected_sha == SHA
    assert arguments.expected_package_digest_sha256 == PACKAGE_DIGEST


def preflight_snapshot() -> PreflightSnapshot:
    return PreflightSnapshot(
        discord_thread_id=THREAD,
        codex_thread_id=CODEX_THREAD,
        session_id=SESSION,
        locator_session_id=SESSION,
        locator_transcript_matches=True,
        actual_package_digest_sha256=PACKAGE_DIGEST,
        locator_package_digest_sha256=PACKAGE_DIGEST,
        expected_package_digest_sha256=PACKAGE_DIGEST,
        permission_mode="dontAsk",
        cmw_authenticated=True,
        cmw_active=False,
        app_thread_id=CODEX_THREAD,
        goal_status=None,
    )


def test_preflight_accepts_inactive_cmw_and_absent_goal() -> None:
    # Given
    snapshot = preflight_snapshot()

    # When
    result = evaluate_preflight(snapshot)

    # Then
    assert result.ready is True
    assert result.cmw_inactive is True
    assert result.goal_absent_or_complete is True


def test_preflight_accepts_completed_goal() -> None:
    result = evaluate_preflight(replace(preflight_snapshot(), goal_status="complete"))

    assert result.ready is True
    assert result.goal_absent_or_complete is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"codex_thread_id": "wrong"}, "app_server_thread_mismatch"),
        ({"locator_session_id": "wrong"}, "locator_session_mismatch"),
        ({"locator_transcript_matches": False}, "locator_transcript_mismatch"),
        ({"actual_package_digest_sha256": "c" * 64}, "installed_package_digest_mismatch"),
        ({"locator_package_digest_sha256": "c" * 64}, "installed_package_digest_mismatch"),
        ({"expected_package_digest_sha256": "c" * 64}, "installed_package_digest_mismatch"),
        ({"permission_mode": "default"}, "approval_required"),
        ({"cmw_authenticated": False}, "cmw_status_unauthenticated"),
        ({"cmw_active": True}, "cmw_active"),
        ({"app_thread_id": "wrong"}, "app_server_thread_mismatch"),
        ({"goal_status": "active"}, "goal_not_absent_or_complete"),
        ({"goal_status": "blocked"}, "goal_not_absent_or_complete"),
    ],
)
def test_preflight_rejects_each_failed_machine_gate(
    change: dict[str, str | bool | None],
    reason: str,
) -> None:
    # Given
    snapshot = replace(preflight_snapshot(), **change)

    # When / Then
    with pytest.raises(PreflightError, match=reason):
        _ = evaluate_preflight(snapshot)


def test_locator_parser_uses_exact_session_hook_record(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    locator = {
        "codex_must_work_locator": {
            "session_id": SESSION,
            "transcript_path": str(rollout.resolve()),
            "plugin_root": str((tmp_path / "plugin").resolve()),
            "plugin_data": str((tmp_path / "data").resolve()),
            "control_capability": "x" * 43,
            "permission_mode": "bypassPermissions",
            "package_digest_sha256": PACKAGE_DIGEST,
        }
    }
    rows = [
        {"type": "session_meta", "payload": {"id": SESSION}},
        {
            "type": "event_msg",
            "payload": {
                "type": "hook_completed",
                "run": {
                    "event_name": "session_start",
                    "entries": [{"kind": "context", "text": json.dumps(locator)}],
                },
            },
        },
    ]
    _ = rollout.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    # When
    parsed = load_preflight_locator(rollout)

    # Then
    assert parsed.session_id == SESSION
    assert parsed.permission_mode == "bypassPermissions"
    assert parsed.transcript_path == rollout.resolve()


@pytest.mark.parametrize("content", ["", "{bad json}\n", '{"type":"session_meta","payload":{}}\n'])
def test_locator_parser_rejects_missing_or_corrupt_records(tmp_path: Path, content: str) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    _ = rollout.write_text(content, encoding="utf-8")

    # When / Then
    with pytest.raises(PreflightError):
        _ = load_preflight_locator(rollout)


def test_mapping_loader_is_read_only_and_requires_one_exact_row(tmp_path: Path) -> None:
    # Given
    database = tmp_path / "discord_mirror.sqlite"
    with sqlite3.connect(database) as connection:
        _ = connection.execute(
            "CREATE TABLE mirror_threads (codex_thread_id TEXT, discord_thread_id INTEGER)"
        )
        _ = connection.execute(
            "INSERT INTO mirror_threads VALUES (?, ?)",
            (CODEX_THREAD, int(THREAD)),
        )
    before = database.read_bytes()

    # When
    mapped = load_mapping(database, THREAD)

    # Then
    assert mapped == CODEX_THREAD
    assert database.read_bytes() == before


def test_installed_generation_is_verified_before_mcp_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locator = PreflightLocator(
        SESSION,
        tmp_path / "rollout.jsonl",
        tmp_path / "plugins" / "cache" / "wrong-marketplace" / "codex-must-work" / "1.2.3",
        tmp_path / "data",
        "dontAsk",
        PACKAGE_DIGEST,
        "x" * 43,
    )

    def reject(_home: Path, _root: Path) -> Never:
        pytest.fail("must reject malformed direct root first")

    require = monkeypatch.setattr(installed_generation, "require_session_generation", reject)

    with pytest.raises(PreflightError, match="installed_root_invalid"):
        _ = validate_installed_generation(locator, PACKAGE_DIGEST)

    assert require is None


def test_cmw_status_timeout_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _ = (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "codex-must-work": {
                        "command": "python",
                        "args": [],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    locator = PreflightLocator(
        SESSION,
        tmp_path / "rollout.jsonl",
        plugin,
        tmp_path / "data",
        "dontAsk",
        PACKAGE_DIGEST,
        "x" * 43,
    )

    def timeout(*_args: str, **_kwargs: str) -> subprocess.CompletedProcess[str]:
        command = "redacted"
        raise subprocess.TimeoutExpired(command, 12)

    monkeypatch.setattr(subprocess, "run", timeout)

    # When / Then
    with pytest.raises(PreflightError, match="cmw_status_failed") as raised:
        _ = read_cmw_status(locator)
    assert "x" * 43 not in str(raised.value)


def test_app_server_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class FailingAppServer:
        def __enter__(self) -> Self:
            message = "private transport detail"
            raise AppServerError(message)

        def __exit__(
            self,
            _error_type: type[BaseException] | None,
            _error: BaseException | None,
            _traceback: TracebackType | None,
        ) -> None:
            return None

    monkeypatch.setattr(runtime, "ResidentAppServer", FailingAppServer)

    # When / Then
    with pytest.raises(PreflightError, match="app_server_read_failed") as raised:
        _ = read_app_server(CODEX_THREAD)
    assert "private transport detail" not in str(raised.value)
