"""Typed command-line boundary for Codex Must Work activation."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.codex_executable import (
    CodexExecutableError,
    resolve_codex_executable,
)
from scripts.control import CapabilityReport, current_capabilities
from scripts.durations import (
    DurationParseError,
    Milliseconds,
    ThresholdOrderError,
    parse_duration_ms,
)
from scripts.manager_launch import ManagerLaunchError, launch_manager
from scripts.setup import (
    ActivationError,
    ActivationRequest,
    MessagePreset,
    Settings,
    complete_session,
    disable_session,
    enable_session,
    request_session_shutdown,
)
from scripts.state import config_path, load_state, runtime_path, state_root
from scripts.state_io import StateError


@unique
class Command(StrEnum):
    """Supported explicit configuration commands."""

    ENABLE = "enable"
    DISABLE = "disable"


@dataclass(frozen=True, slots=True)
class CliArgs:
    """Parsed command-line values before activation validation."""

    command: Command
    session_id: str
    transcript_path: Path | None
    warning: str | None
    restart: str | None
    message_preset: str | None
    auto_restart: bool
    goal_companion: bool
    observe_only: bool
    completed: bool
    permission_mode: str | None


class _Namespace(argparse.Namespace):
    """Mutable parser target whose fields stay statically typed."""

    command: str | None
    session_id: str | None
    transcript_path: str | None
    warning: str | None
    restart: str | None
    message_preset: str | None
    auto_restart: bool
    goal_companion: bool
    observe_only: bool
    completed: bool
    permission_mode: str | None

    def __init__(self) -> None:
        super().__init__()
        self.command = None
        self.session_id = None
        self.transcript_path = None
        self.warning = None
        self.restart = None
        self.message_preset = None
        self.auto_restart = False
        self.goal_companion = False
        self.observe_only = False
        self.completed = False
        self.permission_mode = None


def main(argv: list[str] | None = None) -> int:
    """Apply one explicit enable or disable request."""
    try:
        args = _parse_cli(argv)
        root = state_root()
        handlers = {Command.ENABLE: _enable, Command.DISABLE: _disable}
        return handlers[args.command](root, args)
    except (
        ActivationError,
        CodexExecutableError,
        DurationParseError,
        ManagerLaunchError,
        StateError,
        ThresholdOrderError,
    ) as error:
        _ = sys.stderr.write(f"{error}\n")
        return 2


def _disable(root: Path, args: CliArgs) -> int:
    if args.completed:
        complete_session(root, args.session_id, datetime.now(UTC))
        _ = sys.stdout.write("Codex Must Work completed; final heartbeat recorded.\n")
        return 0
    request_session_shutdown(root, args.session_id, interrupt_active=True)
    _ = sys.stdout.write("Codex Must Work disabled for the current task.\n")
    return 0


def _enable(root: Path, args: CliArgs) -> int:
    if args.transcript_path is None:
        raise ActivationError(reason_code="transcript_path_missing")
    settings = _settings_from_args(args, root)
    report = _capability_report(settings, args)
    request = ActivationRequest(
        session_id=args.session_id,
        transcript_path=args.transcript_path,
        settings=settings,
        observe_only=args.observe_only,
        permission_mode=args.permission_mode,
        now=datetime.now(UTC),
        goal_companion=args.goal_companion,
    )
    result = enable_session(root, request, report)
    if args.observe_only:
        _ = sys.stdout.write("Codex Must Work observe-only enabled; no warnings or restarts.\n")
        return 0
    if result.effective_auto_restart:
        try:
            _ = launch_manager(root, runtime_path(root, args.session_id))
        except (ManagerLaunchError, OSError):
            disable_session(root, args.session_id)
            raise
    warning = result.warning_delivery_active
    restart = result.effective_auto_restart
    continuation = result.stop_continuation_active
    message = (
        "Codex Must Work enabled: "
        f"live_warning={warning}, stop_continuation={continuation}, restart={restart}, "
        f"goal_companion={args.goal_companion}\n"
    )
    _ = sys.stdout.write(message)
    return 0


def _capability_report(settings: Settings, args: CliArgs) -> CapabilityReport:
    if not settings.auto_restart_requested_by_user or args.observe_only:
        return current_capabilities("unavailable")
    if args.permission_mode not in {"dontAsk", "bypassPermissions"}:
        raise ActivationError(reason_code="managed_mode_requires_approval_free_permission")
    executable = resolve_codex_executable()
    fingerprint = hashlib.sha256(executable.read_bytes()).hexdigest()
    return CapabilityReport(
        warning_delivery_ready=False,
        auto_restart_ready=True,
        reason_code="managed_owner_ready",
        evidence_fingerprint=fingerprint,
        stop_continuation_ready=False,
    )


def _settings_from_args(args: CliArgs, root: Path) -> Settings:
    if args.warning is None and args.restart is None and args.message_preset is None:
        return _settings_from_state(root)
    if args.warning is None or args.restart is None or args.message_preset is None:
        raise ActivationError(reason_code="warning_restart_message_preset_required_together")
    return Settings(
        warning_after_ms=parse_duration_ms(args.warning),
        restart_after_ms=parse_duration_ms(args.restart),
        message_preset=MessagePreset(args.message_preset),
        auto_restart_requested_by_user=args.auto_restart,
    )


def _settings_from_state(root: Path) -> Settings:
    path = config_path(root)
    if not path.exists():
        raise ActivationError(reason_code="configuration_missing")
    values = load_state(root, path).values
    warning = values.get("warning_after_ms")
    restart = values.get("restart_after_ms")
    preset = values.get("message_preset")
    auto_restart = values.get("auto_restart_requested_by_user")
    if (
        type(warning) is not int
        or type(restart) is not int
        or type(preset) is not str
        or type(auto_restart) is not bool
    ):
        raise ActivationError(reason_code="configuration_invalid")
    try:
        message_preset = MessagePreset(preset)
    except ValueError as error:
        raise ActivationError(reason_code="configuration_invalid") from error
    return Settings(
        warning_after_ms=Milliseconds(warning),
        restart_after_ms=Milliseconds(restart),
        message_preset=message_preset,
        auto_restart_requested_by_user=auto_restart,
    )


def _parse_cli(argv: list[str] | None) -> CliArgs:
    namespace = _Namespace()
    _ = _parser().parse_args(argv, namespace=namespace)
    if namespace.command is None or namespace.session_id is None:
        raise ActivationError(reason_code="cli_parse_incomplete")
    return CliArgs(
        command=Command(namespace.command),
        session_id=namespace.session_id,
        transcript_path=(
            Path(namespace.transcript_path) if namespace.transcript_path is not None else None
        ),
        warning=namespace.warning,
        restart=namespace.restart,
        message_preset=namespace.message_preset,
        auto_restart=namespace.auto_restart,
        goal_companion=namespace.goal_companion,
        observe_only=namespace.observe_only,
        completed=namespace.completed,
        permission_mode=namespace.permission_mode,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-must-work")
    commands = parser.add_subparsers(dest="command", required=True)
    enable = commands.add_parser(Command.ENABLE.value)
    _ = enable.add_argument("--session-id", required=True)
    _ = enable.add_argument("--transcript-path", required=True)
    _ = enable.add_argument("--warning")
    _ = enable.add_argument("--restart")
    _ = enable.add_argument(
        "--message-preset",
        choices=tuple(preset.value for preset in MessagePreset),
    )
    _ = enable.add_argument("--auto-restart", action="store_true")
    _ = enable.add_argument("--goal-companion", action="store_true")
    _ = enable.add_argument("--observe-only", action="store_true")
    _ = enable.add_argument(
        "--permission-mode",
        choices=("default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"),
    )
    disable = commands.add_parser(Command.DISABLE.value)
    _ = disable.add_argument("--session-id", required=True)
    _ = disable.add_argument("--completed", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
