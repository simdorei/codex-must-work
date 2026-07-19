import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from scripts.control import CapabilityReport
from scripts.durations import Milliseconds
from scripts.setup import (
    ActivationError,
    ActivationRequest,
    MessagePreset,
    Settings,
    complete_session,
    disable_session,
    enable_session,
)
from scripts.state import (
    StateDocument,
    config_path,
    cursor_path,
    load_state,
    runtime_path,
    save_state,
)
from tests.rollout_fixture import SESSION_ID, write_session_meta


def settings() -> Settings:
    return Settings(
        warning_after_ms=Milliseconds(90_000),
        restart_after_ms=Milliseconds(300_000),
        message_preset=MessagePreset.CLEANUP,
        auto_restart_requested_by_user=True,
    )


def request(root: Path, *, observe_only: bool) -> ActivationRequest:
    transcript = root.parent / "sessions" / "rollout.jsonl"
    write_session_meta(transcript)
    return ActivationRequest(
        session_id=SESSION_ID,
        transcript_path=transcript,
        settings=settings(),
        observe_only=observe_only,
        permission_mode="bypassPermissions",
        now=datetime(2026, 7, 17, tzinfo=UTC),
    )


def unavailable_report() -> CapabilityReport:
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=False,
        reason_code="same_live_server_attach_unavailable",
        evidence_fingerprint="fingerprint-a",
    )


def ready_report() -> CapabilityReport:
    return CapabilityReport(
        warning_delivery_ready=True,
        auto_restart_ready=False,
        reason_code="auto_restart_controls_unavailable",
        evidence_fingerprint="fingerprint-a",
        stop_continuation_ready=True,
    )


def managed_report() -> CapabilityReport:
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=True,
        reason_code="managed_owner_ready",
        evidence_fingerprint="fingerprint-managed",
        stop_continuation_ready=False,
    )


def event_stream_missing_report() -> CapabilityReport:
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=False,
        reason_code="event_stream_unavailable",
        evidence_fingerprint="fingerprint-a",
    )


def test_failed_activation_creates_no_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "state"

    with pytest.raises(ActivationError):
        _ = enable_session(
            root,
            request(root, observe_only=False),
            unavailable_report(),
        )

    assert not root.exists()


def test_observe_only_records_detection_mode_without_enabling_actions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"

    result = enable_session(
        root,
        request(root, observe_only=True),
        unavailable_report(),
    )

    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert result.warning_delivery_active is False
    assert result.effective_auto_restart is False
    assert runtime["observe_only"] is True


def test_activation_secures_root_before_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    secured: list[Path] = []

    def secure(target: Path) -> None:
        secured.append(target)
        target.mkdir()

    monkeypatch.setattr("scripts.setup.ensure_private_root", secure)

    _ = enable_session(root, request(root, observe_only=True), unavailable_report())

    assert secured == [root]
    assert config_path(root).is_file()


def test_observe_only_requires_the_rollout_event_stream(tmp_path: Path) -> None:
    root = tmp_path / "state"

    with pytest.raises(ActivationError):
        _ = enable_session(
            root,
            request(root, observe_only=True),
            event_stream_missing_report(),
        )

    assert not root.exists()


def test_activation_persists_config_and_only_current_session(tmp_path: Path) -> None:
    root = tmp_path / "state"

    result = enable_session(root, request(root, observe_only=False), ready_report())

    config = load_state(root, config_path(root)).values
    runtime_file = runtime_path(root, SESSION_ID)
    runtime = load_state(root, runtime_file).values
    assert result.effective_auto_restart is False
    assert config["message_preset"] == "cleanup"
    assert "nudge_message" not in config
    assert runtime["session_id"] == SESSION_ID
    assert runtime["enabled"] is True
    assert runtime["observe_only"] is False
    assert runtime["transcript_path"] == "sessions/rollout.jsonl"
    assert runtime["children"] == {}
    assert SESSION_ID not in runtime_file.name


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths")
def test_activation_accepts_windows_extended_rollout_path(tmp_path: Path) -> None:
    # Given: Codex reports the valid rollout with the Windows extended-path prefix.
    root = tmp_path / "codex-home" / "codex-must-work"
    activation = request(root, observe_only=True)
    extended = Path("\\\\?\\" + str(activation.transcript_path))

    # When: the current session is activated from that locator path.
    _ = enable_session(
        root,
        replace(activation, transcript_path=extended),
        unavailable_report(),
    )

    # Then: activation stores the same CODEX_HOME-relative rollout identity.
    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert runtime["transcript_path"] == "sessions/rollout.jsonl"


