"""Privacy-bounded lifecycle notification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Protocol, assert_never, final, override


@unique
class NotificationKind(StrEnum):
    """Lifecycle transitions that may leave the local machine."""

    BOTTLENECK_SUSPECTED = "bottleneck_suspected"
    BOTTLENECK_CRITICAL = "bottleneck_critical"
    PROGRESS_RECOVERED = "progress_recovered"
    COMPLETED = "completed"


@unique
class NotificationSubjectKind(StrEnum):
    """Local target categories disclosed by a lifecycle notification."""

    TASK = "task"
    MAIN_AGENT = "main_agent"
    SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class NotificationSubject:
    """Identify a task, main agent, or local subagent without remote content."""

    kind: NotificationSubjectKind
    target_id: str | None = None

    def __post_init__(self) -> None:
        """Reject subject shapes that could blur main and subagent alerts."""
        match self.kind:
            case NotificationSubjectKind.TASK | NotificationSubjectKind.MAIN_AGENT:
                if self.target_id is not None:
                    raise NotificationSubjectError(self.kind)
                return
            case NotificationSubjectKind.SUBAGENT:
                if self.target_id is None or not self.target_id:
                    raise NotificationSubjectError(self.kind)
                return
            case _:
                assert_never(self.kind)


@dataclass(frozen=True, slots=True)
class NotificationSubjectError(ValueError):
    """Report one impossible subject shape without exposing an opaque ID."""

    kind: NotificationSubjectKind

    @override
    def __str__(self) -> str:
        return f"invalid notification subject: {self.kind.value}"


@dataclass(frozen=True, slots=True)
class LifecycleNotification:
    """One content-free lifecycle transition for an opted-in task."""

    event_id: str
    session_id: str
    kind: NotificationKind
    subject: NotificationSubject
    elapsed_ms: int | None = None
    threshold_ms: int | None = None


class NotificationDeliveryError(RuntimeError):
    """Expose only a public-safe delivery reason."""

    def __init__(self, reason_code: str) -> None:
        """Retain one stable code without storing remote response content."""
        super().__init__(reason_code)
        self.reason_code: str = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code


class NotificationSink(Protocol):
    """Deliver lifecycle transitions without owning watcher state."""

    def notify(self, event: LifecycleNotification) -> None:
        """Deliver one lifecycle transition."""
        ...


@final
class NullNotificationSink:
    """Disable remote delivery without adding a process or timer."""

    def notify(self, event: LifecycleNotification) -> None:
        """Intentionally ignore an event when no webhook is configured."""
        _ = event
