from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts.control_capability import derive_control_capability, provision_control_key
from scripts.work_on_activation import (
    ActivationIdentity,
    ActivationTicketError,
    ActivationTicketStore,
)
from scripts.work_on_hook import process_user_prompt_submit

if TYPE_CHECKING:
    from pathlib import Path


def test_explicit_prompt_issues_same_session_turn_ticket(tmp_path: Path) -> None:
    plugin_data = tmp_path / "plugin-data"
    key = provision_control_key(plugin_data, tmp_path / "state")
    payload = json.dumps(
        {
            "session_id": "session-a",
            "turn_id": "turn-a",
            "transcript_path": "C:/rollouts/a.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "please use $work-on for this task",
        }
    )

    context = process_user_prompt_submit(payload, plugin_data=plugin_data)

    assert context == {
        "session_id": "session-a",
        "activation_turn_id": "turn-a",
        "transcript_path": "C:/rollouts/a.jsonl",
    }
    ActivationTicketStore(plugin_data, key).consume(
        ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"),
        derive_control_capability(key, "session-a"),
    )


def test_consumed_hook_payload_replay_does_not_issue_fresh_ticket(
    tmp_path: Path,
) -> None:
    # Given: one exact hook payload whose authorization was already consumed.
    plugin_data = tmp_path / "plugin-data"
    key = provision_control_key(plugin_data, tmp_path / "state")
    identity = ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl")
    capability = derive_control_capability(key, identity.session_id)
    payload = json.dumps(
        {
            "session_id": identity.session_id,
            "turn_id": identity.turn_id,
            "transcript_path": identity.transcript_path,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "please use $work-on for this task",
        }
    )
    assert process_user_prompt_submit(payload, plugin_data=plugin_data) == {
        "session_id": identity.session_id,
        "activation_turn_id": identity.turn_id,
        "transcript_path": identity.transcript_path,
    }
    ActivationTicketStore(plugin_data, key).consume(identity, capability)

    # When: Codex replays the identical UserPromptSubmit payload.
    replay_context = process_user_prompt_submit(payload, plugin_data=plugin_data)

    # Then: no fresh authorization is exposed or consumable.
    assert replay_context is None
    with pytest.raises(ActivationTicketError, match="work_on_authorization_required"):
        ActivationTicketStore(plugin_data, key).consume(identity, capability)


def test_implicit_prompt_does_not_issue_ticket(tmp_path: Path) -> None:
    plugin_data = tmp_path / "plugin-data"
    key = provision_control_key(plugin_data, tmp_path / "state")
    payload = json.dumps(
        {
            "session_id": "session-a",
            "turn_id": "turn-a",
            "transcript_path": "C:/rollouts/a.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "please monitor this task",
        }
    )

    context = process_user_prompt_submit(payload, plugin_data=plugin_data)

    assert context is None
    with pytest.raises(
        ActivationTicketError,
        match="work_on_authorization_required",
    ):
        ActivationTicketStore(plugin_data, key).consume(
            ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"),
            derive_control_capability(key, "session-a"),
        )


@pytest.mark.parametrize(
    ("character", "side"),
    [
        (character, side)
        for character in (
            "\u200b",
            "\u2060",
            "\ufeff",
            "\u00ad",
            "\u2066",
            "\u2067",
            "\u2069",
            "\u0001",
            "\ud800",
            "\ue000",
            "\u0378",
        )
        for side in ("before", "after")
    ],
    ids=[
        f"{name}-{side}"
        for name in (
            "zero-width-space",
            "word-joiner",
            "bom",
            "soft-hyphen",
            "ltr-isolate",
            "rtl-isolate",
            "pop-isolate",
            "control",
            "surrogate",
            "private-use",
            "unassigned",
        )
        for side in ("before", "after")
    ],
)
def test_invisible_adjacent_character_never_mints_ticket(
    tmp_path: Path,
    character: str,
    side: str,
) -> None:
    plugin_data = tmp_path / "plugin-data"
    key = provision_control_key(plugin_data, tmp_path / "state")
    prompt = f"{character}$work-on" if side == "before" else f"$work-on{character}"
    payload = json.dumps(
        {
            "session_id": "session-a",
            "turn_id": "turn-a",
            "transcript_path": "C:/rollouts/a.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )

    assert process_user_prompt_submit(payload, plugin_data=plugin_data) is None
    with pytest.raises(ActivationTicketError, match="work_on_authorization_required"):
        ActivationTicketStore(plugin_data, key).consume(
            ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"),
            derive_control_capability(key, "session-a"),
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "한글 문장: $work-on 시작",
        "English: ($work-on), now.",
        "「$work-on」、",
        "\u2003$work-on\u3000",
        "$work-on\n요청",
        "요청\n$work-on",
        "\t$work-on\t",
        "\r$work-on\r",
    ],
    ids=(
        "korean",
        "english",
        "cjk-punctuation",
        "unicode-separators",
        "line-feed-after",
        "line-feed-before",
        "tab",
        "carriage-return",
    ),
)
def test_visible_separator_prompt_mints_ticket(tmp_path: Path, prompt: str) -> None:
    plugin_data = tmp_path / "plugin-data"
    key = provision_control_key(plugin_data, tmp_path / "state")
    payload = json.dumps(
        {
            "session_id": "session-a",
            "turn_id": "turn-a",
            "transcript_path": "C:/rollouts/a.jsonl",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )

    assert process_user_prompt_submit(payload, plugin_data=plugin_data) == {
        "session_id": "session-a",
        "activation_turn_id": "turn-a",
        "transcript_path": "C:/rollouts/a.jsonl",
    }
    ActivationTicketStore(plugin_data, key).consume(
        ActivationIdentity("session-a", "turn-a", "C:/rollouts/a.jsonl"),
        derive_control_capability(key, "session-a"),
    )
