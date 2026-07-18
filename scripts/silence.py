"""Provide deterministic heartbeat-silence state transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum, unique
from math import isfinite
from typing import assert_never

type Seconds = float
type MonotonicSeconds = float


@unique
class ProgressKind(StrEnum):
    """Progress timestamps tracked by the detector."""

    ITEM = "item"
    DELTA = "delta"
    TOOL_RESULT = "tool_result"


@unique
class MonitorPhase(StrEnum):
    """Lifecycle phases for one monitored child."""

    HEALTHY = "healthy"
    PAUSED = "paused"
    SILENT_WARNED = "silent_warned"
    RESTART_ELIGIBLE = "restart_eligible"
    RESTARTED = "restarted"
    TERMINAL = "terminal"


@unique
class Action(StrEnum):
    """Single side effect requested by an evaluation."""

    NONE = "none"
    WARNING = "warning"
    RESTART_LIMITED = "restart_limited"
    AUTO_RESTART = "auto_restart"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """User-confirmed warning and restart durations."""

    warning: Seconds
    restart: Seconds

    def __post_init__(self) -> None:
        """Reject unsafe threshold relationships."""
        finite = isfinite(self.warning) and isfinite(self.restart)
        if not finite or self.warning <= 0 or self.restart <= self.warning:
            raise ValueError


@dataclass(frozen=True, slots=True)
class RestartGate:
    """Evidence required before a targeted automatic restart."""

    requested_by_user: bool
    capability_ready: bool
    latest_event_check_passed: bool
    target_generation_verified: bool
    clock_reliable: bool
    restart_eligible_siblings: int = 1

    @property
    def effective(self) -> bool:
        """Return whether every restart prerequisite is verified."""
        return (
            self.requested_by_user
            and self.capability_ready
            and self.latest_event_check_passed
            and self.target_generation_verified
            and self.clock_reliable
            and self.restart_eligible_siblings == 1
        )


@dataclass(frozen=True, slots=True)
class WaitState:
    """Confirmed reasons why silence time must not advance."""

    open_tool_count: int = 0
    waiting_for_approval: bool = False
    waiting_for_user: bool = False
    child_terminal: bool = False
    parent_complete: bool = False

    def __post_init__(self) -> None:
        """Reject impossible nested-tool counts."""
        if self.open_tool_count < 0:
            raise ValueError

    @property
    def terminal(self) -> bool:
        """Return whether the watcher should stop for this child."""
        return self.child_terminal or self.parent_complete

    @property
    def paused(self) -> bool:
        """Return whether a nonterminal wait is confirmed."""
        return not self.terminal and (
            self.open_tool_count > 0 or self.waiting_for_approval or self.waiting_for_user
        )


@dataclass(frozen=True, slots=True)
class SilenceState:
    """Detector state for one child and one generation."""

    phase: MonitorPhase
    silence_id: str
    silence_sequence: int
    silence_started_at: MonotonicSeconds
    waits: WaitState
    item_at: MonotonicSeconds | None = None
    delta_at: MonotonicSeconds | None = None
    tool_result_at: MonotonicSeconds | None = None
    warning_emitted: bool = False
    restart_eligibility_emitted: bool = False


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Next detector state and at most one requested action."""

    state: SilenceState
    action: Action


def initial_state(at: MonotonicSeconds) -> SilenceState:
    """Create a healthy detector at child creation or turn start."""
    return SilenceState(
        phase=MonitorPhase.HEALTHY,
        silence_id=f"0:{float(at).hex()}",
        silence_sequence=0,
        silence_started_at=at,
        waits=WaitState(),
    )


def latest_progress_at(state: SilenceState) -> MonotonicSeconds:
    """Return the latest progress or silence-start time."""
    progress = (state.silence_started_at, state.item_at, state.delta_at, state.tool_result_at)
    return max(value for value in progress if value is not None)


def _next_interval(state: SilenceState, at: MonotonicSeconds) -> SilenceState:
    sequence = state.silence_sequence + 1
    return replace(
        state,
        phase=MonitorPhase.PAUSED if state.waits.paused else MonitorPhase.HEALTHY,
        silence_id=f"{sequence}:{float(at).hex()}",
        silence_sequence=sequence,
        warning_emitted=False,
        restart_eligibility_emitted=False,
    )


