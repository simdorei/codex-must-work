from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.app_server_protocol import TurnOutcome
from scripts.diagnostics import DiagnosticCode
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.setup import enable_session, request_verified_completion
from scripts.setup_cli import main
from scripts.state import StateDocument, load_state, runtime_path, save_state
from tests.manager_fixture import FakeAppServer, manager_runtime_fixture
from tests.rollout_fixture import SESSION_ID
from tests.test_setup import managed_report, request
from tests.watcher_fixture import diagnostic_codes

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_completed_cli_defers_managed_heartbeat_until_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a Goal-less managed turn is still active before its Final answer.
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _ = enable_session(root, request(root, observe_only=False), managed_report())
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values.update({"manager_ready": True, "managed_turn_id": "turn-owned"})
    save_state(root, path, StateDocument(values=values))

    # When: verified completion is requested immediately before Final.
    exit_code = main(["disable", "--session-id", SESSION_ID, "--completed"])

    # Then: only the request is persisted; no COMPLETE heartbeat exists yet.
    runtime = load_state(root, path).values
    assert exit_code == 0
    assert runtime["shutdown_requested"] is True
    assert runtime["shutdown_interrupt"] is False
    assert not (root / "logs" / "diagnostic.jsonl").exists()
    assert capsys.readouterr().out == (
        "Codex Must Work completion requested; waiting for final turn.\n"
    )


def test_requested_completion_commits_after_final_turn_completes(tmp_path: Path) -> None:
    # Given: work-off requested completion while its Goal-less turn remained active.
    root, path, client, engine = _started_manager(tmp_path)
    _request_completion(root, path)
    assert not (root / "logs" / "diagnostic.jsonl").exists()
    client.completed.add("turn-1")
    client.turn_outcomes["turn-1"] = TurnOutcome.COMPLETED
    client.active = None

    # When: the manager observes the terminal outcome after Final.
    keep_running = engine.tick()

    # Then: COMPLETE is recorded exactly once and the managed runtime is removed.
    assert keep_running is False
    assert not path.exists()
    assert diagnostic_codes(root).count(DiagnosticCode.WATCHER_COMPLETED.value) == 1


def test_requested_completion_does_not_commit_when_final_turn_fails(tmp_path: Path) -> None:
    # Given: work-off requested completion but the Goal-less turn later fails.
    root, path, client, engine = _started_manager(tmp_path)
    _request_completion(root, path)
    client.completed.add("turn-1")
    client.turn_outcomes["turn-1"] = TurnOutcome.FAILED
    client.active = None

    # When: the manager observes the failed terminal outcome.
    keep_running = engine.tick()

    # Then: it preserves the failure and never records COMPLETE.
    runtime = load_state(root, path).values
    assert keep_running is False
    assert runtime["manager_error"] == "turn_failed"
    assert DiagnosticCode.WATCHER_COMPLETED.value not in diagnostic_codes(root)


def test_requested_completion_does_not_commit_when_final_turn_is_interrupted(
    tmp_path: Path,
) -> None:
    # Given: work-off requested completion but the Goal-less turn is externally interrupted.
    root, path, client, engine = _started_manager(tmp_path)
    _request_completion(root, path)
    client.completed.add("turn-1")
    client.turn_outcomes["turn-1"] = TurnOutcome.INTERRUPTED
    client.active = None

    # When: the manager observes the interrupted terminal outcome.
    keep_running = engine.tick()

    # Then: it removes the interrupted runtime without ever recording COMPLETE.
    assert keep_running is False
    assert not path.exists()
    assert not (root / "logs" / "diagnostic.jsonl").exists()


def _started_manager(tmp_path: Path) -> tuple[Path, Path, FakeAppServer, ManagerEngine]:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    engine.initialize()
    assert engine.tick() is True
    return root, path, client, engine


def _request_completion(root: Path, path: Path) -> None:
    deferred = request_verified_completion(
        root,
        "thread-1",
        datetime(2026, 7, 20, tzinfo=UTC),
    )
    runtime = load_state(root, path).values
    assert deferred is True
    assert runtime["shutdown_requested"] is True
    assert runtime["shutdown_interrupt"] is False
