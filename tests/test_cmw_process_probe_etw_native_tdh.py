from __future__ import annotations

import ctypes
from typing import final

import pytest

from tests.cmw_process_probe_etw_native_tdh import (
    EventDecodeError,
    EventRecordDecoder,
)
from tests.cmw_process_probe_etw_native_types import (
    EventDescriptor,
    EventHeader,
    EventRecord,
    EventRecordPointer,
    Guid,
)
from tests.cmw_process_probe_events import EventKind, ProcessIdentity

_PROCESS_PROVIDER = "22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716"
_WMI_PROVIDER = "1418ef04-b0b4-4623-bf7e-d74ab47bbdaa"
_UNIX_EVENT_FILETIME = 116_444_736_000_000_010


@final
class FakeProperties:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values

    def integer(self, record: EventRecordPointer, name: str) -> int | None:
        _ = record
        return self.values.get(name)


def _record(provider: str, event_id: int) -> EventRecordPointer:
    header = EventHeader(
        timestamp=_UNIX_EVENT_FILETIME,
        provider_id=Guid.parse(provider),
        descriptor=EventDescriptor(event_id=event_id),
    )
    return ctypes.pointer(EventRecord(header=header))


def test_tdh_process_start_contains_pid_creation_and_parent_identity() -> None:
    # Given
    properties = FakeProperties({"ProcessID": 20, "CreateTime": 30, "ParentProcessID": 10})
    decoder = EventRecordDecoder(properties, identity_reader=lambda pid: pid * 1_000)

    # When
    event = decoder.decode(_record(_PROCESS_PROVIDER, 1))

    # Then
    assert event is not None
    assert event.kind is EventKind.PROCESS_START
    assert event.timestamp_ns == 1_000
    assert event.subject == ProcessIdentity(20, 3_000)
    assert event.parent == ProcessIdentity(10, 10_000)


def test_tdh_process_stop_reuses_exact_creation_identity() -> None:
    # Given
    properties = FakeProperties({"ProcessID": 20, "CreateTime": 30, "ParentProcessID": 10})
    decoder = EventRecordDecoder(properties, identity_reader=lambda pid: pid * 1_000)
    _ = decoder.decode(_record(_PROCESS_PROVIDER, 1))

    # When
    properties.values = {"ProcessID": 20}
    event = decoder.decode(_record(_PROCESS_PROVIDER, 2))

    # Then
    assert event is not None
    assert event.kind is EventKind.PROCESS_STOP
    assert event.subject == ProcessIdentity(20, 3_000)


def test_tdh_wmi_operation_contains_client_pid_and_creation_identity() -> None:
    # Given
    decoder = EventRecordDecoder(
        FakeProperties(
            {
                "ClientProcessId": 77,
                "GroupOperationId": 8,
                "OperationId": 9,
            }
        ),
        identity_reader=lambda pid: pid * 1_000,
    )

    # When
    event = decoder.decode(_record(_WMI_PROVIDER, 11))

    # Then
    assert event is not None
    assert event.kind is EventKind.WMI_OPERATION
    assert event.actor == ProcessIdentity(77, 77_000)
    assert event.operation_key == (8, 9)


def test_tdh_wmi_rejects_identity_created_after_event_timestamp() -> None:
    # Given
    filetime_epoch_ns = 11_644_473_600_000_000_000
    decoder = EventRecordDecoder(
        FakeProperties(
            {
                "ClientProcessId": 77,
                "GroupOperationId": 8,
                "OperationId": 9,
            }
        ),
        identity_reader=lambda pid: filetime_epoch_ns + pid + 1_923,
    )

    # When
    event = decoder.decode(_record(_WMI_PROVIDER, 11))

    # Then
    assert event is None


def test_tdh_selected_wmi_event_requires_operation_identity() -> None:
    # Given
    decoder = EventRecordDecoder(
        FakeProperties({"ClientProcessId": 77, "GroupOperationId": 8}),
        identity_reader=lambda pid: pid * 1_000,
    )

    # When / Then
    with pytest.raises(EventDecodeError, match="OperationId"):
        _ = decoder.decode(_record(_WMI_PROVIDER, 11))
