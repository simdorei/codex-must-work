"""Correlate native Discord message identities without Discord writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from tests.live_discord_e2e_audit_records import deduplicate_messages

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tests.live_discord_e2e_audit_records import AuditRecord


class DiscordAuditError(RuntimeError):
    """Name one failed native Discord correlation."""


@dataclass(frozen=True, slots=True)
class DiscordUserExpected:
    discord_thread_id: str
    marker: str
    session_id: str
    codex_thread_id: str
    activation_turn_id: str
    activation_item_id: str


@dataclass(frozen=True, slots=True)
class DiscordBotExpected:
    discord_thread_id: str
    marker: str
    bot_author_id: str
    session_id: str
    codex_thread_id: str
    automatic_turn_id: str
    final_item_id: str
    final_at: datetime
    terminal_at: datetime
    discord_user_at: datetime


def discord_user_message(
    discord: Sequence[AuditRecord],
    expected: DiscordUserExpected,
) -> tuple[str, datetime]:
    """Bind the exact native user message to the rollout activation."""
    marker_rows = tuple(row for row in discord if expected.marker in row.text)
    _require(
        all(row.thread_id == expected.discord_thread_id for row in marker_rows),
        "discord_thread_mismatch",
    )
    users = tuple(
        row for row in marker_rows if row.event == "message_create" and row.author_role == "user"
    )
    _require(len(users) == 1 and bool(users[0].message_id), "discord_user_message_count")
    user = users[0]
    _require(user.session_id == expected.session_id, "discord_user_session_mismatch")
    _require(
        user.codex_thread_id == expected.codex_thread_id,
        "discord_user_thread_mismatch",
    )
    _require(
        user.turn_id == expected.activation_turn_id,
        "discord_user_turn_mismatch",
    )
    _require(user.item_id == expected.activation_item_id, "discord_user_item_mismatch")
    return user.message_id, _timestamp(user)


def discord_bot_message(
    discord: Sequence[AuditRecord],
    expected: DiscordBotExpected,
) -> tuple[str, datetime]:
    """Require the exact bot author and one post-terminal native delivery."""
    bots = deduplicate_messages(
        tuple(
            row
            for row in discord
            if row.event == "message_create"
            and row.author_role == "bot"
            and row.text == f"{expected.marker}_OK"
        )
    )
    _require(len(bots) == 1 and bool(bots[0].message_id), "discord_bot_message_count")
    bot = bots[0]
    _require(bot.thread_id == expected.discord_thread_id, "discord_thread_mismatch")
    _require(bot.author_id == expected.bot_author_id, "discord_bot_author_mismatch")
    bot_at = _timestamp(bot)
    _require(
        bot_at >= expected.terminal_at
        and bot_at > expected.final_at
        and bot_at > expected.discord_user_at,
        "discord_bot_message_order",
    )
    _require(bot.session_id == expected.session_id, "bot_message_session_mismatch")
    _require(
        bot.codex_thread_id == expected.codex_thread_id,
        "bot_message_thread_mismatch",
    )
    _require(bot.turn_id == expected.automatic_turn_id, "bot_message_turn_mismatch")
    _require(bot.item_id == expected.final_item_id, "bot_message_item_mismatch")
    return bot.message_id, bot_at


def require_no_intervening_discord_user(
    discord: Sequence[AuditRecord],
    *,
    discord_thread_id: str,
    activation_message_id: str,
    activation_at: datetime,
    bot_at: datetime,
) -> None:
    """Reject every other user message through the selected bot delivery."""
    users = deduplicate_messages(
        tuple(
            row
            for row in discord
            if row.event == "message_create"
            and row.author_role == "user"
            and row.thread_id == discord_thread_id
        )
    )
    intervening = tuple(
        row
        for row in users
        if row.message_id != activation_message_id and activation_at <= _timestamp(row) <= bot_at
    )
    _require(not intervening, "intervening_user_event")


def _timestamp(record: AuditRecord) -> datetime:
    try:
        return datetime.fromisoformat(record.timestamp)
    except ValueError as error:
        reason = "timestamp_invalid"
        raise DiscordAuditError(reason) from error


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise DiscordAuditError(reason)
