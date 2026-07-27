"""Wire the lifecycle engine to the exact resident endpoint and Windows ETW."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Final, TypedDict, final

from scripts.daemon_control_endpoint_identity import current_process_created_ns
from tests.cmw_process_probe_etw_session import EtwSession
from tests.cmw_process_probe_events import ProcessIdentity
from tests.cmw_process_probe_runtime import ProbeExecutionError
from tests.cmw_process_probe_sampler import sample_process

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.mcp_protocol import JsonObject
    from tests.cmw_process_probe_endpoint import EndpointClient
    from tests.cmw_process_probe_io import SessionLocator
    from tests.cmw_process_probe_models import (
        AuditResult,
        ProcessSample,
        PublicCounters,
        TraceWindow,
    )

_QUIESCENCE_SECONDS: Final = 2.0
_FINAL_STATUS_TIMEOUT_SECONDS: Final = 15.0


class PublicEvidence(TypedDict):
    outcome: str
    reasons: list[str]
    daemon_pid: int
    daemon_process_created_ns: int
    monitor_pid: int
    monitor_process_created_ns: int
    provider_started_ns: int
    bootstrap_boundary_ns: int
    coverage_end_ns: int
    provider_stopped_ns: int
    enabled_providers: list[str]
    records_seen: int
    provider_records: dict[str, int]
    decoded_events: int
    sentinel_verified: bool
    counters: dict[str, float | int | None]


@final
class LiveDependencies:
    """Own live resources for one exact daemon audit."""

    def __init__(
        self,
        daemon_pid: int,
        locator: SessionLocator,
        client: EndpointClient,
        duration_seconds: float,
        output_directory: Path,
    ) -> None:
        self._daemon_pid = daemon_pid
        self._locator = locator
        self._client = client
        self._duration_seconds = duration_seconds
        self._trace = EtwSession(output_directory)
        self._boundary_wall_ns = 0
        self._boundary_monotonic = 0.0
        self._coverage_end_ns = 0
        self._heartbeat = time.monotonic()
        self._samples: list[ProcessSample] = []
        self._completed_trace: TraceWindow | None = None

    def start_trace(self) -> None:
        if os.name != "nt":
            reason = "posix_child_spawn_counter_unavailable"
            raise ProbeExecutionError(reason)
        monitor = ProcessIdentity(os.getpid(), current_process_created_ns())
        self._trace.start(monitor)

    def stop_trace(self) -> TraceWindow:
        coverage = self._coverage_end_ns or time.time_ns()
        boundary = self._boundary_wall_ns or coverage
        trace = self._trace.stop(
            bootstrap_boundary_ns=boundary,
            coverage_end_ns=coverage,
        )
        self._completed_trace = trace
        return trace

    def sample(self) -> ProcessSample:
        sample = sample_process(self._daemon_pid, self._heartbeat)
        self._samples.append(sample)
        return sample

    def control(self, action: str) -> str:
        arguments: JsonObject = {
            "session_id": self._locator.session_id,
            "control_capability": self._locator.control_capability,
        }
        if action == "start":
            arguments["transcript_path"] = str(self._locator.transcript_path)
            arguments["goal_companion"] = False
            arguments["observe_only"] = True
            if self._locator.permission_mode in {
                "default",
                "acceptEdits",
                "plan",
                "dontAsk",
                "bypassPermissions",
            }:
                arguments["permission_mode"] = self._locator.permission_mode
            response = self._client.call("cmw.start", arguments)
        elif action == "stop":
            response = self._client.call("cmw.stop", arguments)
        else:
            response = (
                self._status_until_inactive(arguments)
                if action == "status_final"
                else (self._client.call("cmw.status", arguments))
            )
        self._heartbeat = time.monotonic()
        status = response.get("status")
        return status if type(status) is str else "invalid"

    def boundary_ns(self) -> int:
        self._boundary_wall_ns = time.time_ns()
        self._boundary_monotonic = time.monotonic()
        return self._boundary_wall_ns

    def before_stop(self) -> None:
        remaining = self._duration_seconds - (time.monotonic() - self._boundary_monotonic)
        if remaining > 0:
            time.sleep(remaining)

    def quiesce(self) -> None:
        time.sleep(_QUIESCENCE_SECONDS)
        self._coverage_end_ns = time.time_ns()

    def _status_until_inactive(self, arguments: JsonObject) -> JsonObject:
        deadline = time.monotonic() + _FINAL_STATUS_TIMEOUT_SECONDS
        while True:
            response = self._client.call("cmw.status", arguments)
            if response.get("status") == "inactive":
                return response
            if time.monotonic() >= deadline:
                return response
            time.sleep(0.05)

    def public_evidence(self, result: AuditResult) -> PublicEvidence:
        """Build a secret-free report only after trace completion."""
        trace = self._completed_trace
        if trace is None or not self._samples:
            reason = "probe_evidence_incomplete"
            raise ProbeExecutionError(reason)
        daemon = self._samples[-1].identity
        return PublicEvidence(
            outcome=result.outcome,
            reasons=list(result.reasons),
            daemon_pid=daemon.pid,
            daemon_process_created_ns=daemon.created_ns,
            monitor_pid=trace.monitor.pid,
            monitor_process_created_ns=trace.monitor.created_ns,
            provider_started_ns=trace.provider_started_ns,
            bootstrap_boundary_ns=trace.bootstrap_boundary_ns,
            coverage_end_ns=trace.coverage_end_ns,
            provider_stopped_ns=trace.provider_stopped_ns,
            enabled_providers=list(trace.enabled_providers),
            records_seen=trace.records_seen,
            provider_records=dict(trace.provider_records),
            decoded_events=len(trace.events),
            sentinel_verified=trace.sentinel_verified,
            counters=_numeric_counters(result.public_counters),
        )


def _numeric_counters(values: PublicCounters) -> dict[str, int | float | None]:
    return {
        "cpu_seconds": values["cpu_seconds"],
        "handle_growth": values["handle_growth"],
        "thread_growth": values["thread_growth"],
        "max_heartbeat_gap_seconds": values["max_heartbeat_gap_seconds"],
        "descendant_starts": values["descendant_starts"],
        "wmi_operations": values["wmi_operations"],
        "events_lost": values["events_lost"],
        "buffers_lost": values["buffers_lost"],
        "provider_loss": values["provider_loss"],
    }
