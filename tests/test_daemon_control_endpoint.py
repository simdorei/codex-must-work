from __future__ import annotations

import json
import socket
from functools import partial
from pathlib import Path
from typing import Final, Protocol, cast, final

from scripts.daemon_control_endpoint import (
    ControlEndpoint,
    EndpointLocator,
    control_endpoint_path,
)
from scripts.daemon_control_endpoint_auth import (
    encode_client_hello,
    verify_server_proof,
)
from scripts.mcp_server import McpServer
from scripts.monitor_models import SessionRequest, StartRequest, ToolResult
from scripts.private_root import ensure_private_root
from scripts.state_io import JsonValue
from scripts.work_on_activation import ActivationIdentity, ActivationTicketStore

_KEY = b"k" * 32
_TEST_CHALLENGE: Final = "c" * 43
_TEST_TRANSCRIPT: Final = str(Path("C:/rollout.jsonl"))
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, value: str | bytes) -> JsonValue: ...


def _json_loader(value: str | bytes) -> JsonValue:
    return cast("JsonValue", json.loads(value))


_LOAD_JSON: Final[_JsonLoader] = _json_loader


@final
class FakeDaemon:
    def __init__(self, *, crash_status: bool = False) -> None:
        self.calls: list[str] = []
        self.crash_status: bool = crash_status

    def start(self, request: StartRequest) -> ToolResult:
        self.calls.append("start")
        return ToolResult(request.session_id, "started")

    def stop(self, request: SessionRequest) -> ToolResult:
        self.calls.append("stop")
        return ToolResult(request.session_id, "stopped")

    def status(self, request: SessionRequest) -> ToolResult:
        if self.crash_status:
            raise KeyboardInterrupt
        self.calls.append("status")
        return ToolResult(request.session_id, "inactive")

    def complete(self, request: SessionRequest) -> ToolResult:
        self.calls.append("complete")
        return ToolResult(request.session_id, "completed")

    def close(self) -> None:
        return


def request_bytes(
    *,
    tool: str = "cmw.status",
    challenge: str = _TEST_CHALLENGE,
    include_model_capability: bool = False,
) -> bytes:
    arguments: JsonObject = {
        "session_id": "session-a",
    }
    if include_model_capability:
        arguments["control_capability"] = "x" * 43
    if tool == "cmw.work_on":
        arguments["transcript_path"] = _TEST_TRANSCRIPT
        arguments["activation_turn_id"] = "turn-a"
    payload = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments,
            "endpoint_challenge": challenge,
        },
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def exchange(locator: EndpointLocator, request: bytes) -> JsonObject:
    with socket.create_connection(("127.0.0.1", locator.port), timeout=4.0) as connection:
        connection.sendall(encode_client_hello(locator, _TEST_CHALLENGE))
        proof = connection.makefile("rb").readline().decode("utf-8")
        assert verify_server_proof(proof, locator, _TEST_CHALLENGE)
        connection.sendall(request)
        response = connection.makefile("rb").readline()
    assert response
    decoded = _LOAD_JSON(response)
    assert type(decoded) is dict
    return decoded


def _endpoint(
    daemon: FakeDaemon,
    plugin_data: Path,
) -> tuple[ControlEndpoint, ActivationTicketStore]:
    ensure_private_root(plugin_data)
    tickets = ActivationTicketStore(plugin_data, _KEY)
    factory = partial(McpServer, activation_tickets=tickets)
    return ControlEndpoint(daemon, _KEY, plugin_data, factory), tickets


def test_endpoint_routes_all_authenticated_tools_through_shared_daemon(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    plugin_data = tmp_path / "plugin-data"
    endpoint, tickets = _endpoint(daemon, plugin_data)
    identity = ActivationIdentity(
        "session-a",
        "turn-a",
        _TEST_TRANSCRIPT,
    )

    # When
    with endpoint as locator:
        unauthorized = exchange(locator, request_bytes(tool="cmw.work_on"))
        _ = tickets.issue(identity)
        responses = (
            exchange(locator, request_bytes(tool="cmw.work_on")),
            *(
                exchange(locator, request_bytes(tool=tool))
                for tool in ("cmw.status", "cmw.complete", "cmw.stop")
            ),
        )

    # Then
    unauthorized_result = cast("JsonObject", unauthorized["result"])
    assert unauthorized_result["structuredContent"] == {"error": "work_on_authorization_required"}
    authorized_result = cast("JsonObject", responses[0]["result"])
    authorized_content = cast("JsonObject", authorized_result["structuredContent"])
    assert authorized_content["status"] == "started"
    assert daemon.calls == ["start", "status", "complete", "stop"]
    assert all("result" in response for response in responses)
    assert not control_endpoint_path(plugin_data).exists()


def test_endpoint_binds_loopback_and_publishes_only_public_locator(
    tmp_path: Path,
) -> None:
    # Given
    plugin_data = tmp_path / "plugin-data"
    endpoint, _ = _endpoint(FakeDaemon(), plugin_data)

    # When
    with endpoint as locator:
        persisted = _LOAD_JSON(control_endpoint_path(plugin_data).read_text(encoding="utf-8"))

    # Then
    assert locator.port > 0
    assert persisted == {
        "schema_version": 1,
        "pid": locator.pid,
        "process_created_ns": locator.process_created_ns,
        "port": locator.port,
        "endpoint_nonce": locator.endpoint_nonce,
    }
    assert "control_capability" not in repr(persisted)


def test_endpoint_rejects_bad_challenge_and_model_capability_argument(
    tmp_path: Path,
) -> None:
    # Given
    endpoint, _ = _endpoint(FakeDaemon(), tmp_path / "plugin-data")

    # When
    with endpoint as locator:
        challenge_error = exchange(locator, request_bytes(challenge="wrong"))
        capability_error = exchange(locator, request_bytes(include_model_capability=True))

    # Then
    assert challenge_error["error"] == {
        "code": -32001,
        "message": "Unauthorized",
        "data": {"code": "cmw_unauthorized"},
    }
    assert capability_error["error"] == {
        "code": -32602,
        "message": "Invalid params",
        "data": "unexpected_argument_unsupported",
    }


def test_endpoint_rejects_duplicate_and_malformed_requests(tmp_path: Path) -> None:
    # Given
    endpoint, _ = _endpoint(FakeDaemon(), tmp_path / "plugin-data")

    # When
    with endpoint as locator:
        duplicate = exchange(
            locator,
            b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"ping"}\n',
        )
        malformed = exchange(locator, b"{broken\n")

    # Then
    assert duplicate["error"] == {"code": -32600, "message": "Invalid Request"}
    assert malformed["error"] == {"code": -32700, "message": "Parse error"}


def test_endpoint_rejects_oversized_request_without_daemon_mutation(tmp_path: Path) -> None:
    # Given
    daemon = FakeDaemon()
    endpoint, _ = _endpoint(daemon, tmp_path / "plugin-data")

    # When
    with endpoint as locator:
        response = exchange(locator, b"x" * 1_048_577 + b"\n")

    # Then
    assert response["error"] == {"code": -32600, "message": "Invalid Request"}
    assert daemon.calls == []
