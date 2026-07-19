"""Typed command-line boundary for Codex Must Work activation."""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
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
from scripts.manager_reuse import reuse_ready_manager
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
from scripts.setup_cli_args import CliArgs, Command, parse_cli
from scripts.state import config_path, load_state, runtime_path, state_root
from scripts.state_io import StateError


def main(argv: list[str] | None = None) -> int:
    """Apply one explicit enable or disable request."""
    try:
        args = parse_cli(argv)
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
    runtime_file = runtime_path(root, args.session_id)
    if reuse_ready_manager(root, runtime_file, request):
        _ = sys.stdout.write("Codex Must Work already enabled; existing resident manager reused.\n")
        return 0
    result = enable_session(root, request, report)
    if args.observe_only:
        _ = sys.stdout.write("Codex Must Work observe-only enabled; no warnings or restarts.\n")
        return 0
    if result.effective_auto_restart:
        try:
            _ = launch_manager(root, runtime_file)
        except (ManagerLaunchError, OSError, StateError):
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


if __name__ == "__main__":
    raise SystemExit(main())
