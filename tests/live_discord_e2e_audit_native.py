"""Normalize current native Codex and Discord audit shapes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scripts.state_io import JsonValue

_DISCORD_LOG: Final = re.compile(r"^\[(?P<timestamp>[^\]]+)\] (?P<body>.+)$")


def flatten_rollout(records: Sequence[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """Bind sequential response items to the native turn that owns them."""
    flattened: list[dict[str, JsonValue]] = []
    active_turn = ""
    pending_user_index: int | None = None
    session_id = ""
    for values in records:
        payload = values.get("payload")
        payload_values = payload if isinstance(payload, dict) else {}
        if values.get("type") == "session_meta":
            session_id = _text(payload_values, "id")
        payload_type = _text(payload_values, "type")
        if values.get("type") == "event_msg" and payload_type == "task_started":
            active_turn = _text(payload_values, "turn_id")
            if pending_user_index is not None:
                flattened[pending_user_index]["turn_id"] = active_turn
                pending_user_index = None
        flat = flatten_rollout_record(values, active_turn, session_id)
        flattened.append(flat)
        if flat.get("event") == "user_message":
            pending_user_index = len(flattened) - 1
    return flattened


def flatten_rollout_record(
    values: dict[str, JsonValue], active_turn: str = "", session_id: str = ""
) -> dict[str, JsonValue]:
    """Preserve native IDs, deriving a stable digest only where Codex emits none."""
    if "event" in values:
        return values
    record_type = _text(values, "type")
    payload = values.get("payload")
    if not isinstance(payload, dict):
        return values
    payload_type = _text(payload, "type")
    event = _native_event(record_type, payload)
    item_id = _text(payload, "id")
    event_id = _text(payload, "event_id") or item_id or _native_identity(values)
    author_id: JsonValue = payload.get("author_id", "")
    if event.startswith("assistant_"):
        author_id = "assistant"
    if payload_type == "message" and payload.get("role") == "user":
        author_id = "user"
    if record_type == "session_meta":
        native_id = payload.get("id", "")
        return {
            "surface": "rollout",
            "event": "session_meta",
            "event_id": event_id,
            "timestamp": values.get("timestamp", payload.get("timestamp", "")),
            "session_id": native_id,
            "thread_id": native_id,
        }
    return {
        "surface": "rollout",
        "event": event,
        "timestamp": values.get("timestamp", payload.get("started_at", "")),
        "event_id": event_id,
        "session_id": payload.get("session_id", session_id),
        "thread_id": payload.get("thread_id", session_id),
        "turn_id": payload.get("turn_id", active_turn),
        "author_id": author_id,
        "item_id": payload.get("item_id", item_id or event_id),
        "text": _native_text(payload),
    }


def flatten_discord_message(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Normalize a read-only Discord API message export."""
    author = values.get("author")
    author_values = author if isinstance(author, dict) else {}
    message_id = _text(values, "id")
    return {
        "surface": "discord",
        "schema": "discord_api",
        "event": "message_create",
        "event_id": message_id,
        "message_id": message_id,
        "timestamp": values.get("timestamp", ""),
        "thread_id": values.get("channel_id", ""),
        "codex_thread_id": values.get("codex_thread_id", ""),
        "session_id": values.get("session_id", ""),
        "turn_id": values.get("turn_id", ""),
        "item_id": values.get("item_id", ""),
        "author_id": author_values.get("id", ""),
        "author_role": "bot" if author_values.get("bot") is True else "user",
        "text": values.get("content", ""),
    }


def flatten_discord_log_line(line: str, line_number: int) -> dict[str, JsonValue] | None:
    """Read only public routing fields from the current plaintext bot log."""
    match = _DISCORD_LOG.match(line)
    if match is None:
        return None
    body = match.group("body")
    if not body.startswith(("message ", "session_mirror_sent ")):
        return None
    fields = {
        key: value
        for token in body.split()[1:]
        if "=" in token
        for key, value in (token.split("=", 1),)
    }
    user_event = body.startswith("message ")
    identity = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return {
        "surface": "discord",
        "event": "discord_user_observed" if user_event else "mirror_batch_sent",
        "event_id": f"log-{identity}",
        "timestamp": match.group("timestamp").replace(" ", "T"),
        "thread_id": fields.get("chat", fields.get("channel", "")),
        "codex_thread_id": fields.get("target", ""),
        "author_id": fields.get("user", ""),
        "author_role": "user" if user_event else "bot",
        "message_id": f"log-line-{line_number}",
    }


def _native_event(record_type: str, payload: dict[str, JsonValue]) -> str:
    payload_type = _text(payload, "type")
    if payload_type in {"message", "agent_message"} and record_type in {
        "event_msg",
        "response_item",
    }:
        if payload.get("role") == "user":
            return "user_message"
        return "assistant_final" if payload.get("phase") == "final_answer" else "assistant_output"
    return payload_type or record_type


def _native_text(payload: dict[str, JsonValue]) -> str:
    direct = payload.get("message")
    if isinstance(direct, str):
        return direct
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(_text(part, "text") for part in content if isinstance(part, dict))


def _native_identity(values: dict[str, JsonValue]) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "native-" + hashlib.sha256(encoded).hexdigest()


def _text(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    return value if isinstance(value, str) else ""
