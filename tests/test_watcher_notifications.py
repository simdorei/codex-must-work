from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from scripts.monitor_diagnostics import DiagnosticCode
from scripts.notifications import (
    LifecycleNotification,
    NotificationDeliveryError,
    NotificationKind,
    NotificationSubjectKind,
)
from scripts.state import StateDocument, load_state, save_state
from scripts.watcher_commit import commit_runtime_snapshot as actual_commit
from scripts.watcher_engine import WatcherEngine
from tests.watcher_fixture import WALL_TIME, append_progress, diagnostic_codes, state

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.monitor_target import RuntimeTarget
    from scripts.watcher_batch import TargetBatch
    from scripts.watcher_commit import TargetProcessor


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[LifecycleNotification] = []

    def notify(self, event: LifecycleNotification) -> None:
        self.events.append(event)


def test_watcher_notifies_warning_recovery_and_completion_once(tmp_path: Path) -> None:
    root, rollout, runtime_file = state(tmp_path)
    sink = _RecordingSink()
    engine = WatcherEngine(root, notification_sink=sink)

    assert engine.tick(0.0, WALL_TIME) is True
    append_progress(rollout, "child-1")
    assert engine.tick(1.0, WALL_TIME) is True
    assert engine.tick(91.0, WALL_TIME) is True
    assert engine.tick(91.5, WALL_TIME) is True

    append_progress(rollout, "child-1")
    assert engine.tick(92.0, WALL_TIME) is True
    assert engine.tick(92.5, WALL_TIME) is True

    document = load_state(root, runtime_file)
    values = dict(document.values)
    children = values["children"]
    assert isinstance(children, dict)
    child = children["child-1"]
    assert isinstance(child, dict)
    child["status"] = "terminal"
    values["children"] = children
    values["parent_complete"] = True
    save_state(root, runtime_file, StateDocument(values=values))

    assert engine.tick(100.0, WALL_TIME) is False
    assert WatcherEngine(root, notification_sink=sink).tick(101.0, WALL_TIME) is False

    assert [event.kind for event in sink.events] == [
        NotificationKind.BOTTLENECK_SUSPECTED,
        NotificationKind.PROGRESS_RECOVERED,
        NotificationKind.COMPLETED,
    ]
    assert len({event.event_id for event in sink.events}) == 3
    assert [event.subject.kind for event in sink.events] == [
        NotificationSubjectKind.SUBAGENT,
        NotificationSubjectKind.SUBAGENT,
        NotificationSubjectKind.TASK,
    ]
    assert sink.events[0].subject.target_id == "child-1"
    assert sink.events[1].subject.target_id == "child-1"
    assert sink.events[2].subject.target_id is None
    codes = diagnostic_codes(root)
    assert codes.count(DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value) == 1
    assert codes.count(DiagnosticCode.PROGRESS_RECOVERED.value) == 1
    assert codes.count(DiagnosticCode.WATCHER_COMPLETED.value) == 1


def test_watcher_notifies_critical_without_requesting_restart(tmp_path: Path) -> None:
    root, _, runtime_file = state(tmp_path, children=0, parent=True)

    sink = _RecordingSink()
    engine = WatcherEngine(root, notification_sink=sink)

    assert engine.tick(0.0, WALL_TIME) is True
    assert engine.tick(90.0, WALL_TIME) is True
    assert engine.tick(301.0, WALL_TIME) is True

    assert [event.kind for event in sink.events] == [
        NotificationKind.BOTTLENECK_SUSPECTED,
        NotificationKind.BOTTLENECK_CRITICAL,
    ]
    assert sink.events[-1].elapsed_ms == 301_000
    assert sink.events[-1].threshold_ms == 300_000
    codes = diagnostic_codes(root)
    assert codes.count(DiagnosticCode.BOTTLENECK_CRITICAL.value) == 1
    assert "restart_request" not in load_state(root, runtime_file).values


def test_recovery_is_emitted_after_daemon_restart(tmp_path: Path) -> None:
    root, rollout, _ = state(tmp_path)
    first_sink = _RecordingSink()
    first_engine = WatcherEngine(root, notification_sink=first_sink)
    assert first_engine.tick(0.0, WALL_TIME) is True
    append_progress(rollout, "child-1")
    assert first_engine.tick(1.0, WALL_TIME) is True
    assert first_engine.tick(91.0, WALL_TIME) is True

    second_sink = _RecordingSink()
    second_engine = WatcherEngine(root, notification_sink=second_sink)
    append_progress(rollout, "child-1")
    assert second_engine.tick(92.0, WALL_TIME) is True

    assert [event.kind for event in first_sink.events] == [NotificationKind.BOTTLENECK_SUSPECTED]
    assert [event.kind for event in second_sink.events] == [NotificationKind.PROGRESS_RECOVERED]


