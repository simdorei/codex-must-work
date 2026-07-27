"""Identity-bound audit events and owned-operation counting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    created_ns: int


class EventKind(StrEnum):
    PROCESS_START = "process_start"
    PROCESS_STOP = "process_stop"
    WMI_OPERATION = "wmi_operation"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    kind: EventKind
    timestamp_ns: int
    actor: ProcessIdentity
    subject: ProcessIdentity | None = None
    parent: ProcessIdentity | None = None
    operation_key: tuple[int, int] | None = None


type _EventKey = tuple[
    EventKind,
    int,
    ProcessIdentity,
    ProcessIdentity | None,
    ProcessIdentity | None,
    tuple[int, int] | None,
]


def owned_event_counts(
    root: ProcessIdentity,
    events: tuple[AuditEvent, ...],
    *,
    bootstrap_boundary_ns: int,
    coverage_end_ns: int,
) -> tuple[int, int]:
    """Count exact owned process starts and logical WMI operations."""
    owned = {root}
    starts = 0
    wmi = 0
    seen: set[_EventKey] = set()
    wmi_seen: set[tuple[ProcessIdentity, tuple[int, int]]] = set()
    for event in sorted(events, key=lambda item: item.timestamp_ns):
        if not bootstrap_boundary_ns <= event.timestamp_ns <= coverage_end_ns:
            continue
        event_key = _event_key(event)
        if event_key in seen:
            continue
        seen.add(event_key)
        if event.kind is EventKind.PROCESS_START:
            if event.parent in owned and event.subject is not None:
                owned.add(event.subject)
                starts += 1
            continue
        if event.kind is EventKind.WMI_OPERATION:
            wmi += int(_is_new_owned_wmi(event, owned, wmi_seen))
            continue
        if event.kind is EventKind.PROCESS_STOP:
            if event.subject is not None:
                owned.discard(event.subject)
            continue
        assert_never(event.kind)
    return starts, wmi


def _event_key(event: AuditEvent) -> _EventKey:
    return (
        event.kind,
        event.timestamp_ns,
        event.actor,
        event.subject,
        event.parent,
        event.operation_key,
    )


def _is_new_owned_wmi(
    event: AuditEvent,
    owned: set[ProcessIdentity],
    seen: set[tuple[ProcessIdentity, tuple[int, int]]],
) -> bool:
    if event.actor not in owned:
        return False
    if event.operation_key is None:
        return True
    operation = (event.actor, event.operation_key)
    if operation in seen:
        return False
    seen.add(operation)
    return True
