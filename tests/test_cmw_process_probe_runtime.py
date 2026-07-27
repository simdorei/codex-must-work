from __future__ import annotations

from typing import final

import pytest

from tests.cmw_process_probe_events import ProcessIdentity
from tests.cmw_process_probe_models import (
    LossCounters,
    ProbeLimits,
    ProcessSample,
    TraceWindow,
)
from tests.cmw_process_probe_runtime import (
    Lifecycle,
    ProbeExecutionError,
    ProbeRun,
    ProbeRuntime,
)


@final
class FakeDependencies:
    def __init__(
        self,
        failure: str | None = None,
        interrupt_at: int | None = None,
    ) -> None:
        self.failure = failure
        self.interrupt_at = interrupt_at
        self.actions: list[str] = []
        self.calls = 0
        self.identity = ProcessIdentity(41, 100)

    def start_trace(self) -> None:
        self.actions.append("provider_start")
        if self.failure == "provider":
            reason = "provider_start_failed"
            raise ProbeExecutionError(reason)

    def stop_trace(self) -> TraceWindow:
        self.actions.append("provider_stop")
        return TraceWindow(
            1,
            2,
            8,
            9,
            ProcessIdentity(99, 900),
            LossCounters(),
            (),
        )

    def sample(self) -> ProcessSample:
        self.actions.append("sample")
        return ProcessSample(self.identity, 1.0, 8, 3, float(self.calls), 0)

    def control(self, action: str) -> str:
        self.actions.append(action)
        self.calls += 1
        if self.interrupt_at == self.calls:
            raise KeyboardInterrupt
        return "inactive" if action in {"status_initial", "status_final"} else "active"

    def boundary_ns(self) -> int:
        self.actions.append("boundary")
        return 2

    def before_stop(self) -> None:
        self.actions.append("duration")

    def quiesce(self) -> None:
        self.actions.append("quiesce")


def _run(lifecycle: Lifecycle = Lifecycle.START_STOP, cycles: int = 2) -> ProbeRun:
    return ProbeRun(
        lifecycle=lifecycle,
        cycle_count=cycles,
        limits=ProbeLimits(
            max_cpu_seconds=0.25,
            max_handle_growth=4,
            max_thread_growth=0,
            max_heartbeat_gap_seconds=15.0,
            max_descendant_starts=0,
            max_wmi_operations=0,
            require_zero_event_loss=True,
            expect_process_alive=True,
        ),
    )


def test_runtime_orders_provider_before_boundary_and_tail_before_stop() -> None:
    # Given
    dependencies = FakeDependencies()

    # When
    result = ProbeRuntime(dependencies).run(_run())

    # Then
    assert result.outcome == "passed"
    assert dependencies.actions == [
        "provider_start",
        "status_initial",
        "sample",
        "boundary",
        "start",
        "status",
        "status",
        "duration",
        "stop",
        "status_final",
        "sample",
        "quiesce",
        "provider_stop",
    ]


def test_runtime_surfaces_provider_startup_failure_without_measurement() -> None:
    # Given
    dependencies = FakeDependencies(failure="provider")

    # When / Then
    with pytest.raises(ProbeExecutionError, match="provider_start_failed"):
        _ = ProbeRuntime(dependencies).run(_run())
    assert dependencies.actions == ["provider_start"]


def test_runtime_stops_provider_when_repeated_cycle_is_interrupted() -> None:
    # Given
    dependencies = FakeDependencies(interrupt_at=3)

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        _ = ProbeRuntime(dependencies).run(_run(cycles=5))
    assert dependencies.actions[-1] == "provider_stop"


def test_observe_inactive_keeps_same_resident_and_performs_no_mutation() -> None:
    # Given
    dependencies = FakeDependencies()

    # When
    result = ProbeRuntime(dependencies).run(_run(Lifecycle.OBSERVE_INACTIVE, cycles=0))

    # Then
    assert result.outcome == "passed"
    assert "start" not in dependencies.actions
    assert "stop" not in dependencies.actions
    assert dependencies.actions.count("sample") == 2
