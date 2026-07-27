from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier, Event
from typing import TYPE_CHECKING, Final, final

from scripts.app_server_protocol import (
    AppServerActivity,
    AppServerActivityKind,
    AppServerProtocolError,
    JsonObject,
    TurnOutcome,
)
from scripts.control import CapabilityReport
from scripts.daemon_activation_fence import DaemonActivationFences
from scripts.daemon_models import SessionId, StartRequest
from scripts.daemon_service import DaemonService
from scripts.durations import Milliseconds
from scripts.setup import ActivationRequest, MessagePreset, Settings
from scripts.state import runtime_path
from tests.rollout_fixture import write_session_meta

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

FIRST_SESSION: Final = "11111111-1111-4111-8111-111111111111"
SECOND_SESSION: Final = "22222222-2222-4222-8222-222222222222"


@dataclass(frozen=True, slots=True)
class CloseSynchronization:
    close_started: Barrier
    response_released: Barrier
    response_delivered: Event


@dataclass(frozen=True, slots=True)
class StartSynchronization:
    factory_entered: Event
    allow_factory_return: Event


@final
class FakeAppServer:
    def __init__(
        self,
        listener: Callable[[AppServerActivity], None],
        *,
        fail_resume: bool = False,
        close_synchronization: CloseSynchronization | None = None,
    ) -> None:
        self.listener = listener
        self.fail_resume = fail_resume
        self.close_synchronization = close_synchronization
        self.closed = False
        self.close_count = 0
        self.closed_event = Event()
        self.pending_server_request: str | None = None
        self.turn_start_called = Event()
        self.allow_turn_start = Event()
        self.allow_turn_start.set()
        self.active: dict[str, str] = {}
        self.outcomes: dict[str, TurnOutcome] = {}

    def start(self) -> None:
        return

    def close(self) -> None:
        self.close_count += 1
        synchronization = self.close_synchronization
        if synchronization is not None:
            reader = threading.Thread(
                target=self._deliver_response_during_close,
                name="fake-app-server-stdout-reader",
                daemon=True,
            )
            reader.start()
            _ = synchronization.close_started.wait()
            _ = synchronization.response_released.wait()
            if not synchronization.response_delivered.wait(0.25):
                message = "stdout_reader_blocked_during_close"
                raise AppServerProtocolError(message)
            reader.join(timeout=0.25)
        self.closed = True
        self.closed_event.set()

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = 10.0,
    ) -> JsonObject:
        _ = timeout_seconds
        if method == "thread/resume" and self.fail_resume:
            message = "resume_failed"
            raise AppServerProtocolError(message)
        if method == "turn/start":
            self.turn_start_called.set()
            assert self.allow_turn_start.wait(1.0)
            thread_id = _string(params, "threadId")
            self.active[thread_id] = "turn-1"
            return {"turn": {"id": "turn-1"}}
        return {}

    def active_turn(self, thread_id: str) -> str | None:
        return self.active.get(thread_id)

    def turn_completed(self, turn_id: str) -> bool:
        return turn_id in self.outcomes

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        return self.outcomes.get(turn_id)

    def latest_started_turn(self, thread_id: str) -> str | None:
        return self.active.get(thread_id)

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        _ = thread_id, turn_id, timeout_seconds
        return True

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        _ = timeout_seconds
        return turn_id in self.outcomes

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        _ = previous_turn_id, timeout_seconds
        return self.active.get(thread_id)

    def emit_activation_complete(self, session_id: str) -> None:
        self.listener(
            AppServerActivity(
                AppServerActivityKind.TURN_COMPLETED,
                session_id,
                "activation-turn",
                TurnOutcome.COMPLETED,
            )
        )

    def _deliver_response_during_close(self) -> None:
        synchronization = self.close_synchronization
        assert synchronization is not None
        _ = synchronization.close_started.wait()
        _ = synchronization.response_released.wait()
        self.emit_activation_complete(FIRST_SESSION)
        synchronization.response_delivered.set()


def create_service(
    root: Path,
    clients: list[FakeAppServer],
    *,
    fail_resume: bool = False,
    close_synchronization: CloseSynchronization | None = None,
    start_synchronization: StartSynchronization | None = None,
) -> DaemonService:
    def factory(
        _fingerprint: str,
        listener: Callable[[AppServerActivity], None],
    ) -> FakeAppServer:
        if start_synchronization is not None:
            start_synchronization.factory_entered.set()
            assert start_synchronization.allow_factory_return.wait(2.0)
        client = FakeAppServer(
            listener,
            fail_resume=fail_resume,
            close_synchronization=close_synchronization,
        )
        clients.append(client)
        return client

    return DaemonService(
        root=root,
        client_factory=factory,
        fingerprint_provider=lambda: "digest",
    )


def session_files(tmp_path: Path, session_id: str) -> tuple[Path, Path]:
    root = tmp_path / "codex-must-work"
    transcript = tmp_path / "sessions" / f"{session_id}.jsonl"
    write_session_meta(transcript, session_id)
    with transcript.open("a", encoding="utf-8", newline="") as handle:
        _ = handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-22T00:00:01.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "activation-turn"},
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    return root, transcript


def bind_pending_activation(root: Path, session_id: str, transcript: Path) -> None:
    fences = DaemonActivationFences(root)
    pending = fences.capture(session_id, transcript)
    fences.bind(pending, runtime_path(root, session_id), datetime.now(UTC))
    fences.clear()


def append_turn_event(transcript: Path, kind: str, turn_id: str = "activation-turn") -> None:
    with transcript.open("a", encoding="utf-8", newline="") as handle:
        _ = handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-22T00:00:02.000Z",
                    "type": "event_msg",
                    "payload": {"type": kind, "turn_id": turn_id},
                },
                separators=(",", ":"),
            )
            + "\n"
        )


def start_request(session_id: str, transcript: Path) -> StartRequest:
    return StartRequest(
        session_id=SessionId(session_id),
        transcript_path=transcript,
        warning_after_ms=Milliseconds(60_000),
        restart_after_ms=Milliseconds(120_000),
        message_preset=MessagePreset.CLEANUP,
        auto_restart=True,
        goal_companion=False,
        observe_only=False,
        permission_mode="bypassPermissions",
    )


def activation_request(session_id: str, transcript: Path) -> ActivationRequest:
    return ActivationRequest(
        session_id=session_id,
        transcript_path=transcript,
        settings=Settings(
            warning_after_ms=Milliseconds(60_000),
            restart_after_ms=Milliseconds(120_000),
            message_preset=MessagePreset.CLEANUP,
            auto_restart_requested_by_user=True,
        ),
        observe_only=False,
        permission_mode="bypassPermissions",
        now=datetime.now(UTC),
    )


def capabilities() -> CapabilityReport:
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=True,
        reason_code="ready",
        evidence_fingerprint="digest",
        stop_continuation_ready=False,
    )


def _string(values: JsonObject, key: str) -> str:
    value = values.get(key)
    assert isinstance(value, str)
    return value
