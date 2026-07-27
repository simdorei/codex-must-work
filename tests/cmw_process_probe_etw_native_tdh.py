"""Decode the two audit providers from EVENT_RECORD payloads through TDH."""

from __future__ import annotations

import ctypes
from typing import TYPE_CHECKING, Final, Protocol, cast, final

from scripts.daemon_control_endpoint_identity import process_created_ns
from tests.cmw_process_probe_etw_native_types import (
    EventRecordPointer,
    Guid,
    PropertyDataDescriptor,
)
from tests.cmw_process_probe_events import AuditEvent, EventKind, ProcessIdentity

if TYPE_CHECKING:
    from collections.abc import Callable

_PROCESS_PROVIDER: Final = Guid.parse("22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716").key()
_WMI_PROVIDER: Final = Guid.parse("1418ef04-b0b4-4623-bf7e-d74ab47bbdaa").key()
_PROCESS_START_EVENT: Final = 1
_PROCESS_STOP_EVENT: Final = 2
_WMI_OPERATION_START_EVENTS: Final = frozenset({1, 11, 22, 23})
_FILETIME_UNIX_EPOCH_100NS: Final = 116_444_736_000_000_000
_FILETIME_UNIX_EPOCH_NS: Final = _FILETIME_UNIX_EPOCH_100NS * 100
_ERROR_SUCCESS: Final = 0


class EventDecodeError(RuntimeError):
    """A selected audit event is missing a required typed property."""

    def __init__(self, property_name: str) -> None:
        message = f"required TDH property missing: {property_name}"
        super().__init__(message)


class PropertyReader(Protocol):
    def integer(self, record: EventRecordPointer, name: str) -> int | None: ...


class IdentityReader(Protocol):
    def __call__(self, pid: int) -> int: ...


@final
class NativeTdhPropertyReader:
    """Read one named integral manifest property without payload offsets."""

    def __init__(self) -> None:
        tdh = ctypes.WinDLL("tdh", use_last_error=True)
        self._get_size = cast("Callable[..., int]", tdh.TdhGetPropertySize)
        self._get = cast("Callable[..., int]", tdh.TdhGetProperty)

    def integer(self, record: EventRecordPointer, name: str) -> int | None:
        property_name = ctypes.create_unicode_buffer(name)
        descriptor = PropertyDataDescriptor(
            ctypes.addressof(property_name),
            0xFFFFFFFF,
            0,
        )
        size = ctypes.c_uint32()
        result = self._get_size(
            record,
            0,
            None,
            1,
            ctypes.byref(descriptor),
            ctypes.byref(size),
        )
        if result != _ERROR_SUCCESS or size.value not in {1, 2, 4, 8}:
            return None
        payload = (ctypes.c_ubyte * size.value)()
        result = self._get(
            record,
            0,
            None,
            1,
            ctypes.byref(descriptor),
            size.value,
            ctypes.byref(payload),
        )
        if result != _ERROR_SUCCESS:
            return None
        return int.from_bytes(bytes(payload), "little", signed=False)


@final
class EventRecordDecoder:
    """Turn provider events into identity-bound audit events."""

    def __init__(
        self,
        properties: PropertyReader | None = None,
        identity_reader: IdentityReader = process_created_ns,
    ) -> None:
        self._properties = properties or NativeTdhPropertyReader()
        self._identity_reader = identity_reader
        self._identities: dict[int, ProcessIdentity] = {}

    def decode(self, record: EventRecordPointer) -> AuditEvent | None:
        header = record.contents.header
        provider = header.provider_id.key()
        timestamp_ns = _unix_ns_from_filetime(header.timestamp)
        event_id = int(header.descriptor.event_id)
        if provider == _PROCESS_PROVIDER:
            return self._decode_process(record, event_id, timestamp_ns)
        if provider == _WMI_PROVIDER and event_id in _WMI_OPERATION_START_EVENTS:
            return self._decode_wmi(record, timestamp_ns)
        return None

    def _decode_process(
        self,
        record: EventRecordPointer,
        event_id: int,
        timestamp_ns: int,
    ) -> AuditEvent | None:
        if event_id not in {_PROCESS_START_EVENT, _PROCESS_STOP_EVENT}:
            return None
        pid = self._required_integer(record, "ProcessID")
        created_100ns = self._properties.integer(record, "CreateTime")
        if event_id == _PROCESS_START_EVENT:
            return self._decode_process_start(record, pid, created_100ns, timestamp_ns)
        return self._decode_process_stop(pid, created_100ns, timestamp_ns)

    def _decode_process_start(
        self,
        record: EventRecordPointer,
        pid: int,
        created_100ns: int | None,
        timestamp_ns: int,
    ) -> AuditEvent | None:
        if created_100ns is None:
            property_name = "CreateTime"
            raise EventDecodeError(property_name)
        parent_pid = self._required_integer(record, "ParentProcessID")
        subject = ProcessIdentity(pid, created_100ns * 100)
        if not _identity_existed_at(subject, timestamp_ns):
            return None
        self._identities[pid] = subject
        parent = self._identity(parent_pid, timestamp_ns)
        if parent is None:
            return None
        return AuditEvent(
            EventKind.PROCESS_START,
            timestamp_ns,
            parent,
            subject=subject,
            parent=parent,
        )

    def _decode_process_stop(
        self,
        pid: int,
        created_100ns: int | None,
        timestamp_ns: int,
    ) -> AuditEvent | None:
        subject = self._identities.pop(pid, None)
        if subject is None and created_100ns is not None:
            subject = ProcessIdentity(pid, created_100ns * 100)
        if subject is None:
            return None
        return AuditEvent(
            EventKind.PROCESS_STOP,
            timestamp_ns,
            subject,
            subject=subject,
        )

    def _decode_wmi(
        self,
        record: EventRecordPointer,
        timestamp_ns: int,
    ) -> AuditEvent | None:
        client_pid = self._required_integer(record, "ClientProcessId")
        group_id = self._required_integer(record, "GroupOperationId")
        operation_id = self._required_integer(record, "OperationId")
        actor = self._identity(client_pid, timestamp_ns)
        if actor is None:
            return None
        return AuditEvent(
            EventKind.WMI_OPERATION,
            timestamp_ns,
            actor,
            operation_key=(group_id, operation_id),
        )

    def _required_integer(
        self,
        record: EventRecordPointer,
        name: str,
    ) -> int:
        value = self._properties.integer(record, name)
        if value is None:
            raise EventDecodeError(name)
        return value

    def _identity(
        self,
        pid: int,
        event_timestamp_ns: int,
    ) -> ProcessIdentity | None:
        known = self._identities.get(pid)
        if known is not None:
            return known if _identity_existed_at(known, event_timestamp_ns) else None
        try:
            created_ns = self._identity_reader(pid)
        except (OSError, RuntimeError):
            return None
        identity = ProcessIdentity(pid, created_ns)
        if not _identity_existed_at(identity, event_timestamp_ns):
            return None
        self._identities[pid] = identity
        return identity


def _unix_ns_from_filetime(value: int) -> int:
    return (value - _FILETIME_UNIX_EPOCH_100NS) * 100


def _identity_existed_at(
    identity: ProcessIdentity,
    event_timestamp_ns: int,
) -> bool:
    return identity.created_ns - _FILETIME_UNIX_EPOCH_NS <= event_timestamp_ns
