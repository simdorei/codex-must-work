"""Group manager side-effect boundaries used by production and tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.goal_turn_source import wait_for_native_goal_turn
from scripts.watcher_launch import launch_watcher

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from scripts.watcher_source import RolloutCursor


@dataclass(frozen=True, slots=True)
class ManagerCallbacks:
    """Inject the two process-launching or blocking manager boundaries."""

    watcher_launcher: Callable[[], None] = launch_watcher
    goal_turn_verifier: Callable[[Path, RolloutCursor, str], bool] = wait_for_native_goal_turn
