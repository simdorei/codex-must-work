from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scripts.mcp_protocol import JsonObject, JsonRpcResponse
    from scripts.state_io import JsonValue
    from scripts.threshold_settings import ThresholdSettingsStore
    from scripts.work_on_activation import ActivationIdentity, ActivationTicketError

from scripts.control_capability import derive_control_capability
from scripts.mcp_server import McpServer
from scripts.monitor_models import (
    DaemonServiceError,
    SessionId,
    SessionRequest,
    StartRequest,
    ToolResult,
)


@final
class FakeDaemon:
    def __init__(self, *, failure: DaemonServiceError | None = None) -> None:
        self.failure = failure
        self.started: StartRequest | None = None
        self.session_request: SessionRequest | None = None
        self.closed = False

    def start(self, request: StartRequest) -> ToolResult:
        self.started = request
        return self._result(request.session_id, "started")

    def stop(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        return self._result(request.session_id, "stopped")

    def status(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        if self.failure is not None:
            raise self.failure
        return self._result(request.session_id, "active")

    def complete(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        return self._result(request.session_id, "completed")

    def close(self) -> None:
        self.closed = True

    @staticmethod
    def _result(session_id: SessionId, status: str) -> ToolResult:
        return ToolResult(session_id=session_id, status=status)


@final
class FakeActivationTickets:
    def __init__(self, failure: ActivationTicketError | None = None) -> None:
        self.failure = failure
        self.consumed: ActivationIdentity | None = None

    def consume(self, identity: ActivationIdentity, capability: str) -> None:
        _ = capability
        if self.failure is not None:
            raise self.failure
        self.consumed = identity


def ready_server(
    daemon: FakeDaemon,
    *,
    threshold_settings: ThresholdSettingsStore | None = None,
    activation_tickets: FakeActivationTickets | None = None,
) -> McpServer:
    server = McpServer(
        daemon,
        control_key(),
        activation_tickets=activation_tickets or FakeActivationTickets(),
        threshold_settings=threshold_settings,
    )
    _ = server.handle_line(request(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert server.handle_line(notification("notifications/initialized")) is None
    return server


def control_key() -> bytes:
    return hashlib.sha256(b"cmw-public-test-key").digest()


def capability(session_id: str) -> str:
    return derive_control_capability(control_key(), session_id)


def request(request_id: int | str, method: str, params: Mapping[str, JsonValue]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def notification(method: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method})


def success_result(response: JsonRpcResponse | None) -> JsonObject:
    assert response is not None
    assert "result" in response
    result = response["result"]
    assert isinstance(result, dict)
    return result
