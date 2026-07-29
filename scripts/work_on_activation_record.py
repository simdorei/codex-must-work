"""Encode and authenticate persisted work-on activation records."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, final

from scripts.state_io import JsonValue, StateError, atomic_json_write, open_direct_file
from scripts.work_on_identity import ActivationIdentity

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA_VERSION: Final = 1
_RECORD_DOMAIN: Final = b"cmw-work-on-ticket-record-v1\0"
_RECORD_FIELDS: Final = (
    "schema_version",
    "session_id",
    "turn_id",
    "transcript_path",
    "capability_binding",
    "issued_at",
    "nonce",
    "consumed",
    "record_mac",
)


class _JsonLoader(Protocol):
    def __call__(self, value: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class ActivationTicketRecord:
    """Retain the complete authenticated state of one activation ticket."""

    identity: ActivationIdentity
    capability_binding: str
    issued_at: int
    nonce: str
    consumed: bool


@final
class ActivationRecordError(StateError):
    """Signal an invalid authenticated activation record."""


def write_activation_record(
    path: Path,
    key: bytes,
    record: ActivationTicketRecord,
) -> None:
    """Persist one record with a MAC covering every mutable field."""
    values = _record_values(record)
    values["record_mac"] = _record_mac(key, record)
    atomic_json_write(path, schema_version=_SCHEMA_VERSION, values=values)


def read_activation_record(
    path: Path,
    key: bytes,
) -> tuple[ActivationTicketRecord, int, int]:
    """Read and authenticate one direct regular-file activation record."""
    _ = path.lstat()
    descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        decoded = _LOAD_JSON(handle.read())
        opened = os.fstat(handle.fileno())
    if type(decoded) is not dict or set(decoded) != set(_RECORD_FIELDS):
        raise ActivationRecordError
    record = _parse_record(decoded)
    record_mac = decoded.get("record_mac")
    if type(record_mac) is not str or not hmac.compare_digest(
        record_mac,
        _record_mac(key, record),
    ):
        raise ActivationRecordError
    return record, opened.st_dev, opened.st_ino


def _parse_record(decoded: dict[str, JsonValue]) -> ActivationTicketRecord:
    session_id = decoded.get("session_id")
    turn_id = decoded.get("turn_id")
    transcript_path = decoded.get("transcript_path")
    capability_binding = decoded.get("capability_binding")
    issued_at = decoded.get("issued_at")
    nonce = decoded.get("nonce")
    consumed = decoded.get("consumed")
    if (
        decoded["schema_version"] != _SCHEMA_VERSION
        or type(session_id) is not str
        or type(turn_id) is not str
        or type(transcript_path) is not str
        or type(capability_binding) is not str
        or type(issued_at) is not int
        or type(nonce) is not str
        or type(consumed) is not bool
    ):
        raise ActivationRecordError
    return ActivationTicketRecord(
        ActivationIdentity(session_id, turn_id, transcript_path),
        capability_binding,
        issued_at,
        nonce,
        consumed,
    )


def _record_values(record: ActivationTicketRecord) -> dict[str, JsonValue]:
    return {
        "session_id": record.identity.session_id,
        "turn_id": record.identity.turn_id,
        "transcript_path": record.identity.transcript_path,
        "capability_binding": record.capability_binding,
        "issued_at": record.issued_at,
        "nonce": record.nonce,
        "consumed": record.consumed,
    }


def _record_mac(key: bytes, record: ActivationTicketRecord) -> str:
    payload: dict[str, JsonValue] = {
        "schema_version": _SCHEMA_VERSION,
        **_record_values(record),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, _RECORD_DOMAIN + encoded, hashlib.sha256).hexdigest()
