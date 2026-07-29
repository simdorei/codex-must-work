from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.mcp_arguments import (
    parse_session_request,
    parse_start_request,
)
from scripts.mcp_protocol import JsonRpcError

if TYPE_CHECKING:
    from scripts.mcp_arguments import ParsedStartRequest
    from scripts.monitor_models import SessionRequest
    from scripts.state_io import JsonValue


def test_start_request_defaults_to_five_and_ten_minutes() -> None:
    values: dict[str, JsonValue] = {
        "session_id": "session-1",
        "transcript_path": "C:/sessions/one.jsonl",
        "activation_turn_id": "turn-1",
    }

    parsed = parse_start_request(values, request_id=1)

    assert parsed.request.warning_after_ms == 300_000
    assert parsed.request.critical_after_ms == 600_000


@pytest.mark.parametrize(
    "start",
    [True, False],
)
def test_request_parser_rejects_unknown_properties(
    start: bool,
) -> None:
    values: dict[str, JsonValue] = {
        "session_id": "session-1",
        "unexpected": True,
    }
    if start:
        values["transcript_path"] = "C:/sessions/one.jsonl"

    with pytest.raises(JsonRpcError) as captured:
        _ = _parse_request(start, values)

    assert captured.value.data == "unexpected_argument_unsupported"


@pytest.mark.parametrize("key", ["session_id", "transcript_path"])
def test_start_request_rejects_text_over_schema_limit(key: str) -> None:
    values: dict[str, JsonValue] = {
        "session_id": "session-1",
        "transcript_path": "C:/sessions/one.jsonl",
    }
    values[key] = "x" * 65_537

    with pytest.raises(JsonRpcError) as captured:
        _ = parse_start_request(values, request_id=1)

    assert captured.value.data == f"{key}_invalid"


def _parse_request(
    start: bool,
    values: dict[str, JsonValue],
) -> ParsedStartRequest | SessionRequest:
    if start:
        return parse_start_request(values, request_id=1)
    return parse_session_request(values, request_id=1)
