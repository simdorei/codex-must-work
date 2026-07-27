from __future__ import annotations

import threading
from threading import Event
from typing import TYPE_CHECKING

from scripts import setup
from scripts.daemon_models import DaemonServiceError, SessionId, SessionRequest
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    create_service,
    session_files,
    start_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    import pytest

    from scripts.daemon_models import ToolResult
    from scripts.daemon_service import DaemonService


def test_stop_mutation_finishes_before_close_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service, request = _started_service(tmp_path)
    mutation_entered = threading.Barrier(2)
    allow_mutation = threading.Barrier(2)
    original = setup.request_session_shutdown

    def blocked_shutdown(
        root: Path,
        session_id: str,
        *,
        interrupt_active: bool,
    ) -> None:
        _ = mutation_entered.wait()
        _ = allow_mutation.wait()
        original(root, session_id, interrupt_active=interrupt_active)

    monkeypatch.setattr(setup, "request_session_shutdown", blocked_shutdown)

    # When / Then
    _assert_mutation_precedes_close(
        service,
        request,
        service.stop,
        mutation_entered,
        allow_mutation,
    )


def test_complete_mutation_finishes_before_close_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service, request = _started_service(tmp_path)
    mutation_entered = threading.Barrier(2)
    allow_mutation = threading.Barrier(2)
    original = setup.request_verified_completion

    def blocked_completion(root: Path, session_id: str, now: datetime) -> bool:
        _ = mutation_entered.wait()
        _ = allow_mutation.wait()
        return original(root, session_id, now)

    monkeypatch.setattr(
        setup,
        "request_verified_completion",
        blocked_completion,
    )

    # When / Then
    _assert_mutation_precedes_close(
        service,
        request,
        service.complete,
        mutation_entered,
        allow_mutation,
    )


def _started_service(tmp_path: Path) -> tuple[DaemonService, SessionRequest]:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    service = create_service(root, [])
    _ = service.start(start_request(FIRST_SESSION, transcript))
    return service, SessionRequest(SessionId(FIRST_SESSION))


def _assert_mutation_precedes_close(
    service: DaemonService,
    request: SessionRequest,
    mutation: Callable[[SessionRequest], ToolResult],
    mutation_entered: threading.Barrier,
    allow_mutation: threading.Barrier,
) -> None:
    mutation_errors: list[Exception] = []
    mutation_done = Event()
    close_done = Event()

    def mutate() -> None:
        try:
            _ = mutation(request)
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            mutation_errors.append(error)
        finally:
            mutation_done.set()

    def close() -> None:
        service.close()
        close_done.set()

    mutation_thread = threading.Thread(
        target=mutate,
        name="test-control-mutation",
        daemon=True,
    )
    close_thread = threading.Thread(target=close, daemon=True)
    mutation_thread.start()
    _ = mutation_entered.wait()

    close_thread.start()
    close_returned_before_mutation = close_done.wait(0.25)
    _ = allow_mutation.wait()

    assert mutation_done.wait(2.0)
    assert close_done.wait(2.0)
    mutation_thread.join(timeout=0.25)
    close_thread.join(timeout=0.25)
    observed_reason = (
        mutation_errors[0].reason_code
        if mutation_errors and isinstance(mutation_errors[0], DaemonServiceError)
        else type(mutation_errors[0]).__name__
        if mutation_errors
        else None
    )
    assert (close_returned_before_mutation, observed_reason) == (False, "daemon_closed")
