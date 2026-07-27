from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_discord_e2e_audit import AuditError, audit_records, main
from tests.live_discord_e2e_audit_authority import resolve_discord_bot_author_id
from tests.live_discord_e2e_audit_records import load_discord_records, load_records
from tests.test_live_discord_e2e_audit import (
    MARKER,
    THREAD,
    rollout_fixture,
)
from tests.test_live_discord_e2e_audit_round3 import (
    api_rows,
    binding_rows,
    write_jsonl,
)


@pytest.mark.parametrize(
    ("binding_index", "field", "value", "reason"),
    [
        (0, "session_id", "wrong", "discord_user_session_mismatch"),
        (0, "codex_thread_id", "wrong", "discord_user_thread_mismatch"),
        (0, "turn_id", "wrong", "discord_user_turn_mismatch"),
        (0, "item_id", "wrong", "discord_user_item_mismatch"),
        (1, "session_id", "wrong", "bot_message_session_mismatch"),
        (1, "codex_thread_id", "wrong", "bot_message_thread_mismatch"),
        (1, "turn_id", "wrong", "bot_message_turn_mismatch"),
        (1, "item_id", "wrong", "bot_message_item_mismatch"),
    ],
)
def test_durable_binding_must_match_exact_rollout_identity(
    tmp_path: Path,
    binding_index: int,
    field: str,
    value: str,
    reason: str,
) -> None:
    bindings = binding_rows()
    bindings[binding_index][field] = value
    messages = api_rows()
    for row in messages:
        for correlation_field in (
            "session_id",
            "codex_thread_id",
            "turn_id",
            "item_id",
        ):
            _ = row.pop(correlation_field)
    discord_path = tmp_path / "discord-api.jsonl"
    write_jsonl(discord_path, [*messages, *bindings])
    discord = load_discord_records(discord_path)

    with pytest.raises(AuditError, match=reason):
        _ = audit_records(
            rollout_fixture(),
            discord,
            thread_id=THREAD,
            marker=MARKER,
            discord_bot_author_id="discord-bot",
        )


def test_explicit_bot_identity_must_match_authoritative_api_identity(
    tmp_path: Path,
) -> None:
    discord_path = tmp_path / "discord-api.jsonl"
    write_jsonl(discord_path, [*api_rows(), *binding_rows()])
    discord = load_discord_records(discord_path)

    with pytest.raises(AuditError, match="discord_bot_author_mismatch"):
        _ = resolve_discord_bot_author_id(discord, THREAD, "attacker")


def test_native_api_check_only_auto_resolves_bot_without_writes(
    tmp_path: Path,
) -> None:
    fixture_root = Path("handoffs/todo10-discord-audit")
    output = tmp_path / "must-not-exist.json"
    rollout = fixture_root / "redacted-native-rollout.jsonl"
    discord = fixture_root / "redacted-native-discord-messages.jsonl"
    before = (rollout.read_bytes(), discord.read_bytes())

    exit_code = main(
        [
            "--rollout",
            str(rollout),
            "--discord-log",
            str(discord),
            "--thread-id",
            THREAD,
            "--marker",
            MARKER,
            "--check-only",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert not output.exists()
    assert before == (rollout.read_bytes(), discord.read_bytes())
    records = load_records(rollout, "rollout")
    assert any(record.event == "assistant_final" for record in records)