def test_disable_removes_runtime_and_cursor_but_keeps_config(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _ = enable_session(root, request(root, observe_only=False), ready_report())
    save_state(
        root,
        cursor_path(root, SESSION_ID),
        StateDocument(values={"offset": 4}),
    )

    disable_session(root, SESSION_ID)

    assert config_path(root).exists()
    assert not runtime_path(root, SESSION_ID).exists()
    assert not cursor_path(root, SESSION_ID).exists()


def test_complete_records_one_heartbeat_and_removes_runtime(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _ = enable_session(root, request(root, observe_only=False), ready_report())

    complete_session(root, SESSION_ID, datetime(2026, 7, 18, tzinfo=UTC))
    complete_session(root, SESSION_ID, datetime(2026, 7, 18, tzinfo=UTC))

    log = (root / "logs" / "diagnostic.jsonl").read_text(encoding="utf-8")
    assert log.count('"code":"watcher_completed"') == 1
    assert not runtime_path(root, SESSION_ID).exists()


def test_managed_completion_waits_for_owned_turn_before_runtime_removal(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _ = enable_session(root, request(root, observe_only=False), managed_report())
    path = runtime_path(root, SESSION_ID)
    document = load_state(root, path)
    values = dict(document.values)
    values["managed_turn_id"] = "turn-owned"
    values["manager_ready"] = True
    save_state(root, path, StateDocument(values=values))

    complete_session(root, SESSION_ID, datetime(2026, 7, 18, tzinfo=UTC))

    runtime = load_state(root, path).values
    assert runtime["shutdown_requested"] is True
    assert runtime["shutdown_interrupt"] is False
    assert runtime["managed_turn_id"] == "turn-owned"


def test_managed_disable_removes_runtime_when_manager_never_became_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _ = enable_session(root, request(root, observe_only=False), managed_report())
    path = runtime_path(root, SESSION_ID)
    values = dict(load_state(root, path).values)
    values["managed_turn_id"] = "turn-unowned"
    values["manager_ready"] = False
    save_state(root, path, StateDocument(values=values))

    disable_session(root, SESSION_ID)

    assert not path.exists()


def test_disable_removes_corrupt_runtime_and_cursor_without_parsing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _ = enable_session(root, request(root, observe_only=False), ready_report())
    runtime = runtime_path(root, SESSION_ID)
    cursor = cursor_path(root, SESSION_ID)
    cursor.parent.mkdir(parents=True)
    _ = runtime.write_text("not json", encoding="utf-8")
    _ = cursor.write_text("not json", encoding="utf-8")

    disable_session(root, SESSION_ID)

    assert not runtime.exists()
    assert not cursor.exists()


def test_disable_started_during_enable_wins_after_enable_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    config_saved = Event()
    release_enable = Event()
    disable_finished = Event()

    def blocking_save(state_root: Path, path: Path, document: StateDocument) -> None:
        save_state(state_root, path, document)
        if path == config_path(root):
            config_saved.set()
            if not release_enable.wait(1.0):
                raise AssertionError

    def enable() -> None:
        _ = enable_session(root, request(root, observe_only=False), ready_report())

    def disable() -> None:
        disable_session(root, SESSION_ID)
        disable_finished.set()

    monkeypatch.setattr("scripts.setup.save_state", blocking_save)
    enable_thread = Thread(target=enable)
    disable_thread = Thread(target=disable)
    enable_thread.start()
    try:
        assert config_saved.wait(1.0)
        disable_thread.start()
        assert not disable_finished.wait(0.1)
    finally:
        release_enable.set()
        enable_thread.join(2.0)
        if disable_thread.ident is not None:
            disable_thread.join(2.0)

    assert disable_finished.is_set()
    assert not runtime_path(root, SESSION_ID).exists()


def test_activation_rejects_mismatched_rollout_identity(tmp_path: Path) -> None:
    root = tmp_path / "state"
    activation = request(root, observe_only=True)
    write_session_meta(activation.transcript_path, "019ba8f0-7b5a-7000-8000-000000000002")

    with pytest.raises(ActivationError, match="rollout_session_mismatch"):
        _ = enable_session(root, activation, unavailable_report())

    assert not root.exists()
