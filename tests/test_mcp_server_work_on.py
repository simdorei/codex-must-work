from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_never

import pytest

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

from scripts.monitor_models import DaemonServiceError
from scripts.work_on_activation import ActivationTicketError
from tests.mcp_server_test_support import (
    FakeActivationTickets,
    FakeDaemon,
    capability,
    ready_server,
    request,
    success_result,
)

type _ControlToolName = Literal["cmw.work_on", "cmw.stop", "cmw.status", "cmw.complete"]
type _CapabilityCase = Literal["copied-wrong", "malformed", "non-string"]


def test_tools_call_parses_start_arguments_before_daemon() -> None:
    # Given
    daemon = FakeDaemon()
    server = ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.work_on",
        "arguments": {
            "session_id": "session-1",
            "control_capability": capability("session-1"),
            "transcript_path": "C:/sessions/one.jsonl",
            "activation_turn_id": "turn-1",
        },
    }

    # When
    response = server.handle_line(request(3, "tools/call", params))

    # Then
    assert daemon.started is not None
    assert daemon.started.session_id == "session-1"
    assert daemon.started.warning_after_ms == 300_000
    assert daemon.started.critical_after_ms == 600_000
    structured = success_result(response).get("structuredContent")
    assert isinstance(structured, dict)
    assert structured["status"] == "started"


def test_direct_work_on_without_prompt_ticket_fails_before_daemon_start() -> None:
    daemon = FakeDaemon()
    tickets = FakeActivationTickets(ActivationTicketError("work_on_authorization_required"))
    server = ready_server(daemon, activation_tickets=tickets)
    arguments: dict[str, JsonValue] = {
        "session_id": "session-direct",
        "control_capability": capability("session-direct"),
        "transcript_path": "C:/sessions/direct.jsonl",
        "activation_turn_id": "turn-direct",
    }

    response = server.handle_line(
        request(36, "tools/call", {"name": "cmw.work_on", "arguments": arguments})
    )

    result = success_result(response)
    assert result["structuredContent"] == {"error": "work_on_authorization_required"}
    assert daemon.started is None


def test_start_rejects_legacy_restart_options_before_daemon_mutation() -> None:
    daemon = FakeDaemon()
    server = ready_server(daemon)
    arguments: dict[str, JsonValue] = {
        "session_id": "session-legacy",
        "control_capability": capability("session-legacy"),
        "transcript_path": "C:/sessions/legacy.jsonl",
        "auto_restart": True,
    }

    response = server.handle_line(
        request(30, "tools/call", {"name": "cmw.work_on", "arguments": arguments})
    )

    assert response is not None
    assert "error" in response
    error = response["error"]
    assert error["code"] == -32602
    assert error.get("data") == "legacy_restart_option_unsupported"
    assert daemon.started is None


def test_start_rejects_goal_companion_as_a_legacy_restart_option() -> None:
    # Given
    daemon = FakeDaemon()
    server = ready_server(daemon)
    arguments: dict[str, JsonValue] = {
        "session_id": "goal-session",
        "control_capability": capability("goal-session"),
        "goal_companion": True,
        "warning_after_ms": 0,
        "transcript_path": "C:/sessions/goal.jsonl",
    }

    # When
    response = server.handle_line(
        request(13, "tools/call", {"name": "cmw.work_on", "arguments": arguments})
    )

    # Then
    assert response is not None
    assert "error" in response
    error = response["error"]
    assert error["code"] == -32602
    assert error.get("data") == "legacy_restart_option_unsupported"
    assert daemon.started is None


