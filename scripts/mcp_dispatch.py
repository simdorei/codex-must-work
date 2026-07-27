"""Dispatch initialized CMW MCP requests to daemon controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from scripts.daemon_models import DaemonServiceError
from scripts.mcp_arguments import parse_session_request, parse_start_request
from scripts.mcp_protocol import (
    DaemonBackend,
    JsonObject,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    decode_request,
    error_response,
    negotiate_protocol,
    result_response,
)
from scripts.mcp_server_tools import (
    notification_setup_success,
    reject_goal_companion,
    require_authorized,
    tool_error,
    tool_success,
)
from scripts.mcp_tool_descriptors import control_tool_descriptors

if TYPE_CHECKING:
    from scripts.notification_setup import NotificationSetupLauncher


@final
class McpServer:
    """Stateful MCP dispatcher for one Codex-owned daemon process."""

    def __init__(
        self,
        service: DaemonBackend,
        control_key: bytes,
        *,
        notification_setup: NotificationSetupLauncher | None = None,
    ) -> None:
        """Create an uninitialized MCP session around the daemon."""
        self._service = service
        self._control_key = control_key
        self._notification_setup = notification_setup
        self._initialize_received = False
        self._ready = False

    def handle_line(self, raw_line: str) -> JsonRpcResponse | None:
        """Handle one line and respond only when JSON-RPC requires it."""
        request: JsonRpcRequest | None = None
        try:
            request = decode_request(raw_line)
            result = self._dispatch(request)
            if request.is_notification:
                return None
            if request.request_id is None:
                return error_response(JsonRpcError(-32600, "Invalid Request"))
            return result_response(request.request_id, result)
        except JsonRpcError as error:
            if request is not None and request.is_notification:
                return None
            return error_response(error)

    def _dispatch(self, request: JsonRpcRequest) -> JsonObject:
        if request.method == "initialize":
            return self._initialize(request)
        if request.method == "notifications/initialized":
            if not self._initialize_received:
                raise JsonRpcError(-32002, "Server not initialized")
            self._ready = True
            return {}
        if request.method == "ping":
            return {}
        if not self._ready:
            raise JsonRpcError(-32002, "Server not initialized", request_id=request.request_id)
        if request.method == "tools/list":
            return {
                "tools": control_tool_descriptors(
                    include_notification_setup=self._notification_setup is not None
                )
            }
        if request.method == "tools/call":
            return self._call_tool(request)
        raise JsonRpcError(-32600, "Invalid Request", request_id=request.request_id)

    def _initialize(self, request: JsonRpcRequest) -> JsonObject:
        if request.is_notification or request.request_id is None:
            raise JsonRpcError(-32600, "Initialize must be a request")
        if self._initialize_received:
            raise JsonRpcError(-32600, "Already initialized", request_id=request.request_id)
        requested = request.params.get("protocolVersion")
        if type(requested) is not str or not requested:
            raise JsonRpcError(-32602, "Invalid params", request_id=request.request_id)
        self._initialize_received = True
        protocol = negotiate_protocol(requested)
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codex-must-work", "version": "0.2.0"},
            "instructions": (
                "Use cmw.start only after explicit opt-in. Use cmw.complete after verified "
                "completion and cmw.stop for a manual shutdown. When SessionStart context "
                "codex_must_work_notifications.action is offer_setup, briefly offer Discord "
                "alerts. If accepted, call cmw.notifications.setup; never request or pass a "
                "webhook URL in chat or tool arguments. After the setup page saves successfully, "
                "recommend restarting the Codex app once."
            ),
        }

    def _call_tool(self, request: JsonRpcRequest) -> JsonObject:
        name = request.params.get("name")
        arguments = request.params.get("arguments", {})
        if type(name) is not str or type(arguments) is not dict:
            raise JsonRpcError(-32600, "Invalid Request", request_id=request.request_id)
        if name == "cmw.notifications.setup":
            return self._call_notification_setup(arguments, request)
        if name not in {"cmw.start", "cmw.stop", "cmw.status", "cmw.complete"}:
            raise JsonRpcError(
                -32600,
                "Invalid Request",
                request_id=request.request_id,
            )
        require_authorized(self._control_key, arguments, request.request_id)
        if name == "cmw.start":
            reject_goal_companion(arguments, request.request_id)
        return self._call_control(name, arguments, request)

    def _call_notification_setup(
        self,
        arguments: JsonObject,
        request: JsonRpcRequest,
    ) -> JsonObject:
        if arguments:
            raise JsonRpcError(-32602, "Invalid params", request_id=request.request_id)
        if self._notification_setup is None:
            raise JsonRpcError(-32600, "Invalid Request", request_id=request.request_id)
        return notification_setup_success(self._notification_setup.start())

    def _call_control(
        self,
        name: str,
        arguments: JsonObject,
        request: JsonRpcRequest,
    ) -> JsonObject:
        try:
            if name == "cmw.start":
                result = self._service.start(parse_start_request(arguments, request.request_id))
            elif name == "cmw.stop":
                result = self._service.stop(parse_session_request(arguments, request.request_id))
            elif name == "cmw.status":
                result = self._service.status(parse_session_request(arguments, request.request_id))
            elif name == "cmw.complete":
                result = self._service.complete(
                    parse_session_request(arguments, request.request_id)
                )
            else:
                raise AssertionError
        except DaemonServiceError as error:
            return tool_error(error.reason_code)
        return tool_success(result)
