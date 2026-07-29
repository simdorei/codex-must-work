"""Project watcher lifecycle diagnostics onto optional remote notifications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never, final

from scripts.monitor_diagnostics import DiagnosticCode
from scripts.notifications import (
    LifecycleNotification,
    NotificationDeliveryError,
    NotificationKind,
    NotificationSink,
    NotificationSubject,
    NotificationSubjectKind,
)
from scripts.watcher_diagnostics import (
    TargetDiagnostic,
    append_target_diagnostic,
    append_target_diagnostic_once,
    lifecycle_event_exists,
    lifecycle_event_id,
    notification_failure_event_id,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scripts.monitor_target import MonitorTarget, RuntimeTarget
    from scripts.stall_detector import SilenceState


@dataclass(frozen=True, slots=True)
class _PendingNotification:
    target: RuntimeTarget
    diagnostic: TargetDiagnostic
    event: LifecycleNotification


@final
class WatcherNotificationRecorder:
    """Record lifecycle diagnostics and deliver each new transition once."""

    def __init__(self, root: Path, sink: NotificationSink) -> None:
        """Bind one watcher root to its optional remote sink."""
        self._root = root
        self._sink = sink
        self._pending: list[_PendingNotification] = []

    def warning_was_emitted(
        self,
        target: RuntimeTarget,
        monitor: MonitorTarget,
        state: SilenceState,
    ) -> bool:
        """Recover the warned state from retained diagnostics after daemon restart."""
        if state.warning_emitted:
            return True
        event_id = lifecycle_event_id(
            DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE,
            target,
            monitor,
        )
        return lifecycle_event_exists(self._root, event_id)

    def record_recovery(
        self,
        target: RuntimeTarget,
        monitor: MonitorTarget,
        occurred_at: datetime,
    ) -> None:
        """Record and deliver progress returning after one warned silence epoch."""
        self.record(
            target,
            TargetDiagnostic(
                occurred_at,
                DiagnosticCode.PROGRESS_RECOVERED,
                monitor,
                event_id=lifecycle_event_id(
                    DiagnosticCode.PROGRESS_RECOVERED,
                    target,
                    monitor,
                ),
            ),
        )

    def record(
        self,
        target: RuntimeTarget,
        diagnostic: TargetDiagnostic,
    ) -> None:
        """Record one diagnostic and deliver only supported new lifecycle events."""
        appended = append_target_diagnostic_once(self._root, target, diagnostic)
        kind = _notification_kind(diagnostic.code)
        if not appended or kind is None or diagnostic.event_id is None:
            return
        self._pending.append(
            _PendingNotification(
                target,
                diagnostic,
                LifecycleNotification(
                    diagnostic.event_id,
                    target.session_id,
                    kind,
                    _notification_subject(diagnostic),
                    diagnostic.elapsed_ms,
                    _notification_threshold_ms(target, kind),
                ),
            )
        )

    def flush(self) -> None:
        """Deliver queued events after runtime state locks have been released."""
        pending = tuple(self._pending)
        self._pending.clear()
        for item in pending:
            try:
                self._sink.notify(item.event)
            except NotificationDeliveryError:
                append_target_diagnostic(
                    self._root,
                    item.target,
                    TargetDiagnostic(
                        item.diagnostic.occurred_at,
                        DiagnosticCode.DISCORD_NOTIFICATION_FAILED,
                        item.diagnostic.target,
                        event_id=notification_failure_event_id(item.event.event_id),
                    ),
                )


def _notification_kind(code: DiagnosticCode) -> NotificationKind | None:
    return {
        DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE: NotificationKind.BOTTLENECK_SUSPECTED,
        DiagnosticCode.BOTTLENECK_CRITICAL: NotificationKind.BOTTLENECK_CRITICAL,
        DiagnosticCode.PROGRESS_RECOVERED: NotificationKind.PROGRESS_RECOVERED,
        DiagnosticCode.WATCHER_COMPLETED: NotificationKind.COMPLETED,
    }.get(code)


def _notification_subject(diagnostic: TargetDiagnostic) -> NotificationSubject:
    monitor = diagnostic.target
    if monitor is None:
        return NotificationSubject(NotificationSubjectKind.TASK)
    if monitor.target_id is None:
        return NotificationSubject(NotificationSubjectKind.MAIN_AGENT)
    return NotificationSubject(
        NotificationSubjectKind.SUBAGENT,
        target_id=monitor.target_id,
    )


def _notification_threshold_ms(
    target: RuntimeTarget,
    kind: NotificationKind,
) -> int | None:
    """Report the configured transition boundary, not scheduler tick drift."""
    match kind:
        case NotificationKind.BOTTLENECK_SUSPECTED:
            return max(0, int(target.thresholds.warning * 1000))
        case NotificationKind.BOTTLENECK_CRITICAL:
            return max(0, int(target.thresholds.critical * 1000))
        case NotificationKind.PROGRESS_RECOVERED | NotificationKind.COMPLETED:
            return None
        case _:
            assert_never(kind)
