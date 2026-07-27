"""Normalize daemon failures without hiding unexpected programming errors."""

from __future__ import annotations

from typing import Final, Protocol, TypeGuard

from scripts.app_server_protocol import AppServerProtocolError
from scripts.codex_executable import CodexExecutableError
from scripts.daemon_models import DaemonServiceError
from scripts.durations import ThresholdOrderError, ThresholdValueError
from scripts.goal_control import GoalControlError
from scripts.manager_runtime_values import ManagerRuntimeError
from scripts.setup import ActivationError
from scripts.state import StateError

type ServiceError = (
    ActivationError
    | AppServerProtocolError
    | CodexExecutableError
    | DaemonServiceError
    | GoalControlError
    | ManagerRuntimeError
    | OSError
    | StateError
    | ThresholdOrderError
    | ThresholdValueError
)

SERVICE_ERRORS: Final = (
    ActivationError,
    AppServerProtocolError,
    CodexExecutableError,
    DaemonServiceError,
    GoalControlError,
    ManagerRuntimeError,
    OSError,
    StateError,
    ThresholdOrderError,
    ThresholdValueError,
)
_MANAGER_DAEMON_REASONS: Final = frozenset(
    {"activation_turn_aborted", "activation_turn_superseded"}
)


class _ReasonCodeError(Protocol):
    reason_code: str


def error_reason(error: ServiceError) -> str:
    """Return the stable reason carried by an expected daemon failure."""
    if _has_reason_code(error):
        return error.reason_code
    return str(error)


def manager_failure_reason(error: ServiceError) -> str:
    """Map infrastructure failures into the manager's fixed reason allowlist."""
    if _has_manager_reason(error):
        return error.reason_code
    return "app_server_failed"


def _has_reason_code(error: ServiceError) -> TypeGuard[_ReasonCodeError]:
    return isinstance(
        error,
        (ActivationError, CodexExecutableError, GoalControlError, ManagerRuntimeError),
    )


def _has_manager_reason(error: ServiceError) -> TypeGuard[_ReasonCodeError]:
    return isinstance(error, (CodexExecutableError, GoalControlError, ManagerRuntimeError)) or (
        isinstance(error, DaemonServiceError) and error.reason_code in _MANAGER_DAEMON_REASONS
    )
