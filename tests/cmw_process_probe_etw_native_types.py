"""ctypes declarations matching the Windows ETW consumer ABI."""

from __future__ import annotations

import ctypes
import uuid
from typing import Protocol, final


@final
class Guid(ctypes.Structure):
    data1: int = 0
    data2: int = 0
    data3: int = 0
    data4: ctypes.Array[ctypes.c_ubyte] = (ctypes.c_ubyte * 8)()
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> Guid:
        raw = uuid.UUID(value)
        return cls(
            raw.time_low,
            raw.time_mid,
            raw.time_hi_version,
            (ctypes.c_ubyte * 8)(
                raw.clock_seq_hi_variant,
                raw.clock_seq_low,
                *raw.node.to_bytes(6, "big"),
            ),
        )

    def key(self) -> bytes:
        return ctypes.string_at(ctypes.byref(self), ctypes.sizeof(self))


@final
class WnodeHeader(ctypes.Structure):
    buffer_size: int = 0
    client_context: int = 0
    flags: int = 0
    _fields_ = [
        ("buffer_size", ctypes.c_uint32),
        ("provider_id", ctypes.c_uint32),
        ("historical_context", ctypes.c_uint64),
        ("timestamp", ctypes.c_int64),
        ("guid", Guid),
        ("client_context", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


@final
class EventTraceProperties(ctypes.Structure):
    wnode: WnodeHeader = WnodeHeader()
    events_lost: int = 0
    log_buffers_lost: int = 0
    realtime_buffers_lost: int = 0
    buffer_size: int = 0
    minimum_buffers: int = 0
    maximum_buffers: int = 0
    log_file_mode: int = 0
    flush_timer: int = 0
    logger_name_offset: int = 0
    _fields_ = [
        ("wnode", WnodeHeader),
        ("buffer_size", ctypes.c_uint32),
        ("minimum_buffers", ctypes.c_uint32),
        ("maximum_buffers", ctypes.c_uint32),
        ("maximum_file_size", ctypes.c_uint32),
        ("log_file_mode", ctypes.c_uint32),
        ("flush_timer", ctypes.c_uint32),
        ("enable_flags", ctypes.c_uint32),
        ("age_limit", ctypes.c_int32),
        ("number_of_buffers", ctypes.c_uint32),
        ("free_buffers", ctypes.c_uint32),
        ("events_lost", ctypes.c_uint32),
        ("buffers_written", ctypes.c_uint32),
        ("log_buffers_lost", ctypes.c_uint32),
        ("realtime_buffers_lost", ctypes.c_uint32),
        ("logger_thread_id", ctypes.c_void_p),
        ("log_file_name_offset", ctypes.c_uint32),
        ("logger_name_offset", ctypes.c_uint32),
    ]


@final
class EventDescriptor(ctypes.Structure):
    event_id: int = 0
    version: int = 0
    _fields_ = [
        ("event_id", ctypes.c_uint16),
        ("version", ctypes.c_ubyte),
        ("channel", ctypes.c_ubyte),
        ("level", ctypes.c_ubyte),
        ("opcode", ctypes.c_ubyte),
        ("task", ctypes.c_uint16),
        ("keyword", ctypes.c_uint64),
    ]


@final
class EventHeader(ctypes.Structure):
    timestamp: int = 0
    provider_id: Guid = Guid()
    descriptor: EventDescriptor = EventDescriptor()
    _fields_ = [
        ("size", ctypes.c_uint16),
        ("header_type", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("event_property", ctypes.c_uint16),
        ("thread_id", ctypes.c_uint32),
        ("process_id", ctypes.c_uint32),
        ("timestamp", ctypes.c_int64),
        ("provider_id", Guid),
        ("descriptor", EventDescriptor),
        ("processor_time", ctypes.c_uint64),
        ("activity_id", Guid),
    ]


@final
class EtwBufferContext(ctypes.Structure):
    _fields_ = [
        ("processor_index", ctypes.c_uint16),
        ("logger_id", ctypes.c_uint16),
    ]


@final
class EventRecord(ctypes.Structure):
    header: EventHeader = EventHeader()
    _fields_ = [
        ("header", EventHeader),
        ("buffer_context", EtwBufferContext),
        ("extended_data_count", ctypes.c_uint16),
        ("user_data_length", ctypes.c_uint16),
        ("extended_data", ctypes.c_void_p),
        ("user_data", ctypes.c_void_p),
        ("user_context", ctypes.c_void_p),
    ]


class EventRecordPointer(Protocol):
    @property
    def contents(self) -> EventRecord: ...


EventRecordCallback = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EventRecord))
BufferCallback = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)


