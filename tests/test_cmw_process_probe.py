from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.cmw_process_probe_events import AuditEvent, EventKind, ProcessIdentity
from tests.cmw_process_probe_io import OutputPathError, parse_output_path
from tests.cmw_process_probe_models import (
    LossCounters,
    ProbeLimits,
    ProcessSample,
    TraceWindow,
    evaluate_audit,
)

if TYPE_CHECKING:
    from pathlib import Path


def _identity(pid: int = 41, created_ns: int = 100) -> ProcessIdentity:
    return ProcessIdentity(pid=pid, created_ns=created_ns)


def _sample(  # noqa: PLR0913 - fixture builder exposes independent counters.
    *,
    identity: ProcessIdentity | None = None,
    cpu: float = 1.0,
    handles: int = 8,
    threads: int = 3,
    heartbeat: float = 10.0,
    spawns: int = 5,
) -> ProcessSample:
    return ProcessSample(
        identity=identity or _identity(),
        cpu_seconds=cpu,
        handle_count=handles,
        thread_count=threads,
        heartbeat_monotonic=heartbeat,
        child_spawn_counter=spawns,
    )


def _window(
    *,
    losses: LossCounters | None = None,
    events: tuple[AuditEvent, ...] = (),
) -> TraceWindow:
    return TraceWindow(
        provider_started_ns=1,
        bootstrap_boundary_ns=2,
        coverage_end_ns=9,
        provider_stopped_ns=10,
        monitor=_identity(99, 900),
        losses=losses or LossCounters(),
        events=events,
    )


def _limits(**changes: float | bool) -> ProbeLimits:
    values: dict[str, float | int | bool] = {
        "max_cpu_seconds": 0.25,
        "max_handle_growth": 4,
        "max_thread_growth": 0,
        "max_heartbeat_gap_seconds": 15.0,
        "max_descendant_starts": 0,
        "max_wmi_operations": 0,
        "require_zero_event_loss": True,
        "expect_process_alive": True,
    }
    values.update(changes)
    return ProbeLimits.from_values(values)


@pytest.mark.parametrize(
    ("losses", "reason"),
    [
        (LossCounters(events_lost=1), "event_loss"),
        (LossCounters(buffers_lost=1), "buffer_loss"),
        (LossCounters(provider_losses=(("kernel-process", 1),)), "provider_loss"),
    ],
)
def test_audit_is_inconclusive_when_trace_reports_loss(
    losses: LossCounters,
    reason: str,
) -> None:
    # Given
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))

    # When
    result = evaluate_audit(samples, _window(losses=losses), _limits())

    # Then
    assert result.outcome == "inconclusive"
    assert reason in result.reasons


def test_zero_aggregate_loss_is_sufficient_without_provider_attribution() -> None:
    # Given
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))

    # When
    result = evaluate_audit(
        samples,
        _window(losses=LossCounters(provider_losses=None)),
        _limits(),
    )

    # Then
    assert result.outcome == "passed"


def test_events_are_windowed_and_deduplicated_before_ownership_counting() -> None:
    # Given
    child = _identity(50, 500)
    outside = AuditEvent(
        EventKind.PROCESS_START,
        1,
        _identity(),
        subject=_identity(51, 501),
        parent=_identity(),
    )
    inside = AuditEvent(
        EventKind.PROCESS_START,
        4,
        _identity(),
        subject=child,
        parent=_identity(),
    )
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))

    # When
    result = evaluate_audit(
        samples,
        _window(events=(outside, inside, inside)),
        _limits(max_descendant_starts=1),
    )

    # Then
    assert result.outcome == "passed"
    assert result.public_counters["descendant_starts"] == 1


def test_wmi_events_with_same_operation_identity_count_once() -> None:
    # Given
    first = AuditEvent(
        EventKind.WMI_OPERATION,
        4,
        _identity(),
        operation_key=(8, 9),
    )
    duplicate = AuditEvent(
        EventKind.WMI_OPERATION,
        5,
        _identity(),
        operation_key=(8, 9),
    )
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))

    # When
    result = evaluate_audit(
        samples,
        _window(events=(first, duplicate)),
        _limits(max_wmi_operations=1),
    )

    # Then
    assert result.outcome == "passed"
    assert result.public_counters["wmi_operations"] == 1


def test_audit_rejects_pid_reuse_even_when_numeric_pid_matches() -> None:
    # Given
    samples = (_sample(), _sample(identity=_identity(created_ns=101)))

    # When
    result = evaluate_audit(samples, _window(), _limits())

    # Then
    assert result.outcome == "inconclusive"
    assert result.reasons == ("pid_reuse",)


def test_audit_rejects_monotonic_spawn_counter_wrap() -> None:
    # Given
    samples = (_sample(spawns=9), _sample(spawns=2))

    # When
    result = evaluate_audit(samples, _window(), _limits())

    # Then
    assert result.outcome == "inconclusive"
    assert result.reasons == ("child_spawn_counter_regressed",)


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (_sample(handles=13), "handle_growth"),
        (_sample(threads=4), "thread_growth"),
        (_sample(heartbeat=26.0), "heartbeat_gap"),
    ],
)
def test_audit_fails_when_resource_limit_is_exceeded(
    sample: ProcessSample,
    reason: str,
) -> None:
    # Given
    samples = (_sample(), sample)

    # When
    result = evaluate_audit(samples, _window(), _limits())

    # Then
    assert result.outcome == "failed"
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (
            AuditEvent(
                kind=EventKind.PROCESS_START,
                timestamp_ns=4,
                actor=_identity(),
                subject=_identity(50, 500),
                parent=_identity(),
            ),
            "descendant_starts",
        ),
        (
            AuditEvent(
                kind=EventKind.WMI_OPERATION,
                timestamp_ns=5,
                actor=_identity(),
            ),
            "wmi_operations",
        ),
    ],
)
def test_audit_fails_for_injected_owned_activity(
    event: AuditEvent,
    reason: str,
) -> None:
    # Given
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))

    # When
    result = evaluate_audit(samples, _window(events=(event,)), _limits())

    # Then
    assert result.outcome == "failed"
    assert reason in result.reasons


def test_audit_passes_with_exact_identity_and_public_counters() -> None:
    # Given
    samples = (_sample(), _sample(cpu=1.2, handles=10, heartbeat=12.0))

    # When
    result = evaluate_audit(samples, _window(), _limits())

    # Then
    assert result.outcome == "passed"
    assert abs(result.public_counters["cpu_seconds"] - 0.2) < 1e-9
    assert "capability" not in repr(result.public_counters).lower()


def test_audit_rejects_loss_free_output_without_event_coverage() -> None:
    # Given
    samples = (_sample(), _sample(cpu=1.1, heartbeat=11.0))
    trace = TraceWindow(
        1,
        2,
        9,
        10,
        _identity(99, 900),
        LossCounters(),
        (),
        event_coverage_complete=False,
    )

    # When
    result = evaluate_audit(samples, trace, _limits())

    # Then
    assert result.outcome == "inconclusive"
    assert result.reasons == ("event_coverage_incomplete",)


def test_output_path_must_be_new_json_inside_existing_directory(tmp_path: Path) -> None:
    # Given
    existing = tmp_path / "result.json"
    _ = existing.write_text("occupied", encoding="utf-8")

    # When / Then
    with pytest.raises(OutputPathError):
        _ = parse_output_path(existing)
