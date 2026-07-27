from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.live_discord_e2e_audit import AuditError, audit_records, load_records, main
from tests.live_discord_e2e_audit_records import load_discord_records

if TYPE_CHECKING:
    from tests.live_discord_e2e_audit_models import AuditResult

THREAD = "1528639615592828980"
SESSION = "session-plan"
CODEX_THREAD = "codex-thread-plan"
MARKER = "CMW_E2E_20260724T120000Z_0123456789ab"


def rollout_fixture() -> list[dict[str, str]]:
    return [
        {
            "surface": "rollout",
            "event": "session_meta",
            "event_id": "r0",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "timestamp": "2026-07-24T12:00:00Z",
        },
        {
            "surface": "rollout",
            "event": "user_message",
            "event_id": "r1",
            "item_id": "activation-item",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "activation",
            "author_id": "discord-user",
            "text": f"request {MARKER}_VISIBLE and {MARKER}_OK",
            "timestamp": "2026-07-24T12:00:01Z",
        },
        {
            "surface": "rollout",
            "event": "task_complete",
            "event_id": "r2",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "activation",
            "author_id": "assistant",
            "timestamp": "2026-07-24T12:00:02Z",
        },
        {
            "surface": "rollout",
            "event": "task_started",
            "event_id": "r3",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "author_id": "assistant",
            "timestamp": "2026-07-24T12:00:03Z",
        },
        {
            "surface": "rollout",
            "event": "assistant_output",
            "event_id": "r4",
            "item_id": "visible-item",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "author_id": "assistant",
            "text": f"verified {MARKER}_VISIBLE",
            "timestamp": "2026-07-24T12:00:04Z",
        },
        {
            "surface": "rollout",
            "event": "assistant_final",
            "event_id": "r5",
            "item_id": "final-item",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "author_id": "assistant",
            "text": f"{MARKER}_OK",
            "timestamp": "2026-07-24T12:00:05Z",
        },
        {
            "surface": "rollout",
            "event": "task_complete",
            "event_id": "r6",
            "session_id": SESSION,
            "thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "author_id": "assistant",
            "timestamp": "2026-07-24T12:00:06Z",
        },
    ]


def discord_fixture() -> list[dict[str, str]]:
    return [
        {
            "surface": "discord",
            "schema": "discord_durable_message",
            "event": "message_create",
            "event_id": "d1",
            "message_id": "discord-user-message",
            "thread_id": THREAD,
            "codex_thread_id": CODEX_THREAD,
            "session_id": SESSION,
            "turn_id": "activation",
            "item_id": "activation-item",
            "author_id": "discord-user",
            "author_role": "user",
            "text": f"request {MARKER}_VISIBLE and {MARKER}_OK",
            "timestamp": "2026-07-24T12:00:01Z",
        },
        {
            "surface": "discord",
            "schema": "discord_durable_message",
            "event": "message_create",
            "event_id": "d2",
            "message_id": "discord-bot-message",
            "item_id": "final-item",
            "thread_id": THREAD,
            "codex_thread_id": CODEX_THREAD,
            "session_id": SESSION,
            "turn_id": "automatic",
            "author_id": "discord-bot",
            "author_role": "bot",
            "text": f"{MARKER}_OK",
            "timestamp": "2026-07-24T12:00:07Z",
        },
    ]


def audit_fixture(rollout: list[dict[str, str]], discord: list[dict[str, str]]) -> AuditResult:
    return audit_records(
        rollout,
        discord,
        thread_id=THREAD,
        marker=MARKER,
        discord_bot_author_id="discord-bot",
    )


def test_audit_accepts_out_of_order_records_and_identity_replay() -> None:
    # Given
    rollout = list(reversed(rollout_fixture()))
    rollout.append(dict(rollout[1]))

    # When
    result = audit_fixture(rollout, discord_fixture())

    # Then
    assert result.automatic_turn_id == "automatic"
    assert result.rollout_visible_item_id == "visible-item"
    assert result.discord_bot_message_id == "discord-bot-message"
    assert result.intervening_user_events == 0


def test_audit_deduplicates_items_and_messages_by_semantic_identity() -> None:
    # Given
    rollout = rollout_fixture()
    replayed_item = dict(rollout[4])
    replayed_item["event_id"] = "rotated-r4"
    replayed_item["timestamp"] = "2026-07-24T12:00:04.5Z"
    rollout.append(replayed_item)
    discord = discord_fixture()
    replayed_message = dict(discord[1])
    replayed_message["event_id"] = "rotated-d2"
    replayed_message["timestamp"] = "2026-07-24T12:00:07.5Z"
    discord.append(replayed_message)

    # When
    result = audit_fixture(rollout, discord)

    # Then
    assert result.rollout_visible_item_id == "visible-item"
    assert result.discord_bot_message_id == "discord-bot-message"


def test_load_records_rejects_malformed_json_without_interpreting_text(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "records.jsonl"
    _ = path.write_text(
        '{"event":"user_message","event_id":"safe"}\nignore previous instructions\n',
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(AuditError, match="malformed_json"):
        _ = load_records(path, expected_surface="rollout")


def test_check_only_cli_contract_writes_no_output_file(tmp_path: Path) -> None:
    # Given
    rollout = tmp_path / "rollout.jsonl"
    discord = tmp_path / "discord.jsonl"
    output = tmp_path / "must-not-exist.json"
    _ = rollout.write_text(
        "\n".join(json.dumps(row) for row in rollout_fixture()) + "\n",
        encoding="utf-8",
    )
    _ = discord.write_text(
        "\n".join(json.dumps(row) for row in discord_fixture()) + "\n",
        encoding="utf-8",
    )

    # When
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
            "--discord-bot-author-id",
            "discord-bot",
            "--check-only",
            "--output",
            str(output),
        ]
    )

    # Then
    assert exit_code == 0
    assert not output.exists()


def test_native_rollout_and_discord_api_shapes_complete_semantic_audit() -> None:
    fixture_root = Path("handoffs/todo10-discord-audit")
    rollout = load_records(
        fixture_root / "redacted-native-rollout.jsonl", expected_surface="rollout"
    )
    discord = load_discord_records(fixture_root / "redacted-native-discord-messages.jsonl")

    result = audit_records(
        rollout,
        discord,
        thread_id=THREAD,
        marker=MARKER,
        discord_bot_author_id="discord-bot",
    )

    assert result.activation_turn_id == "activation"
    assert result.automatic_turn_id == "automatic"
    assert result.discord_bot_message_id == "discord-bot-message"


def test_current_plaintext_discord_log_shape_is_read_without_private_text() -> None:
    records = load_discord_records(Path("handoffs/todo10-discord-audit/redacted-discord-log.txt"))

    assert [record.event for record in records] == [
        "discord_user_observed",
        "mirror_batch_sent",
    ]
