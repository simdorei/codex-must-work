"""Lifecycle orchestration independent of operating-system trace adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, assert_never, final

from tests.cmw_process_probe_models import (
    AuditResult,
    ProbeLimits,
    ProcessSample,
    TraceWindow,
    evaluate_audit,
)


class Lifecycle(StrEnum):
    START_STOP = "start-stop"
    OBSERVE_INACTIVE = "observe-inactive"


@dataclass(frozen=True, slots=True)
class ProbeRun:
    lifecycle: Lifecycle
    cycle_count: int
    limits: ProbeLimits


@final
class ProbeExecutionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class RuntimeDependencies(Protocol):
    def start_trace(self) -> None: ...

    def stop_trace(self) -> TraceWindow: ...

    def sample(self) -> ProcessSample: ...

    def control(self, action: str) -> str: ...

    def boundary_ns(self) -> int: ...

    def before_stop(self) -> None: ...

    def quiesce(self) -> None: ...


@final
class ProbeRuntime:
    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self._dependencies = dependencies

    def run(self, run: ProbeRun) -> AuditResult:
        """Drive one exact lifecycle while keeping trace teardown unconditional."""
        self._dependencies.start_trace()
        trace_stopped = False
        samples: tuple[ProcessSample, ...] = ()
        try:
            if self._dependencies.control("status_initial") != "inactive":
                reason = "initial_state_active"
                raise ProbeExecutionError(reason)
            initial = self._dependencies.sample()
            _ = self._dependencies.boundary_ns()
            self._execute_lifecycle(run)
            if self._dependencies.control("status_final") != "inactive":
                reason = "final_state_active"
                raise ProbeExecutionError(reason)
            final = self._dependencies.sample()
            samples = (initial, final)
            self._dependencies.quiesce()
            trace = self._dependencies.stop_trace()
            trace_stopped = True
        finally:
            if not trace_stopped:
                _ = self._dependencies.stop_trace()
        return evaluate_audit(samples, trace, run.limits)

    def _execute_lifecycle(self, run: ProbeRun) -> None:
        if run.lifecycle is Lifecycle.START_STOP:
            _ = self._dependencies.control("start")
            for _index in range(run.cycle_count):
                _ = self._dependencies.control("status")
            self._dependencies.before_stop()
            _ = self._dependencies.control("stop")
            return
        if run.lifecycle is Lifecycle.OBSERVE_INACTIVE:
            if run.cycle_count != 0:
                reason = "inactive_cycles_must_be_zero"
                raise ProbeExecutionError(reason)
            return
        assert_never(run.lifecycle)