@final
class EventTraceHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint16),
        ("field_type_flags", ctypes.c_uint16),
        ("version", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("process_id", ctypes.c_uint32),
        ("timestamp", ctypes.c_int64),
        ("guid", Guid),
        ("processor_time", ctypes.c_uint64),
    ]


@final
class EventTrace(ctypes.Structure):
    _fields_ = [
        ("header", EventTraceHeader),
        ("instance_id", ctypes.c_uint32),
        ("parent_instance_id", ctypes.c_uint32),
        ("parent_guid", Guid),
        ("mof_data", ctypes.c_void_p),
        ("mof_length", ctypes.c_uint32),
        ("buffer_context", ctypes.c_uint32),
    ]


@final
class SystemTime(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint16)
        for name in (
            "year",
            "month",
            "day_of_week",
            "day",
            "hour",
            "minute",
            "second",
            "milliseconds",
        )
    ]


@final
class TimeZoneInformation(ctypes.Structure):
    _fields_ = [
        ("bias", ctypes.c_int32),
        ("standard_name", ctypes.c_wchar * 32),
        ("standard_date", SystemTime),
        ("standard_bias", ctypes.c_int32),
        ("daylight_name", ctypes.c_wchar * 32),
        ("daylight_date", SystemTime),
        ("daylight_bias", ctypes.c_int32),
    ]


@final
class TraceLogfileHeader(ctypes.Structure):
    _fields_ = [
        ("buffer_size", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("provider_version", ctypes.c_uint32),
        ("number_of_processors", ctypes.c_uint32),
        ("end_time", ctypes.c_int64),
        ("timer_resolution", ctypes.c_uint32),
        ("maximum_file_size", ctypes.c_uint32),
        ("log_file_mode", ctypes.c_uint32),
        ("buffers_written", ctypes.c_uint32),
        ("log_instance_guid", Guid),
        ("logger_name", ctypes.c_wchar_p),
        ("log_file_name", ctypes.c_wchar_p),
        ("time_zone", TimeZoneInformation),
        ("boot_time", ctypes.c_int64),
        ("perf_freq", ctypes.c_int64),
        ("start_time", ctypes.c_int64),
        ("reserved_flags", ctypes.c_uint32),
        ("buffers_lost", ctypes.c_uint32),
    ]


@final
class EventTraceLogfile(ctypes.Structure):
    _fields_ = [
        ("log_file_name", ctypes.c_wchar_p),
        ("logger_name", ctypes.c_wchar_p),
        ("current_time", ctypes.c_int64),
        ("buffers_read", ctypes.c_uint32),
        ("process_trace_mode", ctypes.c_uint32),
        ("current_event", EventTrace),
        ("logfile_header", TraceLogfileHeader),
        ("buffer_callback", BufferCallback),
        ("buffer_size", ctypes.c_uint32),
        ("filled", ctypes.c_uint32),
        ("events_lost", ctypes.c_uint32),
        ("event_record_callback", EventRecordCallback),
        ("is_kernel_trace", ctypes.c_uint32),
        ("context", ctypes.c_void_p),
    ]


@final
class PropertyDataDescriptor(ctypes.Structure):
    _fields_ = [
        ("property_name", ctypes.c_uint64),
        ("array_index", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


@final
class EnableTraceParameters(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("enable_property", ctypes.c_uint32),
        ("control_flags", ctypes.c_uint32),
        ("source_id", Guid),
        ("enable_filter_desc", ctypes.c_void_p),
        ("filter_desc_count", ctypes.c_uint32),
    ]
