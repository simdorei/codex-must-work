"""Bind native Discord API messages to durable Codex identity records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, Protocol, override

from tests.live_discord_e2e_audit_native import flatten_discord_message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scripts.state_io import JsonValue

_BINDING_SCHEMA: Final = "codex_discord_message_binding_v1"
_AUTHORITATIVE_SCHEMAS: Final = frozenset({"discord_api_bound", "discord_durable_message"})


@dataclass(frozen=True, slots=True)
class DiscordAuthorityError(RuntimeError):
    """Reject Discord evidence that is diagnostic, unbound, or contradictory."""

    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class DiscordMessageBinding:
    """One durable link from a Discord message ID to exact Codex identities."""

    message_id: str
    session_id: str
    codex_thread_id: str
    turn_id: str
    item_id: str


class AuthoritativeRecord(Protocol):
    """Minimal Discord record shape consumed by authority policy."""

    @property
    def event(self) -> str: ...

    @property
    def schema(self) -> str: ...

    @property
    def message_id(self) -> str: ...

    @property
    def thread_id(self) -> str: ...

    @property
    def author_id(self) -> str: ...

    @property
    def author_role(self) -> str: ...

    @property
    def timestamp(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def codex_thread_id(self) -> str: ...

    @property
    def turn_id(self) -> str: ...

    @property
    def item_id(self) -> str: ...


def bind_discord_documents(
    documents: Sequence[dict[str, JsonValue]],
) -> tuple[dict[str, JsonValue], ...]:
    """Join raw API rows to separate durable bindings by exact message ID."""
    bindings: dict[str, DiscordMessageBinding] = {}
    messages: list[dict[str, JsonValue]] = []
    passthrough: list[dict[str, JsonValue]] = []
    for document in documents:
        if document.get("type") == _BINDING_SCHEMA:
            binding = _parse_binding(document)
            existing = bindings.get(binding.message_id)
            if existing is not None and existing != binding:
                _fail("discord_message_binding_conflict")
            bindings[binding.message_id] = binding
        elif "id" in document and "author" in document:
            messages.append(document)
        else:
            passthrough.append(document)
    bound = [_bind_message(message, bindings) for message in messages]
    bound_ids = {str(message["message_id"]) for message in bound}
    if set(bindings) - bound_ids:
        _fail("discord_message_binding_orphaned")
    return (*passthrough, *bound)


def require_authoritative_discord(records: Sequence[AuthoritativeRecord]) -> None:
    """Fail closed unless every semantic message is from an authoritative store."""
    messages = tuple(row for row in records if row.event == "message_create")
    if not messages or any(row.schema not in _AUTHORITATIVE_SCHEMAS for row in messages):
        _fail("authoritative_discord_records_required")
    if any(
        not (
            row.message_id
            and row.thread_id
            and row.author_id
            and row.timestamp
            and row.session_id
            and row.codex_thread_id
            and row.turn_id
            and row.item_id
        )
        for row in messages
    ):
        _fail("authoritative_discord_identity_missing")


def resolve_discord_bot_author_id(
    records: Sequence[AuthoritativeRecord],
    discord_thread_id: str,
    explicit_author_id: str | None = None,
) -> str:
    """Resolve one bot ID from authoritative native API or durable message rows."""
    require_authoritative_discord(records)
    candidates = {
        row.author_id
        for row in records
        if row.event == "message_create"
        and row.schema in _AUTHORITATIVE_SCHEMAS
        and row.thread_id == discord_thread_id
        and row.author_role == "bot"
        and row.author_id
    }
    if len(candidates) != 1:
        _fail("discord_bot_identity_not_unique")
    resolved = next(iter(candidates))
    if explicit_author_id is not None and explicit_author_id != resolved:
        _fail("discord_bot_author_mismatch")
    return resolved


def _parse_binding(values: dict[str, JsonValue]) -> DiscordMessageBinding:
    fields = tuple(
        _required_text(values, key)
        for key in (
            "message_id",
            "session_id",
            "codex_thread_id",
            "turn_id",
            "item_id",
        )
    )
    return DiscordMessageBinding(*fields)


def _bind_message(
    values: dict[str, JsonValue],
    bindings: dict[str, DiscordMessageBinding],
) -> dict[str, JsonValue]:
    message_id = _required_text(values, "id")
    binding = bindings.get(message_id)
    if binding is None:
        _fail("discord_message_binding_missing")
    flattened = flatten_discord_message(values)
    role = "bot" if flattened.get("author_role") == "bot" else "user"
    correlation = {
        "session_id": binding.session_id,
        "codex_thread_id": binding.codex_thread_id,
        "turn_id": binding.turn_id,
        "item_id": binding.item_id,
    }
    for key, expected in correlation.items():
        supplied = values.get(key)
        if isinstance(supplied, str) and supplied and supplied != expected:
            prefix = "bot_message" if role == "bot" else "discord_user"
            suffix = "thread" if key == "codex_thread_id" else key.removesuffix("_id")
            _fail(f"{prefix}_{suffix}_mismatch")
        flattened[key] = expected
    flattened["schema"] = "discord_api_bound"
    return flattened


def _required_text(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        _fail("discord_message_binding_invalid")
    return value


def _fail(reason: str) -> Never:
    raise DiscordAuthorityError(reason)
