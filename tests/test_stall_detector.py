from __future__ import annotations

from scripts.stall_detector import (
    Action,
    MonitorPhase,
    ProgressKind,
    Thresholds,
    WaitState,
    evaluate,
    initial_state,
    record_progress,
    set_wait_state,
)


def test_warning_then_critical_are_each_emitted_once() -> None:
    thresholds = Thresholds(warning=300.0, critical=600.0)
    state = record_progress(initial_state(0.0), ProgressKind.ITEM, 1.0)

    warning = evaluate(state, 301.0, thresholds)
    before_critical = evaluate(warning.state, 599.0, thresholds)
    critical = evaluate(before_critical.state, 601.0, thresholds)
    repeated = evaluate(critical.state, 900.0, thresholds)

    assert warning.action is Action.WARNING
    assert before_critical.action is Action.NONE
    assert critical.action is Action.CRITICAL
    assert critical.state.phase is MonitorPhase.CRITICAL
    assert repeated.action is Action.NONE


def test_real_progress_rearms_warning_and_critical() -> None:
    thresholds = Thresholds(warning=300.0, critical=600.0)
    warned = evaluate(initial_state(0.0), 300.0, thresholds)
    critical = evaluate(warned.state, 600.0, thresholds)

    progressed = record_progress(critical.state, ProgressKind.DELTA, 601.0)
    next_warning = evaluate(progressed, 901.0, thresholds)
    next_critical = evaluate(next_warning.state, 1_201.0, thresholds)

    assert next_warning.action is Action.WARNING
    assert next_critical.action is Action.CRITICAL


def test_wait_resume_does_not_rearm_without_observable_progress() -> None:
    thresholds = Thresholds(warning=300.0, critical=600.0)
    paused = set_wait_state(
        initial_state(0.0),
        WaitState(open_tool_count=1),
        1.0,
        resume_confirmed=False,
    )

    resumed = set_wait_state(
        paused,
        WaitState(),
        10.0,
        resume_confirmed=True,
    )
    evaluation = evaluate(resumed, 301.0, thresholds)

    assert resumed.silence_sequence == paused.silence_sequence
    assert evaluation.action is Action.WARNING
