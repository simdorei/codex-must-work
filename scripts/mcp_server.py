"""Serve Codex Must Work controls over dependency-free MCP STDIO."""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from scripts.notification_setup import NotificationSetupLauncher
    from scripts.work_on_activation import ActivationAuthorizer

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.control_capability import ControlKeyError, provision_control_key
from scripts.daemon_control_endpoint import ControlEndpoint
from scripts.mcp_dispatch import McpServer
from scripts.mcp_protocol import DaemonBackend, StdioStreams
from scripts.mcp_stdio import serve_lines
from scripts.notification_setup import (
    NotificationSetupCoordinator,
)

__all__ = ("McpServer", "run_server")


def run_server(
    service: DaemonBackend,
    streams: StdioStreams,
    control_key: bytes,
    *,
    activation_tickets: ActivationAuthorizer,
    notification_setup: NotificationSetupLauncher | None = None,
) -> None:
    """Run until EOF while reserving stdout for MCP protocol messages."""
    serve_lines(
        McpServer(
            service,
            control_key,
            activation_tickets=activation_tickets,
            notification_setup=notification_setup,
        ),
        streams,
    )


def main(argv: list[str] | None = None) -> int:
    """Create the resident daemon service and expose it over STDIO."""
    plugin_data = configure_plugin_data(argv, cwd=Path.cwd(), environ=os.environ)
    from scripts.notification_daemon import NotificationDaemonService  # noqa: PLC0415
    from scripts.state import state_root  # noqa: PLC0415
    from scripts.work_on_activation import ActivationTicketStore  # noqa: PLC0415

    try:
        control_key = provision_control_key(plugin_data, state_root())
    except ControlKeyError as error:
        _ = sys.stderr.write(f"{error.reason_code}\n")
        return 2

    service = NotificationDaemonService(notification_plugin_data=plugin_data)
    notification_setup = NotificationSetupCoordinator(plugin_data)
    activation_tickets = ActivationTicketStore(plugin_data, control_key)
    try:
        endpoint_factory = partial(McpServer, activation_tickets=activation_tickets)
        with ControlEndpoint(service, control_key, plugin_data, endpoint_factory):
            run_server(
                service,
                StdioStreams(sys.stdin, sys.stdout, sys.stderr),
                control_key,
                activation_tickets=activation_tickets,
                notification_setup=notification_setup,
            )
    except OSError:
        return 2
    finally:
        notification_setup.close()
        service.close()
    return 0


def configure_plugin_data(
    argv: list[str] | None,
    *,
    cwd: Path,
    environ: MutableMapping[str, str],
) -> Path:
    """Resolve the required plugin data root before daemon construction."""
    parser = argparse.ArgumentParser(prog="codex-must-work-mcp")
    _ = parser.add_argument("--plugin-data")
    namespace = _PluginDataArgs()
    _ = parser.parse_args(argv, namespace=namespace)
    configured = namespace.plugin_data or environ.get("PLUGIN_DATA")
    if not configured:
        parser.error("--plugin-data or PLUGIN_DATA is required")
    value = Path(configured)
    resolved = (value if value.is_absolute() else cwd / value).resolve()
    environ["PLUGIN_DATA"] = str(resolved)
    return resolved


class _PluginDataArgs(argparse.Namespace):
    plugin_data: str

    def __init__(self) -> None:
        """Initialize the argparse destination for strict typing."""
        super().__init__()
        self.plugin_data = ""


if __name__ == "__main__":
    raise SystemExit(main())
