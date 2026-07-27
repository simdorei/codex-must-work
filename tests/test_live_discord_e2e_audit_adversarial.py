from __future__ import annotations

import pytest

from tests.live_discord_e2e_audit import AuditError
from tests.test_live_discord_e2e_audit import (
    audit_fixture,
    discord_fixture,
    rollout_fixture,
)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("wrong_thread", "discord_thread_mismatch"),
        ("wrong_session", "cross_session_record"),
        ("wrong_turn", "bot_message_turn_mismatch"),
        ("wrong_author", "assistant_author_invalid"),
        ("intervening_user", "intervening_user_event"),
        ("duplicate_event", "duplicate_event_identity"),
        ("stale_bot", "discord_bot_message_count"),
        ("marker_prompt_only", "visible_item_count"),
        ("duplicate_visible", "visible_item_count"),
    ],
)
def test_audit_rejects_semantic_false_positives(mutation: str, reason: str) -> None:
    rollout = rollout_fixture()
    discord = discord_fixture()
    if mutation == "wrong_thread":
        discord[1]["thread_id"] = "other"
    elif mutation == "wrong_session":
        rollout[4]["session_id"] = "other"
    elif mutation == "wrong_turn":
        discord[1]["turn_id"] = "activation"
    elif mutation == "wrong_author":
        rollout[4]["author_id"] = "discord-user"
    elif mutation == "intervening_user":
        rollout.append(
            {
                **rollout[1],
                "event_id": "later-user",
                "turn_id": "between",
                "timestamp": "2026-07-24T12:00:02.5Z",
            }
        )
    elif mutation == "duplicate_event":
        replay = dict(rollout[4])
        replay["text"] = "changed replay"
        rollout.append(replay)
    elif mutation == "stale_bot":
        stale = dict(discord[1])
        stale["event_id"] = "d3"
        stale["message_id"] = "stale"
        discord.append(stale)
    elif mutation == "marker_prompt_only":
        rollout[4]["text"] = "no visible marker"
    else:
        duplicate = dict(rollout[4])
        duplicate["event_id"] = "r7"
        duplicate["item_id"] = "visible-item-2"
        rollout.append(duplicate)

    with pytest.raises(AuditError, match=reason):
        _ = audit_fixture(rollout, discord)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("user_during_automatic", "intervening_user_event"),
        ("assistant_items_before_turn", "assistant_item_order"),
        ("bot_before_user", "discord_bot_message_order"),
        ("wrong_bot_author_id", "discord_bot_author_mismatch"),
    ],
)
def test_audit_rejects_invalid_automatic_interval(mutation: str, reason: str) -> None:
    rollout = rollout_fixture()
    discord = discord_fixture()
    if mutation == "user_during_automatic":
        rollout.append(
            {
                **rollout[1],
                "event_id": "later-user",
                "turn_id": "automatic",
                "timestamp": "2026-07-24T12:00:04.5Z",
            }
        )
    elif mutation == "assistant_items_before_turn":
        rollout[4]["timestamp"] = "2026-07-24T12:00:02.5Z"
    elif mutation == "bot_before_user":
        discord[1]["timestamp"] = "2026-07-24T12:00:00.5Z"
    else:
        discord[1]["author_id"] = "attacker"

    with pytest.raises(AuditError, match=reason):
        _ = audit_fixture(rollout, discord)
