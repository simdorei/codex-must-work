"""Typed argument parsing for activation commands."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path

from scripts.activation_error import ActivationError
from scripts.setup import MessagePreset


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


def parse_cli(argv: list[str] | None) -> CliArgs:
    """Parse one complete activation command."""
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
