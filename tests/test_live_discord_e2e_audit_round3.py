from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import live_discord_e2e_audit as audit_module
from tests.live_discord_e2e_audit import AuditError, main, parse_args
from tests.live_discord_e2e_audit_preflight import PreflightLocator, PreflightSnapshot
from tests.live_discord_e2e_audit_records import decode_json, load_discord_records
from tests.test_live_discord_e2e_audit import (
    CODEX_THREAD,
    MARKER,
    SESSION,
    THREAD,
    audit_fixture,
    discord_fixture,
    rollout_fixture,
)
from tests.test_live_discord_e2e_audit_preflight import (
    PACKAGE_DIGEST,
    SHA,
    preflight_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from scripts.state_io import JsonValue


def write_jsonl(path: Path, rows: Sequence[Mapping[str, JsonValue]]) -> None:
    _ = path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def api_rows(*, contradictory: bool = False) -> list[dict[str, JsonValue]]:
    session_id = "wrong-session" if contradictory else SESSION
    return [
        {
            "id": "discord-user-message",
            "channel_id": THREAD,
            "author": {"id": "discord-user", "bot": False},
            "content": f"request {MARKER}_VISIBLE and {MARKER}_OK",
            "timestamp": "2026-07-24T12:00:01Z",
            "session_id": session_id,
            "codex_thread_id": CODEX_THREAD,
            "turn_id": "activation",
            "item_id": "activation-item",
        },
        {
            "id": "discord-bot-message",
            "channel_id": THREAD,
            "author": {"id": "discord-bot", "bot": True},
            "content": f"{MARKER}_OK",
            "timestamp": "2026-07-24T12:00:07Z",
            "session_id": session_id,
            "codex_thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "item_id": "final-item",
        },
    ]


def binding_rows() -> list[dict[str, JsonValue]]:
    return [
        {
            "type": "codex_discord_message_binding_v1",
            "message_id": "discord-user-message",
            "session_id": SESSION,
            "codex_thread_id": CODEX_THREAD,
            "turn_id": "activation",
            "item_id": "activation-item",
        },
        {
            "type": "codex_discord_message_binding_v1",
            "message_id": "discord-bot-message",
            "session_id": SESSION,
            "codex_thread_id": CODEX_THREAD,
            "turn_id": "automatic",
            "item_id": "final-item",
        },
    ]


def test_check_only_rejects_plaintext_diagnostics_as_semantic_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    write_jsonl(rollout, rollout_fixture())

    exit_code = main(
        [
            "--rollout",
            str(rollout),
            "--discord-log",
            "handoffs/todo10-discord-audit/redacted-discord-log.txt",
            "--thread-id",
            THREAD,
            "--marker",
            MARKER,
            "--check-only",
        ]
    )

    assert exit_code == 1
    assert "authoritative_discord_records_required" in capsys.readouterr().err


def test_raw_api_messages_require_durable_identity_bindings(tmp_path: Path) -> None:
    discord_path = tmp_path / "discord-api.jsonl"
    write_jsonl(discord_path, api_rows())

    with pytest.raises(AuditError, match="discord_message_binding_missing"):
        _ = load_discord_records(discord_path)


def test_api_identity_contradiction_rejects_instead_of_bypassing(
    tmp_path: Path,
) -> None:
    discord_path = tmp_path / "discord-api.jsonl"
    write_jsonl(
        discord_path,
        [*api_rows(contradictory=True), *binding_rows()],
    )

    with pytest.raises(AuditError, match="discord_user_session_mismatch"):
        _ = load_discord_records(discord_path)


def test_rollout_user_after_terminal_before_bot_is_intervening() -> None:
    rollout = rollout_fixture()
    rollout.append(
        {
            **rollout[1],
            "event_id": "post-terminal-rollout-user",
            "turn_id": "later",
            "text": "unrelated follow-up",
            "timestamp": "2026-07-24T12:00:06.5Z",
        }
    )

    with pytest.raises(AuditError, match="intervening_user_event"):
        _ = audit_fixture(rollout, discord_fixture())


def test_discord_user_after_terminal_before_bot_is_intervening() -> None:
    discord = discord_fixture()
    discord.append(
        {
            **discord[0],
            "event_id": "post-terminal-discord-user",
            "message_id": "post-terminal-discord-user",
            "text": "unrelated follow-up",
            "timestamp": "2026-07-24T12:00:06.5Z",
        }
    )

    with pytest.raises(AuditError, match="intervening_user_event"):
        _ = audit_fixture(rollout_fixture(), discord)


def test_cli_accepts_external_candidate_binding_receipt() -> None:
    arguments = parse_args(
        [
            "--rollout",
            "rollout.jsonl",
            "--discord-log",
            "discord-api.jsonl",
            "--thread-id",
            THREAD,
            "--candidate-binding",
            "todo12-candidate-binding.json",
        ]
    )

    assert arguments.candidate_binding == Path("todo12-candidate-binding.json")


def test_plan_style_basic_preflight_auto_resolves_bot_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    discord_path = tmp_path / "discord-api.jsonl"
    write_jsonl(rollout, [])
    write_jsonl(discord_path, [*api_rows(), *binding_rows()])

    locator = PreflightLocator(
        SESSION,
        rollout,
        tmp_path / "plugin",
        tmp_path / "data",
        "dontAsk",
        PACKAGE_DIGEST,
    )

    def located(_path: Path) -> PreflightLocator:
        return locator

    def mapped(_database: Path, _thread: str) -> str:
        return CODEX_THREAD

    monkeypatch.setattr(audit_module, "load_preflight_locator", located)
    monkeypatch.setattr(audit_module, "load_mapping", mapped)

    def collect(
        _locator: PreflightLocator,
        _discord_thread_id: str,
        _codex_thread_id: str,
        expected_digest: str | None,
    ) -> PreflightSnapshot:
        assert expected_digest is None
        return replace(preflight_snapshot(), expected_package_digest_sha256=None)

    monkeypatch.setattr(audit_module, "collect_preflight", collect)

    exit_code = main(
        [
            "--rollout",
            str(rollout),
            "--discord-log",
            str(discord_path),
            "--thread-id",
            THREAD,
            "--expected-sha",
            SHA,
            "--preflight-only",
        ]
    )

    output = decode_json(capsys.readouterr().out)
    assert isinstance(output, dict)
    assert exit_code == 0
    assert output["discord_bot_author_id"] == "discord-bot"
    assert output["installed_sha_matches"] is True
    assert "source_git_sha" not in output
