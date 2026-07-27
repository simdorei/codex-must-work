from __future__ import annotations

from typing import TYPE_CHECKING, Never

import pytest

from tests import real_install_smoke_ledger as ledger
from tests import real_install_smoke_support as support
from tests.real_install_smoke_fixtures import HEAD as _HEAD
from tests.real_install_smoke_fixtures import JsonObject, JsonValue
from tests.real_install_smoke_fixtures import rollout_records as _rollout_records

if TYPE_CHECKING:
    from pathlib import Path


def _mapping(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _invalid_fixture()
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        _invalid_fixture()
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        _invalid_fixture()
    return value


def _invalid_fixture() -> Never:
    pytest.fail("rollout fixture shape is invalid")


def test_rollout_accepts_distinct_session_and_first_prompt_turns(tmp_path: Path) -> None:
    rollout, records, visible = _rollout_records(tmp_path)

    result = support.verify_rollout(
        rollout,
        records,
        visible,
        tmp_path / "cmw/hooks/hooks.json",
        tmp_path / "lazy/hooks/hooks.json",
    )

    assert result == support.RolloutCheck(
        locator_matches=True,
        lazy_context_matches=True,
        visible_output_matches=True,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("combined", "hook_record_identity_mismatch"),
        ("wrong_header", "lazy_context_header_invalid"),
        ("different_turn", "lazy_first_prompt_binding_invalid"),
        ("ambiguous_engine", "lazy_engine_invalid"),
        ("warning", "lazy_warning_invalid"),
        ("locator", "cmw_locator_identity_invalid"),
        ("prefix", "visible_prefix_invalid"),
        ("tail", "visible_tail_invalid"),
        ("prompt_marker_only", "visible_prefix_invalid"),
    ],
)
def test_rollout_rejects_false_positive_records(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    rollout, records, visible = _rollout_records(tmp_path)
    cmw_payload = _mapping(records[2]["payload"])
    lazy_payload = _mapping(records[3]["payload"])
    cmw_run = _mapping(cmw_payload["run"])
    lazy_run = _mapping(lazy_payload["run"])
    cmw_entries = _items(cmw_run["entries"])
    lazy_entries = _items(lazy_run["entries"])

    if mutation == "combined":
        cmw_entries.extend(lazy_entries)
    elif mutation == "wrong_header":
        context = _mapping(lazy_entries[1])
        context["text"] = _text(context["text"]).replace("Prompt", "Wrong", 1)
    elif mutation == "different_turn":
        lazy_payload["turn_id"] = "turn-later"
    elif mutation == "ambiguous_engine":
        context = _mapping(lazy_entries[1])
        context["text"] = _text(context["text"]) + "\nRewrite engine: second"
    elif mutation == "warning":
        warning = _mapping(lazy_entries[0])
        warning["text"] = _text(warning["text"]) + " extra"
    elif mutation == "locator":
        locator = _mapping(cmw_entries[0])
        locator["text"] = _text(locator["text"]).replace("019b", "019c")
    elif mutation == "prefix":
        visible[0] = "wrong"
    elif mutation == "tail":
        visible[-1] = "not done"
    else:
        visible[:] = ["user prompt contains SMOKE_OK"]

    with pytest.raises(ledger.SmokeError, match=reason):
        _ = support.verify_rollout(
            rollout,
            records,
            visible,
            tmp_path / "cmw/hooks/hooks.json",
            tmp_path / "lazy/hooks/hooks.json",
        )


def test_privacy_safe_output_rejects_paths_digests_bodies_or_credentials() -> None:
    check = ledger.ConfigCheck(
        allowed_delta_exact=True,
        non_cmw_bytes_unchanged=True,
        trust_count=3,
    )

    safe = support.safe_output(_HEAD, check, 0, 0)

    assert "second_install_no_write=true" in safe
    assert "allowed_delta_exact=true" in safe
    assert _HEAD in safe
    assert not any(token in safe.lower() for token in ("path=", "digest=", "auth", "translation="))