def record_progress(
    state: SilenceState,
    kind: ProgressKind,
    at: MonotonicSeconds,
) -> SilenceState:
    """Record fresh progress and open a new silence interval."""
    if state.waits.terminal or at < latest_progress_at(state):
        return state
    match kind:
        case ProgressKind.ITEM:
            updated = replace(state, silence_started_at=at, item_at=at)
        case ProgressKind.DELTA:
            updated = replace(state, silence_started_at=at, delta_at=at)
        case ProgressKind.TOOL_RESULT:
            updated = replace(state, silence_started_at=at, tool_result_at=at)
        case _:
            assert_never(kind)
    return _next_interval(updated, at)


def rearm_cancelled_restart(
    state: SilenceState,
    at: MonotonicSeconds,
) -> SilenceState:
    """Start a fresh interval after a queued whole-turn restart is cancelled."""
    if state.phase is not MonitorPhase.RESTARTED or state.waits.terminal:
        return state
    return _next_interval(replace(state, silence_started_at=at), at)


def record_tool_started(state: SilenceState, at: MonotonicSeconds) -> SilenceState:
    """Count item progress and begin one nested tool wait."""
    waits = replace(state.waits, open_tool_count=state.waits.open_tool_count + 1)
    return record_progress(replace(state, waits=waits), ProgressKind.ITEM, at)


def record_tool_result(state: SilenceState, at: MonotonicSeconds) -> SilenceState:
    """Finish one nested tool wait and restart silence at its result."""
    waits = replace(state.waits, open_tool_count=max(0, state.waits.open_tool_count - 1))
    return record_progress(replace(state, waits=waits), ProgressKind.TOOL_RESULT, at)


def set_wait_state(
    state: SilenceState,
    waits: WaitState,
    at: MonotonicSeconds,
    *,
    resume_confirmed: bool,
) -> SilenceState:
    """Apply proven waits while keeping an unproven resume paused."""
    if waits.terminal:
        return replace(state, phase=MonitorPhase.TERMINAL, waits=waits)
    if waits.paused:
        return replace(state, phase=MonitorPhase.PAUSED, waits=waits)
    if state.waits.paused and not resume_confirmed:
        return replace(state, phase=MonitorPhase.PAUSED)
    resumed = replace(state, waits=waits)
    if not state.waits.paused:
        return resumed
    resumed = replace(resumed, silence_started_at=at)
    return _next_interval(resumed, at)


def evaluate(
    state: SilenceState,
    now: MonotonicSeconds,
    thresholds: Thresholds,
    restart_gate: RestartGate,
) -> Evaluation:
    """Advance one tick and request no more than one action."""
    phase = state.phase
    warning_emitted = state.warning_emitted
    restart_emitted = state.restart_eligibility_emitted
    action = Action.NONE
    elapsed = now - latest_progress_at(state)
    if state.waits.terminal:
        phase, action = MonitorPhase.TERMINAL, Action.STOP
    elif state.waits.paused:
        phase = MonitorPhase.PAUSED
    elif elapsed < thresholds.warning:
        phase = MonitorPhase.HEALTHY
    elif not warning_emitted:
        phase, action, warning_emitted = MonitorPhase.SILENT_WARNED, Action.WARNING, True
    elif elapsed < thresholds.restart:
        phase = MonitorPhase.SILENT_WARNED
    elif restart_gate.effective and phase is not MonitorPhase.RESTARTED:
        phase, action, restart_emitted = MonitorPhase.RESTARTED, Action.AUTO_RESTART, True
    elif not restart_emitted:
        phase = MonitorPhase.RESTART_ELIGIBLE
        action = Action.RESTART_LIMITED
        restart_emitted = True
    next_state = replace(
        state,
        phase=phase,
        warning_emitted=warning_emitted,
        restart_eligibility_emitted=restart_emitted,
    )
    return Evaluation(next_state, action)
