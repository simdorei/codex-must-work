"""Parse identity-bearing rollout and Discord audit records."""
# ruff: noqa: EM101, EM102, TC001, TC003

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NewType, Protocol

from scripts.state_io import JsonValue
from tests.live_discord_e2e_audit_authority import bind_discord_documents
from tests.live_discord_e2e_audit_native import (
    flatten_discord_log_line,
    flatten_discord_message,
    flatten_rollout,
    flatten_rollout_record,
)

RecordId = NewType("RecordId", str)
ItemId = NewType("ItemId", str)

_REQUIRED_ID_EVENTS: Final = frozenset(
    {
        "user_message",
        "task_started",
        "task_complete",
        "assistant_output",
        "assistant_final",
        "message_create",
    }
)


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One public, identity-bearing event from one audit surface."""

    surface: str
    event: str
    event_id: RecordId
    timestamp: str
    session_id: str = ""
    thread_id: str = ""
    codex_thread_id: str = ""
    turn_id: str = ""
    author_id: str = ""
    author_role: str = ""
    item_id: ItemId = ItemId("")
    message_id: str = ""
    text: str = ""
    schema: str = ""

    @property
    def identity(self) -> tuple[str, RecordId]:
        return self.surface, self.event_id


class RecordError(RuntimeError):
    """Reject malformed or ambiguous audit records."""


def load_records(path: Path, expected_surface: str) -> tuple[AuditRecord, ...]:
    """Load newline-delimited JSON without interpreting non-JSON text."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RecordError("record_read_failed") from error
    decoded_records: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded = decode_json(line)
        except json.JSONDecodeError as error:
            raise RecordError(f"malformed_json:{line_number}") from error
        if not isinstance(decoded, dict):
            raise RecordError(f"record_not_object:{line_number}")
        decoded_records.append(decoded)
    flattened = (
        flatten_rollout(decoded_records) if expected_surface == "rollout" else decoded_records
    )
    records = [
        parse_record(values, expected_surface, index)
        for index, values in enumerate(flattened, start=1)
    ]
    return deduplicate(records)


def decode_json(value: str) -> JsonValue:
    """Decode JSON through a type-preserving boundary."""
    return _LOAD_JSON(value)


def load_discord_records(path: Path) -> tuple[AuditRecord, ...]:
    """Load bound native Discord API rows or diagnostic plaintext records."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise RecordError("record_read_failed") from error
    documents: list[dict[str, JsonValue]] = []
    diagnostics: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if line.lstrip().startswith("{"):
            try:
                decoded = decode_json(line)
            except json.JSONDecodeError as error:
                raise RecordError(f"malformed_json:{line_number}") from error
            if not isinstance(decoded, dict):
                raise RecordError(f"record_not_object:{line_number}")
            documents.append(decoded)
            continue
        diagnostic = flatten_discord_log_line(line, line_number)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    flattened = (*bind_discord_documents(documents), *diagnostics)
    records = [
        parse_record(values, "discord", line_number)
        for line_number, values in enumerate(flattened, start=1)
    ]
    if not records:
        raise RecordError("discord_records_missing")
    return deduplicate(records)


def parse_record(
    values: dict[str, JsonValue],
    expected_surface: str,
    line_number: int = 0,
) -> AuditRecord:
    """Parse one flat audit row or one native Codex rollout event."""
    if "event" in values:
        flat = values
    elif expected_surface == "discord" and "author" in values and "id" in values:
        flat = flatten_discord_message(values)
    else:
        flat = flatten_rollout_record(values)
    surface = _text(flat, "surface") or expected_surface
    event = _text(flat, "event")
    event_id = _text(flat, "event_id")
    if surface != expected_surface:
        raise RecordError(f"surface_mismatch:{line_number}")
    if not event:
        raise RecordError(f"event_missing:{line_number}")
    if event in _REQUIRED_ID_EVENTS and not event_id:
        raise RecordError(f"event_identity_missing:{line_number}")
    return AuditRecord(
        surface=surface,
        event=event,
        event_id=RecordId(event_id or f"ignored-{line_number}"),
        timestamp=_text(flat, "timestamp"),
        session_id=_text(flat, "session_id"),
        thread_id=_text(flat, "thread_id"),
        codex_thread_id=_text(flat, "codex_thread_id"),
        turn_id=_text(flat, "turn_id"),
        author_id=_text(flat, "author_id"),
        author_role=_text(flat, "author_role"),
        item_id=ItemId(_text(flat, "item_id")),
        message_id=_text(flat, "message_id"),
        text=_text(flat, "text"),
        schema=_text(flat, "schema"),
    )


def deduplicate(records: list[AuditRecord]) -> tuple[AuditRecord, ...]:
    """Deduplicate byte-equivalent replays and reject identity collisions."""
    retained: dict[tuple[str, RecordId], AuditRecord] = {}
    for record in records:
        existing = retained.get(record.identity)
        if existing is None:
            retained[record.identity] = record
        elif existing != record:
            raise RecordError("duplicate_event_identity")
    return tuple(sorted(retained.values(), key=lambda item: (item.timestamp, item.event_id)))


def deduplicate_items(records: tuple[AuditRecord, ...]) -> tuple[AuditRecord, ...]:
    """Deduplicate assistant outputs by item identity across replayed records."""
    retained: dict[str, AuditRecord] = {}
    for record in records:
        identity = record.item_id
        if not identity:
            retained[f"missing:{record.event_id}"] = record
            continue
        existing = retained.get(identity)
        if existing is not None and _item_signature(existing) != _item_signature(record):
            raise RecordError("duplicate_item_identity")
        _ = retained.setdefault(identity, record)
    return tuple(retained.values())


def deduplicate_messages(records: tuple[AuditRecord, ...]) -> tuple[AuditRecord, ...]:
    """Deduplicate Discord deliveries by message identity across log rotation."""
    retained: dict[str, AuditRecord] = {}
    for record in records:
        identity = record.message_id
        if not identity:
            retained[f"missing:{record.event_id}"] = record
            continue
        existing = retained.get(identity)
        if existing is not None and _message_signature(existing) != _message_signature(record):
            raise RecordError("duplicate_message_identity")
        _ = retained.setdefault(identity, record)
    return tuple(retained.values())


def _item_signature(record: AuditRecord) -> tuple[str, ...]:
    return (
        record.event,
        record.session_id,
        record.thread_id,
        record.turn_id,
        record.author_id,
        record.text,
    )


def _message_signature(record: AuditRecord) -> tuple[str, ...]:
    return (
        record.session_id,
        record.thread_id,
        record.codex_thread_id,
        record.turn_id,
        record.author_id,
        record.author_role,
        record.item_id,
        record.text,
    )


def _text(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    return value if isinstance(value, str) else ""
