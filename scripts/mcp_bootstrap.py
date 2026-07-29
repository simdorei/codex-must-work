"""Start the MCP server behind a fail-closed local import policy."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import TYPE_CHECKING, Final, final, override

if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

ALLOWED_SCRIPTS_MODULES: Final = frozenset(
    {
        "scripts.activity_epoch",
        "scripts.agent_identity",
        "scripts.calibration",
        "scripts.calibration_state",
        "scripts.control_capability",
        "scripts.daemon_control_endpoint",
        "scripts.daemon_control_endpoint_auth",
        "scripts.daemon_control_endpoint_connection",
        "scripts.daemon_control_endpoint_identity",
        "scripts.daemon_control_endpoint_models",
        "scripts.daemon_scheduler",
        "scripts.discord_notifications",
        "scripts.discord_webhook",
        "scripts.durations",
        "scripts.event_source",
        "scripts.marketplace_identity",
        "scripts.mcp_arguments",
        "scripts.mcp_bootstrap",
        "scripts.mcp_dispatch",
        "scripts.mcp_limits",
        "scripts.mcp_protocol",
        "scripts.mcp_server",
        "scripts.mcp_server_tools",
        "scripts.mcp_stdio",
        "scripts.mcp_threshold_settings",
        "scripts.mcp_tool_descriptors",
        "scripts.monitor_diagnostics",
        "scripts.monitor_models",
        "scripts.monitor_state",
        "scripts.monitor_target",
        "scripts.notification_config",
        "scripts.notification_daemon",
        "scripts.notification_session",
        "scripts.notification_setup",
        "scripts.notification_setup_http",
        "scripts.notification_setup_page",
        "scripts.notifications",
        "scripts.private_root",
        "scripts.private_root_windows",
        "scripts.stall_detector",
        "scripts.state",
        "scripts.state_io",
        "scripts.thread_title",
        "scripts.threshold_settings",
        "scripts.watcher_actions",
        "scripts.watcher_batch",
        "scripts.watcher_commit",
        "scripts.watcher_completion",
        "scripts.watcher_context",
        "scripts.watcher_diagnostics",
        "scripts.watcher_engine",
        "scripts.watcher_events",
        "scripts.watcher_failure",
        "scripts.watcher_heartbeat",
        "scripts.watcher_notifications",
        "scripts.watcher_progress",
        "scripts.watcher_recovery",
        "scripts.watcher_runtime",
        "scripts.watcher_source",
        "scripts.work_on_activation",
        "scripts.work_on_activation_record",
        "scripts.work_on_identity",
        "scripts.work_on_token",
    }
)
_DENIED_REASON: Final = "scripts_import_denied"

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass(frozen=True, slots=True)
class ScriptsImportDeniedError(ImportError):
    """Expose only the stable reason and rejected module identity."""

    module_name: str
    reason_code: str = _DENIED_REASON

    @override
    def __str__(self) -> str:
        return f"{self.reason_code}: {self.module_name}"


@final
class _ScriptsImportPolicy(MetaPathFinder):
    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        """Reject every unaudited child of the local scripts package."""
        _ = path, target
        if fullname.startswith("scripts.") and fullname not in ALLOWED_SCRIPTS_MODULES:
            raise ScriptsImportDeniedError(fullname)
        return None


def install_import_policy() -> None:
    """Install the MCP-local policy after rejecting unsafe preloaded modules."""
    denied = sorted(
        name
        for name in sys.modules
        if name.startswith("scripts.") and name not in ALLOWED_SCRIPTS_MODULES
    )
    if denied:
        raise ScriptsImportDeniedError(denied[0])
    sys.meta_path.insert(0, _ScriptsImportPolicy())


def main(argv: list[str] | None = None) -> int:
    """Install the policy before loading any MCP implementation module."""
    install_import_policy()
    from scripts.mcp_server import main as serve_mcp  # noqa: PLC0415

    return serve_mcp(argv)


if __name__ == "__main__":
    raise SystemExit(main())
