from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING, Final, Protocol, cast, final

from scripts.control_capability import derive_control_capability
from scripts.daemon_control_endpoint import (
    ControlEndpoint,
    EndpointLocator,
    control_endpoint_path,
)
from scripts.daemon_control_endpoint_auth import (
    encode_client_hello,
    verify_server_proof,
)
from scripts.daemon_models import SessionRequest, StartRequest, ToolResult
from scripts.mcp_server import McpServer
from scripts.state_io import JsonValue

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"k" * 32
_TEST_CHALLENGE: Final = "c" * 43
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
    capability: str | None = None,
) -> bytes:
    arguments: JsonObject = {
        "session_id": "session-a",
        "control_capability": capability or derive_control_capability(_KEY, "session-a"),
    }
    if tool == "cmw.start":
        arguments["transcript_path"] = "C:/rollout.jsonl"
        arguments["goal_companion"] = False
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


def test_endpoint_routes_all_authenticated_tools_through_shared_daemon(
    tmp_path: Path,
) -> None:
    # Given
    daemon = FakeDaemon()
    endpoint = ControlEndpoint(daemon, _KEY, tmp_path, McpServer)

    # When
    with endpoint as locator:
        responses = tuple(
            exchange(locator, request_bytes(tool=tool))
            for tool in ("cmw.start", "cmw.status", "cmw.complete", "cmw.stop")
        )

    # Then
    assert daemon.calls == ["start", "status", "complete", "stop"]
    assert all("result" in response for response in responses)
    assert not control_endpoint_path(tmp_path).exists()


def test_endpoint_binds_loopback_and_publishes_only_public_locator(
    tmp_path: Path,
) -> None:
    # Given
    endpoint = ControlEndpoint(FakeDaemon(), _KEY, tmp_path, McpServer)

    # When
    with endpoint as locator:
        persisted = _LOAD_JSON(control_endpoint_path(tmp_path).read_text(encoding="utf-8"))

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


def test_endpoint_returns_uniform_unauthorized_for_challenge_or_capability(
    tmp_path: Path,
) -> None:
    # Given
    endpoint = ControlEndpoint(FakeDaemon(), _KEY, tmp_path, McpServer)

    # When
    with endpoint as locator:
        challenge_error = exchange(locator, request_bytes(challenge="wrong"))
        capability_error = exchange(locator, request_bytes(capability="x" * 43))

    # Then
    assert challenge_error["error"] == capability_error["error"]
    assert challenge_error["error"] == {
        "code": -32001,
        "message": "Unauthorized",
        "data": {"code": "cmw_unauthorized"},
    }


def test_endpoint_rejects_duplicate_and_malformed_requests(tmp_path: Path) -> None:
    # Given
    endpoint = ControlEndpoint(FakeDaemon(), _KEY, tmp_path, McpServer)

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
    endpoint = ControlEndpoint(daemon, _KEY, tmp_path, McpServer)

    # When
    with endpoint as locator:
        response = exchange(locator, b"x" * 1_048_577 + b"\n")

    # Then
    assert response["error"] == {"code": -32600, "message": "Invalid Request"}
    assert daemon.calls == []
