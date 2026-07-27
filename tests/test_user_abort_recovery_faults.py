from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from scripts.daemon_activation_fence import DaemonActivationFences
from scripts.daemon_recovery import discover_persisted_tasks, recover_activation_fence
from scripts.setup import enable_session
from scripts.state import load_state, runtime_path
from tests.daemon_service_fixture import (
    FIRST_SESSION,
    activation_request,
    append_turn_event,
    bind_pending_activation,
    capabilities,
    session_files,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.state_io import JsonValue


def test_recovery_turn_abort_opens_one_persisted_handoff(tmp_path: Path) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    _ = enable_session(
        root,
        activation_request(FIRST_SESSION, transcript),
        capabilities(),
    )
    bind_pending_activation(root, FIRST_SESSION, transcript)
    append_turn_event(transcript, "turn_aborted")
    saved = discover_persisted_tasks(root)
    assert len(saved) == 1
    fences = DaemonActivationFences(root)

    recovered = recover_activation_fence(root, fences, saved[0])

    runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
    assert recovered is True
    assert runtime["enabled"] is True
    assert runtime["handoff_requested"] is True
    assert runtime["managed_turn_id"] is None


def test_adoption_persistence_failure_never_interrupts_user_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = session_files(tmp_path, FIRST_SESSION)
    _ = enable_session(
        root,
        activation_request(FIRST_SESSION, transcript),
        capabilities(),
    )
    bind_pending_activation(root, FIRST_SESSION, transcript)
    append_turn_event(transcript, "turn_aborted")
    saved = discover_persisted_tasks(root)
    fences = DaemonActivationFences(root)
    assert recover_activation_fence(root, fences, saved[0]) is True

    def fail_persistence(
        _root: Path,
        _path: Path,
        _mutator: Callable[[dict[str, JsonValue]], JsonValue],
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "scripts.daemon_activation_fence.mutate_existing_state",
        fail_persistence,
    )

    with pytest.raises(KeyboardInterrupt):
        _ = fences.adopt_started(
            FIRST_SESSION,
            transcript,
            "user-turn",
            datetime.now(UTC),
        )

    runtime = load_state(root, runtime_path(root, FIRST_SESSION)).values
    assert runtime["handoff_requested"] is True
    assert runtime["managed_turn_id"] is None
