from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from typing import TypedDict, Unpack, final

import pytest

from scripts.app_server_activity import (
    INITIAL_ACTIVITY_SEQUENCE,
    ActivitySequence,
    AppServerActivity,
    AppServerActivityKind,
    AppServerActivityStream,
)
from scripts.app_server_client import ResidentAppServer
from scripts.app_server_protocol import JsonObject, TurnOutcome


class _PopenOptions(TypedDict):
    stdin: int
    stdout: int
    stderr: int
    text: bool
    encoding: str
    errors: str
    bufsize: int
    creationflags: int


class _RequestOptions(TypedDict):
    timeout_seconds: float


class _WaitOptions(TypedDict):
    timeout: float


def test_progress_observation_excludes_notification_content() -> None:
    # Given
    stream = AppServerActivityStream()
    message: JsonObject = {
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "delta": "private model output",
            "item": {"arguments": "private tool input"},
        },
    }

    # When
    stream.record(message)

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity == AppServerActivity(
        AppServerActivityKind.TURN_PROGRESS,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert "private" not in repr(observation)


def test_threadless_known_turn_is_classified_with_correlated_thread() -> None:
    # Given
    stream = AppServerActivityStream()
    stream.correlate_turn("thread-1", "turn-1")

    # When
    stream.record(
        {
            "method": "turn/started",
            "params": {"turn": {"id": "turn-1", "status": "inProgress"}},
        }
    )

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity == AppServerActivity(
        AppServerActivityKind.TURN_STARTED,
        thread_id="thread-1",
        turn_id="turn-1",
    )


def test_response_does_not_emit_progress() -> None:
    # Given
    stream = AppServerActivityStream()

    # When
    stream.record({"id": "request-1", "result": {"private": "value"}})

    # Then
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None


def test_unrelated_notification_does_not_emit_progress() -> None:
    # Given
    stream = AppServerActivityStream()

    # When
    stream.record({"method": "account/updated", "params": {"private": "value"}})

    # Then
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None


def test_server_request_emits_only_ownership_metadata() -> None:
    # Given
    stream = AppServerActivityStream()
    message: JsonObject = {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "command": "private command",
        },
    }

    # When
    stream.record(message)

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity == AppServerActivity(
        AppServerActivityKind.SERVER_REQUEST,
        thread_id="thread-1",
        turn_id="turn-1",
    )


def test_stdout_reader_updates_exact_state_before_activity_wake() -> None:
    # Given
    stream = AppServerActivityStream()
    generation = stream.reset()
    stdout = StringIO(
        '{"method":"turn/started","params":' + '{"threadId":"thread-1","turnId":"turn-1"}}\n'
    )

    # When
    stream.read_stdout(stdout, generation)

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity.kind is AppServerActivityKind.TURN_STARTED
    assert stream.active_turn("thread-1") == "turn-1"


def test_completion_activity_preserves_exact_outcome() -> None:
    # Given
    stream = AppServerActivityStream()
    message: JsonObject = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "interrupted"},
        },
    }

    # When
    stream.record(message)

    # Then
    observation = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert observation is not None
    assert observation.activity.outcome is TurnOutcome.INTERRUPTED


def test_connection_close_is_emitted_once() -> None:
    # Given
    stream = AppServerActivityStream()

    # When
    stream.mark_closed("resident_app_server_closed")
    first = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    stream.mark_closed("resident_app_server_exited")

    # Then
    assert first is not None
    assert first.activity.kind is AppServerActivityKind.CONNECTION_CLOSED
    assert stream.wait_activity(first.sequence, 0.0) is None
    assert stream.closed_error == "resident_app_server_closed"


def test_activity_sequence_survives_connection_reset() -> None:
    # Given
    stream = AppServerActivityStream()
    stream.record(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
    )
    first = stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0)
    assert first is not None

    # When
    _ = stream.reset()
    stream.record(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-2", "turnId": "turn-2"},
        }
    )

    # Then
    second = stream.wait_activity(first.sequence, 0.0)
    assert second is not None
    assert second.sequence == ActivitySequence(first.sequence + 1)
    assert stream.active_turn("thread-1") is None
    assert stream.active_turn("thread-2") == "turn-2"


def test_stale_generation_cannot_close_current_connection() -> None:
    # Given
    stream = AppServerActivityStream()
    stale_generation = stream.reset()
    _ = stream.reset()

    # When
    stream.mark_closed("stale_connection_exited", stale_generation)

    # Then
    assert stream.closed_error is None
    assert stream.wait_activity(INITIAL_ACTIVITY_SEQUENCE, 0.0) is None


def test_listener_runs_after_state_update_and_outside_activity_lock() -> None:
    # Given
    observed: list[tuple[AppServerActivity, str | None]] = []
    stream: AppServerActivityStream

    def read_active_turn() -> str | None:
        with stream.condition:
            return stream.active_turn("thread-1")

    def listener(activity: AppServerActivity) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            active_turn = executor.submit(read_active_turn).result(timeout=1.0)
        observed.append((activity, active_turn))

    stream = AppServerActivityStream(listener)

    # When
    stream.record(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
    )

    # Then
    assert observed == [
        (
            AppServerActivity(
                AppServerActivityKind.TURN_STARTED,
                thread_id="thread-1",
                turn_id="turn-1",
            ),
            "turn-1",
        )
    ]


def test_listener_exception_is_not_swallowed() -> None:
    # Given
    def reject(_activity: AppServerActivity) -> None:
        message = "listener_failed"
        raise RuntimeError(message)

    stream = AppServerActivityStream(reject)

    # When / Then
    with pytest.raises(RuntimeError, match="listener_failed"):
        stream.record(
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            }
        )


def test_shared_client_starts_only_one_process_when_called_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    client = ResidentAppServer()
    created: list[_FakeProcess] = []
    barrier = threading.Barrier(3)

    def create_process(
        _args: list[str],
        **_options: Unpack[_PopenOptions],
    ) -> _FakeProcess:
        process = _FakeProcess()
        created.append(process)
        return process

    def initialize(
        _client: ResidentAppServer,
        _method: str,
        _params: JsonObject,
        **_options: Unpack[_RequestOptions],
    ) -> JsonObject:
        return {}

    def resolve_executable(_expected_sha256: str | None) -> Path:
        return Path("codex")

    def start_after_barrier() -> None:
        _ = barrier.wait()
        client.start()

    monkeypatch.setattr("scripts.app_server_client.resolve_codex_executable", resolve_executable)
    monkeypatch.setattr("scripts.app_server_client.subprocess.Popen", create_process)
    monkeypatch.setattr(ResidentAppServer, "_request_started", initialize)

    # When
    with ThreadPoolExecutor(max_workers=2) as executor:
        starts = [executor.submit(start_after_barrier) for _ in range(2)]
        _ = barrier.wait()
        for started in starts:
            started.result()

    # Then
    assert len(created) == 1
    client.close()


@final
class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = StringIO()
        self.stdout = StringIO()
        self.stderr = StringIO()
        self._return_code: int | None = None

    def poll(self) -> int | None:
        return self._return_code

    def terminate(self) -> None:
        self._return_code = 0

    def kill(self) -> None:
        self._return_code = 1

    def wait(self, **_options: Unpack[_WaitOptions]) -> int:
        return 0 if self._return_code is None else self._return_code
