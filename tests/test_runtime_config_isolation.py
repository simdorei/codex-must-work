from datetime import UTC, datetime
from pathlib import Path

from scripts.control import CapabilityReport
from scripts.durations import Milliseconds
from scripts.manager_runtime import load_manager_runtime
from scripts.setup import ActivationRequest, MessagePreset, Settings, enable_session
from scripts.state import load_state, runtime_path
from scripts.watcher_state import runtime_target_from_values
from tests.rollout_fixture import SESSION_ID, write_session_meta

_SECOND_SESSION_ID = "019ba8f0-7b5a-7000-8000-000000000002"


def _request(
    root: Path,
    session_id: str,
    *,
    warning_ms: int,
    preset: MessagePreset,
) -> ActivationRequest:
    transcript = root.parent / "sessions" / f"{session_id}.jsonl"
    write_session_meta(transcript, session_id)
    return ActivationRequest(
        session_id=session_id,
        transcript_path=transcript,
        settings=Settings(
            warning_after_ms=Milliseconds(warning_ms),
            restart_after_ms=Milliseconds(warning_ms + 120_000),
            message_preset=preset,
            auto_restart_requested_by_user=True,
        ),
        observe_only=False,
        permission_mode="bypassPermissions",
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )


def test_each_runtime_keeps_its_activation_settings_after_global_config_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex-must-work"
    capabilities = CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=True,
        reason_code="managed_owner_ready",
        evidence_fingerprint="digest",
    )
    _ = enable_session(
        root,
        _request(root, SESSION_ID, warning_ms=90_000, preset=MessagePreset.CLEANUP),
        capabilities,
    )
    _ = enable_session(
        root,
        _request(
            root,
            _SECOND_SESSION_ID,
            warning_ms=180_000,
            preset=MessagePreset.CONTINUE,
        ),
        capabilities,
    )

    first_path = runtime_path(root, SESSION_ID)
    second_path = runtime_path(root, _SECOND_SESSION_ID)
    first_manager = load_manager_runtime(root, first_path.name)
    second_manager = load_manager_runtime(root, second_path.name)
    first_watcher = runtime_target_from_values(
        root,
        first_path,
        load_state(root, first_path).values,
    )
    second_watcher = runtime_target_from_values(
        root,
        second_path,
        load_state(root, second_path).values,
    )

    assert first_manager is not None
    assert first_manager.message_preset == "cleanup"
    assert second_manager is not None
    assert second_manager.message_preset == "continue"
    assert first_watcher.thresholds.warning == 90.0
    assert second_watcher.thresholds.warning == 180.0
