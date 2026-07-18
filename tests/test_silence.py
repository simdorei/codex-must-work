from __future__ import annotations

import pytest

from scripts.silence import (
    Action,
    MonitorPhase,
    ProgressKind,
    RestartGate,
    Thresholds,
    WaitState,
    evaluate,
    initial_state,
    record_progress,
    record_tool_result,
    record_tool_started,
    set_wait_state,
)

_THRESHOLDS = Thresholds(warning=120.0, restart=600.0)


def restart_gate(
    *,
    requested_by_user: bool,
    capability_ready: bool,
    restart_eligible_siblings: int = 1,
) -> RestartGate:
    return RestartGate(
        requested_by_user=requested_by_user,
        capability_ready=capability_ready,
        latest_event_check_passed=True,
        target_generation_verified=True,
        clock_reliable=True,
        restart_eligible_siblings=restart_eligible_siblings,
    )


_NO_RESTART = restart_gate(requested_by_user=False, capability_ready=False)


def test_warning_fires_once_then_progress_opens_a_new_interval() -> None:
    state = initial_state(0.0)

    warned = evaluate(state, 121.0, _THRESHOLDS, _NO_RESTART)
    assert warned.action is Action.WARNING
    assert warned.state.phase is MonitorPhase.SILENT_WARNED

    repeated = evaluate(warned.state, 400.0, _THRESHOLDS, _NO_RESTART)
    assert repeated.action is Action.NONE

    progressed = record_progress(repeated.state, ProgressKind.DELTA, 500.0)
    assert progressed.warning_emitted is False
    assert progressed.silence_sequence == 1
    assert evaluate(progressed, 619.0, _THRESHOLDS, _NO_RESTART).action is Action.NONE
    assert evaluate(progressed, 621.0, _THRESHOLDS, _NO_RESTART).action is Action.WARNING


def test_twenty_minute_tool_wait_is_paused_and_result_resets_silence() -> None:
    waiting = record_tool_started(initial_state(0.0), 10.0)

    paused = evaluate(waiting, 1_210.0, _THRESHOLDS, _NO_RESTART)
    assert paused.action is Action.NONE
    assert paused.state.phase is MonitorPhase.PAUSED

    resumed = record_tool_result(paused.state, 1_210.0)
    assert resumed.waits.open_tool_count == 0
    assert resumed.phase is MonitorPhase.HEALTHY
    assert evaluate(resumed, 1_329.0, _THRESHOLDS, _NO_RESTART).action is Action.NONE
    assert evaluate(resumed, 1_331.0, _THRESHOLDS, _NO_RESTART).action is Action.WARNING


def test_pause_requires_confirmed_resume_and_resets_silence_clock() -> None:
    paused = set_wait_state(
        initial_state(0.0),
        WaitState(waiting_for_approval=True),
        10.0,
        resume_confirmed=False,
    )
    still_paused = set_wait_state(
        paused,
        WaitState(),
        1_210.0,
        resume_confirmed=False,
    )

    assert still_paused.phase is MonitorPhase.PAUSED
    assert still_paused.waits.waiting_for_approval is True

    resumed = set_wait_state(
        still_paused,
        WaitState(),
        1_210.0,
        resume_confirmed=True,
    )
    assert resumed.phase is MonitorPhase.HEALTHY
    assert resumed.waits.paused is False
    assert evaluate(resumed, 1_329.0, _THRESHOLDS, _NO_RESTART).action is Action.NONE
    assert evaluate(resumed, 1_331.0, _THRESHOLDS, _NO_RESTART).action is Action.WARNING


def test_39m40s_systemic_sibling_silence_is_restart_limited() -> None:
    warned = evaluate(initial_state(0.0), 121.0, _THRESHOLDS, _NO_RESTART)
    systemic_gate = restart_gate(
        requested_by_user=True,
        capability_ready=True,
        restart_eligible_siblings=5,
    )

    limited = evaluate(warned.state, 2_380.0, _THRESHOLDS, systemic_gate)
    assert limited.action is Action.RESTART_LIMITED
    assert limited.action is not Action.AUTO_RESTART
    assert limited.state.phase is MonitorPhase.RESTART_ELIGIBLE
    assert evaluate(limited.state, 2_381.0, _THRESHOLDS, systemic_gate).action is Action.NONE


def test_one_verified_sibling_is_auto_restart_ready() -> None:
    gate = restart_gate(requested_by_user=True, capability_ready=True)
    warned = evaluate(initial_state(0.0), 601.0, _THRESHOLDS, gate)

    assert warned.action is Action.WARNING
    restarted = evaluate(warned.state, 601.0, _THRESHOLDS, gate)
    assert restarted.action is Action.AUTO_RESTART
    assert restarted.state.phase is MonitorPhase.RESTARTED


@pytest.mark.parametrize(
    "waits",
    [
        WaitState(child_terminal=True),
        WaitState(parent_complete=True),
    ],
)
def test_terminal_state_stops_watcher(waits: WaitState) -> None:
    terminal = set_wait_state(
        initial_state(0.0),
        waits,
        10.0,
        resume_confirmed=False,
    )

    result = evaluate(terminal, 2_500.0, _THRESHOLDS, _NO_RESTART)
    assert result.action is Action.STOP
    assert result.state.phase is MonitorPhase.TERMINAL