@pytest.mark.parametrize("name", ["cmw.work_on", "cmw.stop", "cmw.status", "cmw.complete"])
def test_authenticated_control_rejects_unknown_properties_before_daemon_mutation(
    name: _ControlToolName,
) -> None:
    daemon = FakeDaemon()
    server = ready_server(daemon)
    arguments: dict[str, JsonValue] = {
        "session_id": "strict-session",
        "control_capability": capability("strict-session"),
        "unexpected": True,
    }
    match name:
        case "cmw.work_on":
            arguments["transcript_path"] = "C:/sessions/strict.jsonl"
        case "cmw.stop" | "cmw.status" | "cmw.complete":
            pass
        case _:
            assert_never(name)

    response = server.handle_line(request(14, "tools/call", {"name": name, "arguments": arguments}))

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32602
    assert response["error"].get("data") == "unexpected_argument_unsupported"
    assert daemon.started is None
    assert daemon.session_request is None


def test_authenticated_overlong_transcript_is_rejected_before_daemon_mutation() -> None:
    daemon = FakeDaemon()
    server = ready_server(daemon)
    session_id = "strict-session"

    response = server.handle_line(
        request(
            15,
            "tools/call",
            {
                "name": "cmw.work_on",
                "arguments": {
                    "session_id": session_id,
                    "control_capability": capability(session_id),
                    "transcript_path": "x" * 65_537,
                },
            },
        )
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32600
    assert daemon.started is None


def test_tools_call_returns_exact_expected_daemon_failure() -> None:
    # Given
    daemon = FakeDaemon(failure=DaemonServiceError("session_not_found"))
    server = ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.status",
        "arguments": {
            "session_id": "missing",
            "control_capability": capability("missing"),
        },
    }

    # When
    response = server.handle_line(request(4, "tools/call", params))

    # Then
    result = success_result(response)
    assert result["isError"] is True
    assert result["structuredContent"] == {"error": "session_not_found"}


def test_tools_call_rejects_missing_capability_without_daemon_mutation() -> None:
    # Given
    daemon = FakeDaemon()
    server = ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.stop",
        "arguments": {"session_id": "session-a"},
    }

    # When
    response = server.handle_line(request("bad-args", "tools/call", params))

    # Then
    assert response is not None
    assert response["id"] == "bad-args"
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.session_request is None


@pytest.mark.parametrize("name", ["cmw.work_on", "cmw.stop", "cmw.status", "cmw.complete"])
@pytest.mark.parametrize("capability_case", ["copied-wrong", "malformed", "non-string"])
def test_cross_session_control_is_unauthorized_before_daemon_mutation(
    name: _ControlToolName,
    capability_case: _CapabilityCase,
) -> None:
    # Given
    daemon = FakeDaemon()
    server = ready_server(daemon)
    match capability_case:
        case "copied-wrong":
            control_capability: JsonValue = capability("session-a")
        case "malformed":
            control_capability = "malformed"
        case "non-string":
            control_capability = 7
        case _:
            assert_never(capability_case)
    arguments: dict[str, JsonValue] = {
        "session_id": "session-b",
        "control_capability": control_capability,
    }
    match name:
        case "cmw.work_on":
            arguments["transcript_path"] = "C:/sessions/b.jsonl"
        case "cmw.stop" | "cmw.status" | "cmw.complete":
            pass
        case _:
            assert_never(name)

    # When
    response = server.handle_line(request(11, "tools/call", {"name": name, "arguments": arguments}))

    # Then
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.started is None
    assert daemon.session_request is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"session_id": 7, "control_capability": "malformed"},
        {"session_id": "x" * 65_537, "control_capability": "x" * 43},
        {"session_id": "session-a", "control_capability": "x" * 65_537},
    ],
    ids=("missing", "non-string-session", "overlong-session", "overlong-capability"),
)
def test_malformed_auth_fields_are_uniformly_unauthorized(
    arguments: dict[str, JsonValue],
) -> None:
    # Given
    daemon = FakeDaemon()
    server = ready_server(daemon)

    # When
    response = server.handle_line(
        request(
            12,
            "tools/call",
            {"name": "cmw.status", "arguments": arguments},
        )
    )

    # Then
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.session_request is None
