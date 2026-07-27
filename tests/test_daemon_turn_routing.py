from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING

from scripts.app_server_protocol import AppServerActivity, AppServerActivityKind, JsonObject
from scripts.daemon_task import DaemonTask
from scripts.state import StateDocument, load_state, runtime_path, save_state
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    SECOND_SESSION,
    FakeAppServer,
    create_service,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_threadless_known_turn_routes_only_its_reverse_index_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, first = session_files(tmp_path, FIRST_SESSION)
    _, second = session_files(tmp_path, SECOND_SESSION)
    clients: list[FakeAppServer] = []
    observed: list[str] = []
    routed = Event()

    def record_activity(task: DaemonTask, _now: float) -> None:
        observed.append(task.session_id)
        routed.set()

    monkeypatch.setattr(DaemonTask, "record_activity", record_activity)
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, first))
        _ = service.start(start_request(SECOND_SESSION, second))
        _set_pending_turn(root, FIRST_SESSION, "turn-known")
        service.app_server_activity(
            AppServerActivity(AppServerActivityKind.TURN_PROGRESS, turn_id="turn-known")
        )
        assert routed.wait(1.0)
        assert observed == [FIRST_SESSION]
    finally:
        service.close()


def test_identity_free_activity_is_ignored_without_broadcast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, first = session_files(tmp_path, FIRST_SESSION)
    _, second = session_files(tmp_path, SECOND_SESSION)
    clients: list[FakeAppServer] = []
    observed: list[str] = []
    processed = Event()

    def record_activity(task: DaemonTask, _now: float) -> None:
        observed.append(task.session_id)
        processed.set()

    monkeypatch.setattr(DaemonTask, "record_activity", record_activity)
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, first))
        _ = service.start(start_request(SECOND_SESSION, second))
        service.app_server_activity(AppServerActivity(AppServerActivityKind.TURN_PROGRESS))
        service.app_server_activity(
            AppServerActivity(
                AppServerActivityKind.TURN_PROGRESS,
                thread_id=FIRST_SESSION,
                turn_id="marker-turn",
            )
        )
        assert processed.wait(1.0)
        assert observed == [FIRST_SESSION]
    finally:
        service.close()


def test_threadless_matching_start_promotes_pending_turn_to_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    promoted = Event()

    def record_activity(_task: DaemonTask, _now: float) -> None:
        promoted.set()

    monkeypatch.setattr(DaemonTask, "record_activity", record_activity)
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        _set_pending_turn(root, FIRST_SESSION, "turn-known")
        service.app_server_activity(
            AppServerActivity(AppServerActivityKind.TURN_STARTED, turn_id="turn-known")
        )
        assert promoted.wait(1.0)
        runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
        assert runtime["pending_turn_id"] is None
        assert runtime["managed_turn_id"] == "turn-known"
    finally:
        service.close()


def test_wrong_turn_start_is_exactly_interrupted_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    clients: list[FakeAppServer] = []
    service = create_service(root, clients)
    try:
        _ = service.start(start_request(FIRST_SESSION, transcript))
        _set_pending_turn(root, FIRST_SESSION, "turn-expected")
        client = clients[0]
        original_request = client.request
        interrupts: list[JsonObject] = []
        interrupted = Event()

        def capture_request(
            method: str,
            params: JsonObject,
            *,
            timeout_seconds: float = 10.0,
        ) -> JsonObject:
            if method == "turn/interrupt":
                interrupts.append(params)
                interrupted.set()
            return original_request(method, params, timeout_seconds=timeout_seconds)

        monkeypatch.setattr(client, "request", capture_request)
        activity = AppServerActivity(
            AppServerActivityKind.TURN_STARTED,
            thread_id=FIRST_SESSION,
            turn_id="turn-wrong",
        )
        service.app_server_activity(activity)
        assert interrupted.wait(1.0)
        interrupted.clear()
        service.app_server_activity(activity)
        assert not interrupted.wait(0.25)
        assert interrupts == [{"threadId": FIRST_SESSION, "turnId": "turn-wrong"}]
    finally:
        service.close()


def _set_pending_turn(root: Path, session_id: str, turn_id: str) -> None:
    path = runtime_path(root, session_id)
    values = dict(load_state(root, path).values)
    values["handoff_requested"] = True
    values["managed_turn_id"] = None
    values["pending_turn_id"] = turn_id
    values["pending_turn_timed_out_at"] = None
    save_state(root, path, StateDocument(values=values))
