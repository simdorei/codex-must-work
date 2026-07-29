"""Dispatch initialized CMW MCP requests to daemon controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from scripts.mcp_arguments import (
    parse_session_request,
    parse_start_request,
)
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
    AuthorizedSession,
    notification_setup_success,
    require_authorized,
    tool_error,
    tool_success,
)
from scripts.mcp_threshold_settings import call_threshold_settings
from scripts.mcp_tool_descriptors import control_tool_descriptors
from scripts.monitor_models import DaemonServiceError
from scripts.state import StateError, state_root
from scripts.threshold_settings import ThresholdSettingsStore
from scripts.work_on_activation import (
    ActivationAuthorizer,
    ActivationIdentity,
    ActivationTicketError,
)

if TYPE_CHECKING:
    from scripts.notification_setup import NotificationSetupLauncher

_STATE_UNAVAILABLE = "monitoring_state_unavailable"


@final
class McpServer:
    """Stateful MCP dispatcher for one Codex-owned daemon process."""

    def __init__(
        self,
        service: DaemonBackend,
        control_key: bytes,
        *,
        activation_tickets: ActivationAuthorizer,
        notification_setup: NotificationSetupLauncher | None = None,
        threshold_settings: ThresholdSettingsStore | None = None,
    ) -> None:
        """Create an uninitialized MCP session around the daemon."""
        self._service = service
        self._control_key = control_key
        self._activation_tickets = activation_tickets
        self._notification_setup = notification_setup
        self._threshold_settings = threshold_settings or ThresholdSettingsStore(state_root())
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
                "Use cmw.work_on only after explicit $work-on invocation. "
                "Use cmw.complete after verified "
                "completion and cmw.stop for a manual shutdown. Never request or pass a "
                "webhook URL in chat or tool arguments."
            ),
        }

    def _call_tool(self, request: JsonRpcRequest) -> JsonObject:
        name = request.params.get("name")
        arguments = request.params.get("arguments", {})
        if type(name) is not str or type(arguments) is not dict:
            raise JsonRpcError(-32600, "Invalid Request", request_id=request.request_id)
        if name not in {
            "cmw.work_on",
            "cmw.stop",
            "cmw.status",
            "cmw.complete",
            "cmw.settings",
            "cmw.notifications.setup",
        }:
            raise JsonRpcError(
                -32600,
                "Invalid Request",
                request_id=request.request_id,
            )
        authorized = require_authorized(self._control_key, arguments, request.request_id)
        if name == "cmw.notifications.setup":
            return self._call_notification_setup(arguments, request)
        if name == "cmw.settings":
            return self._call_threshold_settings(arguments, request)
        return self._call_control(name, arguments, request, authorized)

    def _call_notification_setup(
        self,
        arguments: JsonObject,
        request: JsonRpcRequest,
    ) -> JsonObject:
        allowed = {"session_id"}
        if not set(arguments).issubset(allowed):
            raise JsonRpcError(-32602, "Invalid params", request_id=request.request_id)
        if self._notification_setup is None:
            raise JsonRpcError(-32600, "Invalid Request", request_id=request.request_id)
        try:
            launch = self._notification_setup.start()
        except (OSError, StateError):
            return tool_error(_STATE_UNAVAILABLE)
        return notification_setup_success(launch)

    def _call_threshold_settings(
        self,
        arguments: JsonObject,
        request: JsonRpcRequest,
    ) -> JsonObject:
        return call_threshold_settings(
            self._threshold_settings,
            arguments,
            request.request_id,
        )

    def _call_control(
        self,
        name: str,
        arguments: JsonObject,
        request: JsonRpcRequest,
        authorized: AuthorizedSession,
    ) -> JsonObject:
        try:
            if name == "cmw.work_on":
                parsed = parse_start_request(arguments, request.request_id)
                self._activation_tickets.consume(
                    ActivationIdentity(
                        authorized.session_id,
                        parsed.activation_turn_id,
                        str(parsed.request.transcript_path),
                    ),
                    authorized.capability,
                )
                result = self._service.start(parsed.request)
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
        except ActivationTicketError as error:
            return tool_error(error.reason_code)
        return tool_success(result)
