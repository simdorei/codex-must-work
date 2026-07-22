import pytest

from scripts.control import CapabilityInputs, current_capabilities, evaluate_capabilities


def test_missing_event_stream_disables_every_action() -> None:
    # Given
    inputs = CapabilityInputs(
        event_stream_ready=False,
        same_live_server_attach=True,
        targeted_interrupt=True,
        reassign_existing_task=True,
        generation_guard=True,
        fingerprint="build-a",
    )

    # When
    result = evaluate_capabilities(inputs)

    # Then
    assert result.warning_delivery_ready is False
    assert result.auto_restart_ready is False
    assert result.reason_code == "event_stream_unavailable"


def test_warning_requires_same_live_server_attach() -> None:
    result = evaluate_capabilities(
        CapabilityInputs(
            event_stream_ready=True,
            same_live_server_attach=False,
            targeted_interrupt=True,
            reassign_existing_task=True,
            generation_guard=True,
            fingerprint="build-a",
        )
    )

    assert result.warning_delivery_ready is False
    assert result.auto_restart_ready is False
    assert result.reason_code == "same_live_server_attach_unavailable"


def test_warning_can_be_ready_without_restart_controls() -> None:
    result = evaluate_capabilities(
        CapabilityInputs(
            event_stream_ready=True,
            same_live_server_attach=True,
            targeted_interrupt=False,
            reassign_existing_task=False,
            generation_guard=False,
            fingerprint="build-a",
        )
    )

    assert result.warning_delivery_ready is True
    assert result.auto_restart_ready is False
    assert result.reason_code == "auto_restart_controls_unavailable"


def test_auto_restart_requires_every_gate() -> None:
    result = evaluate_capabilities(
        CapabilityInputs(
            event_stream_ready=True,
            same_live_server_attach=True,
            targeted_interrupt=True,
            reassign_existing_task=True,
            generation_guard=True,
            fingerprint="build-a",
        )
    )

    assert result.warning_delivery_ready is True
    assert result.auto_restart_ready is True
    assert result.reason_code == "ready"


def test_missing_capability_evidence_disables_every_action() -> None:
    # Given
    inputs = CapabilityInputs(
        event_stream_ready=True,
        same_live_server_attach=True,
        targeted_interrupt=True,
        reassign_existing_task=True,
        generation_guard=True,
        fingerprint=" ",
    )

    # When
    result = evaluate_capabilities(inputs)

    # Then
    assert result.warning_delivery_ready is False
    assert result.auto_restart_ready is False
    assert result.reason_code == "capability_evidence_missing"


def test_current_evidence_changes_with_the_control_schema() -> None:
    # Given / When
    first = current_capabilities("codex-a", "desktop-a", "schema-a")
    second = current_capabilities("codex-a", "desktop-a", "schema-b")

    # Then
    assert first.evidence_fingerprint != second.evidence_fingerprint


def test_current_runtime_supports_stop_continuation_without_live_attach() -> None:
    report = current_capabilities("codex-a", "desktop-a", "schema-a")

    assert report.stop_continuation_ready is True
    assert report.warning_delivery_ready is False
    assert report.auto_restart_ready is False


def test_current_capabilities_do_not_query_platform_wmi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> str:
        pytest.fail("platform module must not be queried")

    for name in ("system", "release"):
        monkeypatch.setattr(f"platform.{name}", fail)

    report = current_capabilities("codex-a", "desktop-a", "schema-a")

    assert report.evidence_fingerprint
