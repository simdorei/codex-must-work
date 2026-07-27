from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING

from scripts.app_server_protocol import (
    AppServerActivity,
    AppServerActivityKind,
    JsonObject,
    TurnOutcome,
)
from scripts.daemon_activation_fence import DaemonActivationFences
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.manager_outcome import resolve_turn_outcome
from scripts.manager_runtime import load_manager_runtime, record_turn_started
from scripts.state import StateDocument, load_state, runtime_path, save_state
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    append_turn_event,
    create_service,
    session_files,
    start_request,
)
from tests.daemon_service_fixture import (
    FakeAppServer as DaemonFakeAppServer,
)
from tests.manager_fixture import FakeAppServer, manager_runtime_fixture

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    import pytest


def test_aborted_activation_turn_creates_one_durable_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: CMW was explicitly activated from one exact live user turn.
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[DaemonFakeAppServer] = []
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        client = clients[0]
        original_request = client.request
        starts: list[JsonObject] = []
        owned = Event()

        def capture_request(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            if method == "turn/start":
                starts.append(dict(params))
            return original_request(method, params, timeout_seconds=timeout_seconds)

        monkeypatch.setattr(client, "request", capture_request)

        def capture_owned(root_path: Path, runtime_file: Path, turn_id: str) -> None:
            record_turn_started(root_path, runtime_file, turn_id)
            owned.set()

        monkeypatch.setattr("scripts.manager_engine.record_turn_started", capture_owned)
        append_turn_event(transcript, "turn_aborted")

        # When: the daemon observes the exact activation turn's terminal abort.
        aborted = AppServerActivity(
            AppServerActivityKind.TURN_COMPLETED,
            FIRST_SESSION,
            "activation-turn",
            TurnOutcome.INTERRUPTED,
        )
        service.app_server_activity(aborted)
        assert owned.wait(1.0)
        service.app_server_activity(aborted)

        # Then: the persistent task stays enabled and starts exactly one handoff.
        runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert runtime["enabled"] is True
        assert runtime["manager_error"] is None
        assert runtime["managed_turn_id"] == "turn-1"
        assert len(starts) == 1
    finally:
        service.close()


def test_unclaimed_interrupted_owned_turn_restarts_once(tmp_path: Path) -> None:
    # Given: an explicitly persistent goal-less task owns one managed turn.
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
    client.completed.add("turn-1")
    client.turn_outcomes["turn-1"] = TurnOutcome.INTERRUPTED
    client.active = None

    # When: the manager observes the exact external interruption and ticks again.
    assert engine.tick() is True
    interrupted = load_state(root, path).values
    assert interrupted["handoff_requested"] is True
    assert interrupted["managed_turn_id"] is None
    assert engine.tick() is True

    # Then: one replacement is owned without disabling or duplicating the task.
    restarted = load_state(root, path).values
    assert restarted["enabled"] is True
    assert restarted["managed_turn_id"] == "turn-2"
    assert client.turn_number == 2


def test_new_user_turn_during_queued_handoff_waits_for_exact_terminal(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: activation completed, but a new exact user turn became active first.
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[DaemonFakeAppServer] = []
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        client = clients[0]
        client.active[FIRST_SESSION] = "user-turn"
        activation_completed = Event()
        adopted = Event()
        original_complete = DaemonActivationFences.complete
        original_adopt = DaemonActivationFences.adopt_started

        def capture_complete(fences: DaemonActivationFences, session_id: str) -> None:
            original_complete(fences, session_id)
            activation_completed.set()

        def capture_adopt(
            fences: DaemonActivationFences,
            session_id: str,
            rollout: Path,
            turn_id: str,
            now: datetime,
        ) -> bool:
            result = original_adopt(fences, session_id, rollout, turn_id, now)
            if result:
                adopted.set()
            return result

        monkeypatch.setattr(DaemonActivationFences, "complete", capture_complete)
        monkeypatch.setattr(DaemonActivationFences, "adopt_started", capture_adopt)
        append_turn_event(transcript, "task_complete")
        service.app_server_activity(
            AppServerActivity(
                AppServerActivityKind.TURN_COMPLETED,
                FIRST_SESSION,
                "activation-turn",
                TurnOutcome.COMPLETED,
            )
        )
        assert activation_completed.wait(1.0)
        activation_epoch = load_state(root, runtime_path(root, FIRST_SESSION)).values[
            "turn_activity_epoch"
        ]
        assert type(activation_epoch) is int
        original_request = client.request
        interrupts: list[JsonObject] = []
        starts: list[JsonObject] = []
        owned = Event()

        def capture_request(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            if method == "turn/interrupt":
                interrupts.append(dict(params))
            if method == "turn/start":
                starts.append(dict(params))
            return original_request(method, params, timeout_seconds=timeout_seconds)

        monkeypatch.setattr(client, "request", capture_request)

        def capture_owned(root_path: Path, runtime_file: Path, turn_id: str) -> None:
            record_turn_started(root_path, runtime_file, turn_id)
            owned.set()

        monkeypatch.setattr("scripts.manager_engine.record_turn_started", capture_owned)
        started = AppServerActivity(
            AppServerActivityKind.TURN_STARTED,
            FIRST_SESSION,
            "user-turn",
        )

        # When: the exact user turn starts while CMW's handoff is queued.
        service.app_server_activity(started)
        assert adopted.wait(1.0)
        service.app_server_activity(started)

        # Then: CMW waits and never interrupts or starts over the live user turn.
        assert interrupts == []
        assert starts == []
        assert client.active[FIRST_SESSION] == "user-turn"
        waiting = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert waiting["parent_turn_id"] == "user-turn"
        assert waiting["turn_activity_epoch"] == activation_epoch + 1
        assert waiting["handoff_requested"] is False

        # When: that same exact user turn reaches a terminal outcome.
        append_turn_event(transcript, "task_complete", "user-turn")
        client.outcomes["user-turn"] = TurnOutcome.COMPLETED
        _ = client.active.pop(FIRST_SESSION)
        service.app_server_activity(
            AppServerActivity(
                AppServerActivityKind.TURN_COMPLETED,
                FIRST_SESSION,
                "user-turn",
                TurnOutcome.COMPLETED,
            )
        )
        assert owned.wait(1.0)

        # Then: exactly one managed handoff starts after the terminal evidence.
        runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert interrupts == []
        assert len(starts) == 1
        assert runtime["managed_turn_id"] == "turn-1"
    finally:
        service.close()


def test_shutdown_wins_over_unclaimed_interrupted_owned_turn(tmp_path: Path) -> None:
    # Given: work-off has already requested terminal shutdown of the exact owned turn.
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
    document = load_state(root, path)
    values = dict(document.values)
    values["shutdown_requested"] = True
    save_state(root, path, StateDocument(values=values))
    client.completed.add("turn-1")
    client.turn_outcomes["turn-1"] = TurnOutcome.INTERRUPTED
    client.active = None

    # When: the external interruption reaches the manager after work-off.
    keep_running = engine.tick()

    # Then: shutdown remains terminal and no replacement turn can start.
    assert keep_running is False
    assert not path.exists()
    assert client.turn_number == 1


def test_goal_companion_external_interrupt_still_fails_closed(tmp_path: Path) -> None:
    # Given: a Goal companion runtime owns an exact turn without an interrupt claim.
    root, path = manager_runtime_fixture(tmp_path)
    document = load_state(root, path)
    values = dict(document.values)
    values["goal_companion"] = True
    values["handoff_requested"] = False
    values["managed_turn_id"] = "goal-turn"
    save_state(root, path, StateDocument(values=values))
    runtime = load_manager_runtime(root, path.name)
    assert runtime is not None

    # When: the Goal-owned turn is interrupted externally.
    resolution = resolve_turn_outcome(
        root,
        runtime,
        None,
        "goal-turn",
        TurnOutcome.INTERRUPTED,
    )

    # Then: Goal behavior remains fail-closed with no durable replacement handoff.
    persisted = load_state(root, path).values
    assert resolution.keep_running is False
    assert resolution.failure_reason == "turn_interrupted_external"
    assert persisted["handoff_requested"] is False
    assert persisted["managed_turn_id"] == "goal-turn"