def test_daemon_restart_preserves_elapsed_silence_for_critical_notification(
    tmp_path: Path,
) -> None:
    root, _, runtime_file = state(tmp_path, children=0, parent=True)
    document = load_state(root, runtime_file)
    values = dict(document.values)
    parent = values["parent"]
    assert isinstance(parent, dict)
    parent["silence_started_at"] = WALL_TIME.isoformat()
    values["parent"] = parent
    save_state(root, runtime_file, StateDocument(values=values))

    first_sink = _RecordingSink()
    first_engine = WatcherEngine(root, notification_sink=first_sink)
    assert first_engine.tick(0.0, WALL_TIME) is True
    assert first_engine.tick(90.0, WALL_TIME + timedelta(seconds=90)) is True

    second_sink = _RecordingSink()
    second_engine = WatcherEngine(root, notification_sink=second_sink)
    assert second_engine.tick(0.0, WALL_TIME + timedelta(seconds=301)) is True

    assert [event.kind for event in first_sink.events] == [NotificationKind.BOTTLENECK_SUSPECTED]
    assert [event.kind for event in second_sink.events] == [NotificationKind.BOTTLENECK_CRITICAL]
    assert second_sink.events[0].elapsed_ms == 301_000


def test_observed_progress_persists_new_silence_anchor_for_later_restart(
    tmp_path: Path,
) -> None:
    root, rollout, runtime_file = state(tmp_path)
    document = load_state(root, runtime_file)
    values = dict(document.values)
    children = values["children"]
    assert isinstance(children, dict)
    child = children["child-1"]
    assert isinstance(child, dict)
    child["silence_started_at"] = WALL_TIME.isoformat()
    children["child-1"] = child
    values["children"] = children
    save_state(root, runtime_file, StateDocument(values=values))
    engine = WatcherEngine(root)
    assert engine.tick(0.0, WALL_TIME) is True

    append_progress(rollout, "child-1")
    progressed_at = WALL_TIME + timedelta(seconds=10)
    assert engine.tick(10.0, progressed_at) is True

    persisted = load_state(root, runtime_file).values
    persisted_children = persisted["children"]
    assert isinstance(persisted_children, dict)
    persisted_child = persisted_children["child-1"]
    assert isinstance(persisted_child, dict)
    assert persisted_child["silence_started_at"] == progressed_at.isoformat()


def test_local_heartbeat_never_becomes_a_remote_notification(tmp_path: Path) -> None:
    root, rollout, _ = state(tmp_path)
    sink = _RecordingSink()
    engine = WatcherEngine(root, notification_sink=sink)

    assert engine.tick(0.0, WALL_TIME) is True
    append_progress(rollout, "child-1")
    assert engine.tick(1.0, WALL_TIME) is True
    assert engine.tick(91.0, WALL_TIME) is True
    assert engine.tick(181.0, WALL_TIME) is True

    assert DiagnosticCode.HEARTBEAT_ACTIVE.value in diagnostic_codes(root)
    assert [event.kind for event in sink.events] == [NotificationKind.BOTTLENECK_SUSPECTED]


def test_delivery_failure_is_visible_without_stopping_monitoring(tmp_path: Path) -> None:
    root, rollout, _ = state(tmp_path)

    class _FailingSink:
        def notify(self, event: LifecycleNotification) -> None:
            _ = event
            reason = "test_delivery_failed"
            raise NotificationDeliveryError(reason)

    engine = WatcherEngine(root, notification_sink=_FailingSink())
    assert engine.tick(0.0, WALL_TIME) is True
    append_progress(rollout, "child-1")
    assert engine.tick(1.0, WALL_TIME) is True

    assert engine.tick(91.0, WALL_TIME) is True

    codes = diagnostic_codes(root)
    assert codes.count(DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE.value) == 1
    assert codes.count(DiagnosticCode.DISCORD_NOTIFICATION_FAILED.value) == 1


def test_delivery_runs_after_runtime_commit_releases_its_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, rollout, _ = state(tmp_path)
    inside_commit = False

    def commit(
        root_path: Path,
        snapshot: RuntimeTarget,
        batch: TargetBatch | None,
        processor: TargetProcessor,
    ) -> bool:
        nonlocal inside_commit
        inside_commit = True
        try:
            return actual_commit(root_path, snapshot, batch, processor)
        finally:
            inside_commit = False

    class _LockCheckingSink:
        def notify(self, event: LifecycleNotification) -> None:
            _ = event
            assert inside_commit is False

    monkeypatch.setattr("scripts.watcher_runtime.commit_runtime_snapshot", commit)
    engine = WatcherEngine(root, notification_sink=_LockCheckingSink())
    assert engine.tick(0.0, WALL_TIME) is True
    append_progress(rollout, "child-1")
    assert engine.tick(1.0, WALL_TIME) is True

    assert engine.tick(91.0, WALL_TIME) is True
