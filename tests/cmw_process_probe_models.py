"""Typed process-audit observations and threshold evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Literal, TypedDict

from tests.cmw_process_probe_events import (
    AuditEvent,
    ProcessIdentity,
    owned_event_counts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ProcessSample:
    identity: ProcessIdentity
    cpu_seconds: float
    handle_count: int
    thread_count: int
    heartbeat_monotonic: float
    child_spawn_counter: int


@dataclass(frozen=True, slots=True)
class LossCounters:
    events_lost: int = 0
    buffers_lost: int = 0
    provider_losses: tuple[tuple[str, int], ...] | None = ()


@dataclass(frozen=True, slots=True)
class TraceWindow:
    provider_started_ns: int
    bootstrap_boundary_ns: int
    coverage_end_ns: int
    provider_stopped_ns: int
    monitor: ProcessIdentity
    losses: LossCounters
    events: tuple[AuditEvent, ...]
    event_coverage_complete: bool = True
    enabled_providers: tuple[str, ...] = ()
    records_seen: int = 0
    provider_records: tuple[tuple[str, int], ...] = ()
    sentinel_verified: bool = False


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    max_cpu_seconds: float
    max_handle_growth: int
    max_thread_growth: int
    max_heartbeat_gap_seconds: float
    max_descendant_starts: int
    max_wmi_operations: int
    require_zero_event_loss: bool
    expect_process_alive: bool

    @classmethod
    def from_values(cls, values: Mapping[str, float | int | bool]) -> ProbeLimits:
        return cls(
            max_cpu_seconds=float(values["max_cpu_seconds"]),
            max_handle_growth=int(values["max_handle_growth"]),
            max_thread_growth=int(values["max_thread_growth"]),
            max_heartbeat_gap_seconds=float(values["max_heartbeat_gap_seconds"]),
            max_descendant_starts=int(values["max_descendant_starts"]),
            max_wmi_operations=int(values["max_wmi_operations"]),
            require_zero_event_loss=bool(values["require_zero_event_loss"]),
            expect_process_alive=bool(values["expect_process_alive"]),
        )


class PublicCounters(TypedDict):
    cpu_seconds: float
    handle_growth: int
    thread_growth: int
    max_heartbeat_gap_seconds: float
    descendant_starts: int
    wmi_operations: int
    events_lost: int
    buffers_lost: int
    provider_loss: int | None


@dataclass(frozen=True, slots=True)
class AuditResult:
    outcome: Literal["passed", "failed", "inconclusive"]
    reasons: tuple[str, ...]
    public_counters: PublicCounters


def evaluate_audit(
    samples: tuple[ProcessSample, ...],
    trace: TraceWindow,
    limits: ProbeLimits,
) -> AuditResult:
    """Evaluate complete, identity-bound observations without guessing missing data."""
    inconclusive = _coverage_reasons(samples, trace, limits)
    counters = _public_counters(samples, trace)
    if inconclusive:
        return AuditResult("inconclusive", tuple(inconclusive), counters)

    failures: list[str] = []
    if counters["cpu_seconds"] > limits.max_cpu_seconds:
        failures.append("cpu_seconds")
    if counters["handle_growth"] > limits.max_handle_growth:
        failures.append("handle_growth")
    if counters["thread_growth"] > limits.max_thread_growth:
        failures.append("thread_growth")
    if counters["max_heartbeat_gap_seconds"] > limits.max_heartbeat_gap_seconds:
        failures.append("heartbeat_gap")
    if counters["descendant_starts"] > limits.max_descendant_starts:
        failures.append("descendant_starts")
    if counters["wmi_operations"] > limits.max_wmi_operations:
        failures.append("wmi_operations")
    outcome: Literal["passed", "failed"] = "failed" if failures else "passed"
    return AuditResult(outcome, tuple(failures), counters)


def _coverage_reasons(
    samples: tuple[ProcessSample, ...],
    trace: TraceWindow,
    limits: ProbeLimits,
) -> list[str]:
    reasons: list[str] = []
    if len(samples) < 2:
        reasons.append("insufficient_samples")
        return reasons
    if not (
        trace.provider_started_ns
        <= trace.bootstrap_boundary_ns
        <= trace.coverage_end_ns
        <= trace.provider_stopped_ns
    ):
        reasons.append("trace_coverage")
    if not trace.event_coverage_complete:
        reasons.append("event_coverage_incomplete")
    identities = {sample.identity for sample in samples}
    if len(identities) != 1:
        reasons.append("pid_reuse")
    if samples[-1].child_spawn_counter < samples[0].child_spawn_counter:
        reasons.append("child_spawn_counter_regressed")
    if limits.require_zero_event_loss:
        reasons.extend(_loss_reasons(trace.losses))
    return reasons


def _loss_reasons(losses: LossCounters) -> list[str]:
    reasons: list[str] = []
    if losses.events_lost:
        reasons.append("event_loss")
    if losses.buffers_lost:
        reasons.append("buffer_loss")
    if losses.provider_losses is not None and any(count for _, count in losses.provider_losses):
        reasons.append("provider_loss")
    return reasons


def _public_counters(
    samples: tuple[ProcessSample, ...],
    trace: TraceWindow,
) -> PublicCounters:
    if len(samples) < 2:
        return _empty_counters(trace.losses)
    first, last = samples[0], samples[-1]
    descendant_starts, wmi_operations = owned_event_counts(
        first.identity,
        trace.events,
        bootstrap_boundary_ns=trace.bootstrap_boundary_ns,
        coverage_end_ns=trace.coverage_end_ns,
    )
    heartbeat_gaps = tuple(
        current.heartbeat_monotonic - previous.heartbeat_monotonic
        for previous, current in pairwise(samples)
    )
    return PublicCounters(
        cpu_seconds=max(0.0, last.cpu_seconds - first.cpu_seconds),
        handle_growth=last.handle_count - first.handle_count,
        thread_growth=last.thread_count - first.thread_count,
        max_heartbeat_gap_seconds=max(heartbeat_gaps, default=0.0),
        descendant_starts=descendant_starts,
        wmi_operations=wmi_operations,
        events_lost=trace.losses.events_lost,
        buffers_lost=trace.losses.buffers_lost,
        provider_loss=_provider_loss_total(trace.losses.provider_losses),
    )


def _empty_counters(losses: LossCounters) -> PublicCounters:
    return PublicCounters(
        cpu_seconds=0.0,
        handle_growth=0,
        thread_growth=0,
        max_heartbeat_gap_seconds=0.0,
        descendant_starts=0,
        wmi_operations=0,
        events_lost=losses.events_lost,
        buffers_lost=losses.buffers_lost,
        provider_loss=_provider_loss_total(losses.provider_losses),
    )


def _provider_loss_total(
    values: tuple[tuple[str, int], ...] | None,
) -> int | None:
    return None if values is None else sum(count for _, count in values)
