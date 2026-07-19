from pathlib import Path
from typing import final

from scripts.app_server_protocol import TurnOutcome
from scripts.state import StateDocument, load_state, runtime_path, save_state
from scripts.state_io import JsonValue
from scripts.watcher_source import initial_cursor, save_cursor
from tests.rollout_fixture import write_session_meta


@final
class FakeAppServer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active: str | None = None
        self.completed: set[str] = set()
        self.turn_outcomes: dict[str, TurnOutcome] = {}
        self.pending_server_request: str | None = None
        self.turn_number: int = 0

    def start(self) -> None:
        self.calls.append("initialize")

    def request(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, JsonValue]:
        _ = timeout_seconds
        _ = params
        self.calls.append(method)
        if method == "turn/start":
            self.turn_number += 1
            self.active = f"turn-{self.turn_number}"
            return {"turn": {"id": self.active}}
        if method == "turn/interrupt" and self.active is not None:
            self.completed.add(self.active)
            self.turn_outcomes[self.active] = TurnOutcome.INTERRUPTED
            self.active = None
        return {}

    def active_turn(self, thread_id: str) -> str | None:
        _ = thread_id
        return self.active

    def turn_completed(self, turn_id: str) -> bool:
        return turn_id in self.completed

    def turn_outcome(self, turn_id: str) -> TurnOutcome | None:
        outcome = self.turn_outcomes.get(turn_id)
        if outcome is not None:
            return outcome
        return TurnOutcome.COMPLETED if turn_id in self.completed else None

    def latest_started_turn(self, thread_id: str) -> str | None:
        _ = thread_id
        return f"turn-{self.turn_number}" if self.turn_number else None

    def wait_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float = 12.0,
    ) -> bool:
        _ = thread_id
        _ = turn_id
        _ = timeout_seconds
        return True

    def wait_turn_completed(self, turn_id: str, timeout_seconds: float = 15.0) -> bool:
        _ = timeout_seconds
        return turn_id in self.completed

    def wait_next_turn_started(
        self,
        thread_id: str,
        previous_turn_id: str | None,
        timeout_seconds: float = 12.0,
    ) -> str | None:
        _ = thread_id
        _ = timeout_seconds
        latest = self.latest_started_turn(thread_id)
        return latest if latest != previous_turn_id else None


def manager_runtime_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "codex-must-work"
    path = runtime_path(root, "thread-1")
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    write_session_meta(rollout, "thread-1")
    save_cursor(root, "thread-1", initial_cursor(rollout))
    save_state(root, root / "config.json", StateDocument(values={"message_preset": "cleanup"}))
    save_state(
        root,
        path,
        StateDocument(
            values={
                "session_id": "thread-1",
                "enabled": True,
                "observe_only": False,
                "managed_mode": True,
                "warning_after_ms": 90_000,
                "restart_after_ms": 300_000,
                "auto_restart_requested_by_user": True,
                "message_preset": "cleanup",
                "executable_sha256": "digest",
                "transcript_path": "sessions/rollout.jsonl",
                "parent_turn_id": None,
                "parent_complete": False,
                "parent": None,
                "children": {},
                "manager_ready": False,
                "manager_pid": None,
                "manager_error": None,
                "handoff_requested": True,
                "managed_turn_id": None,
                "restart_request": None,
                "restart_claimed": False,
                "restart_count": 0,
                "shutdown_requested": False,
                "shutdown_interrupt": False,
                "revision": 0,
            }
        ),
    )
    return root, path


def arm_restart(root: Path, path: Path, turn_id: str) -> None:
    values: dict[str, JsonValue] = dict(load_state(root, path).values)
    values["restart_request"] = {
        "request_id": "request-1",
        "turn_id": turn_id,
        "target_id": None,
        "target_generation": 1,
        "progress_epoch": 0,
    }
    values["restart_claimed"] = False
    save_state(root, path, StateDocument(values=values))
