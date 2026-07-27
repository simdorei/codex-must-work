"""Evaluate the semantic Discord continuation proof."""
# ruff: noqa: TC003

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tests.live_discord_e2e_audit_discord import (
    DiscordBotExpected,
    DiscordUserExpected,
    discord_bot_message,
    discord_user_message,
    require_no_intervening_discord_user,
)
from tests.live_discord_e2e_audit_models import AuditResult, AuditTarget
from tests.live_discord_e2e_audit_records import (
    AuditRecord,
    deduplicate_items,
)


class SemanticAuditError(RuntimeError):
    """Name one machine-checkable semantic audit failure."""


@dataclass(frozen=True, slots=True)
class _AuditContext:
    session_id: str
    codex_thread_id: str
    activation_turn_id: str
    activation_item_id: str
    activation_event_id: str
    user_at: datetime


@dataclass(frozen=True, slots=True)
class _TurnProof:
    automatic_turn_id: str
    visible_item_id: str
    final_item_id: str
    intervening_user_events: int
    final_at: datetime
    terminal_at: datetime


def audit_semantics(
    rollout: Sequence[AuditRecord],
    discord: Sequence[AuditRecord],
    target: AuditTarget,
) -> AuditResult:
    """Require one exact user activation and one automatic managed turn."""
    context = _rollout_context(rollout, target)
    discord_user_id, discord_user_at = discord_user_message(
        discord,
        DiscordUserExpected(
            target.discord_thread_id,
            target.marker,
            context.session_id,
            context.codex_thread_id,
            context.activation_turn_id,
            context.activation_item_id,
        ),
    )
    scoped = _rollout_scope(rollout, target, context)
    proof = _turn_proof(scoped, target, context)
    bot_message_id, bot_at = discord_bot_message(
        discord,
        DiscordBotExpected(
            target.discord_thread_id,
            target.marker,
            target.discord_bot_author_id,
            context.session_id,
            context.codex_thread_id,
            proof.automatic_turn_id,
            proof.final_item_id,
            proof.final_at,
            proof.terminal_at,
            discord_user_at,
        ),
    )
    _require_no_intervening_rollout_user(scoped, context, bot_at)
    require_no_intervening_discord_user(
        discord,
        discord_thread_id=target.discord_thread_id,
        activation_message_id=discord_user_id,
        activation_at=discord_user_at,
        bot_at=bot_at,
    )
    return AuditResult(
        context.session_id,
        context.codex_thread_id,
        context.activation_turn_id,
        proof.automatic_turn_id,
        proof.visible_item_id,
        proof.final_item_id,
        discord_user_id,
        bot_message_id,
        proof.intervening_user_events,
    )


def _rollout_context(
    rollout: Sequence[AuditRecord],
    target: AuditTarget,
) -> _AuditContext:
    sessions = tuple(row for row in rollout if row.event == "session_meta")
    _require(
        len(sessions) == 1 and bool(sessions[0].session_id and sessions[0].thread_id),
        "session_meta_mismatch",
    )
    marker_rows = tuple(row for row in rollout if target.marker in row.text)
    users = tuple(row for row in marker_rows if row.event == "user_message")
    _require(bool(users), "rollout_user_message_count")
    first_at = min(_timestamp(row) for row in users)
    first = tuple(row for row in users if _timestamp(row) == first_at)
    _require(
        len(first) == 1 and bool(first[0].turn_id and first[0].item_id),
        "rollout_user_message_count",
    )
    user = first[0]
    _require(
        user.session_id == sessions[0].session_id and user.thread_id == sessions[0].thread_id,
        "rollout_user_identity_mismatch",
    )
    return _AuditContext(
        user.session_id,
        user.thread_id,
        user.turn_id,
        user.item_id,
        user.event_id,
        _timestamp(user),
    )


def _rollout_scope(
    rollout: Sequence[AuditRecord],
    target: AuditTarget,
    context: _AuditContext,
) -> tuple[AuditRecord, ...]:
    marker_rows = tuple(row for row in rollout if target.marker in row.text)
    _require(
        all(row.session_id == context.session_id for row in marker_rows),
        "cross_session_record",
    )
    _require(
        all(row.thread_id == context.codex_thread_id for row in marker_rows),
        "cross_thread_record",
    )
    related = tuple(
        row
        for row in rollout
        if row.session_id == context.session_id or row.event == "session_meta"
    )
    sessions = tuple(row for row in related if row.event == "session_meta")
    _require(
        len(sessions) == 1 and sessions[0].session_id == context.session_id,
        "session_meta_mismatch",
    )
    scoped = tuple(row for row in related if row.event != "session_meta")
    _require(
        all(row.session_id == context.session_id for row in scoped),
        "cross_session_record",
    )
    _require(
        all(row.thread_id == context.codex_thread_id for row in scoped),
        "cross_thread_record",
    )
    return scoped


def _turn_proof(
    scoped: Sequence[AuditRecord],
    target: AuditTarget,
    context: _AuditContext,
) -> _TurnProof:
    terminals = tuple(
        row
        for row in scoped
        if row.event == "task_complete" and row.turn_id == context.activation_turn_id
    )
    _require(len(terminals) == 1, "activation_terminal_count")
    terminal_at = _timestamp(terminals[0])
    starts = tuple(
        row
        for row in scoped
        if row.event == "task_started"
        and _timestamp(row) > terminal_at
        and row.turn_id != context.activation_turn_id
    )
    _require(len(starts) == 1, "automatic_turn_count")
    automatic = starts[0]
    automatic_at = _timestamp(automatic)
    automatic_terminals = tuple(
        row for row in scoped if row.event == "task_complete" and row.turn_id == automatic.turn_id
    )
    _require(len(automatic_terminals) == 1, "automatic_terminal_count")
    automatic_terminal_at = _timestamp(automatic_terminals[0])
    intervening = tuple(
        row
        for row in scoped
        if row.event == "user_message"
        and context.user_at < _timestamp(row) <= automatic_terminal_at
    )
    _require(not intervening, "intervening_user_event")
    items = deduplicate_items(
        tuple(
            row
            for row in scoped
            if row.turn_id == automatic.turn_id
            and row.event in {"assistant_output", "assistant_final"}
        )
    )
    _require(all(row.author_id == "assistant" for row in items), "assistant_author_invalid")
    _require(
        all(automatic_at < _timestamp(row) < automatic_terminal_at for row in items),
        "assistant_item_order",
    )
    visible = tuple(row for row in items if row.text.count(f"{target.marker}_VISIBLE") == 1)
    _require(len(visible) == 1 and bool(visible[0].item_id), "visible_item_count")
    final = tuple(
        row for row in items if row.event == "assistant_final" and row.text == f"{target.marker}_OK"
    )
    _require(len(final) == 1 and bool(final[0].item_id), "final_item_count")
    return _TurnProof(
        automatic.turn_id,
        visible[0].item_id,
        final[0].item_id,
        len(intervening),
        _timestamp(final[0]),
        automatic_terminal_at,
    )


def _timestamp(record: AuditRecord) -> datetime:
    try:
        return datetime.fromisoformat(record.timestamp)
    except ValueError as error:
        message = "timestamp_invalid"
        raise SemanticAuditError(message) from error


def _require_no_intervening_rollout_user(
    scoped: Sequence[AuditRecord],
    context: _AuditContext,
    bot_at: datetime,
) -> None:
    intervening = tuple(
        row
        for row in scoped
        if row.event == "user_message"
        and row.event_id != context.activation_event_id
        and context.user_at <= _timestamp(row) <= bot_at
    )
    _require(not intervening, "intervening_user_event")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise SemanticAuditError(reason)
