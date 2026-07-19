from pathlib import Path

from scripts.app_server_protocol import TurnOutcome
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.state import StateDocument, load_state, runtime_path, save_state
from scripts.state_io import JsonValue
from scripts.watcher_source import RolloutCursor, initial_cursor, save_cursor
from tests.rollout_fixture import write_session_meta


class FakeGoalAppServer:
    def __init__(
        self,
        *,
        start_on_resume: bool = True,
        complete_on_resume: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.active: str | None = None
        self.completed: set[str] = set()
        self.turn_outcomes: dict[str, TurnOutcome] = {}
        self.pending_server_request: str | None = None
        self.goal_status: str = "active"
        self.goal_created_at: int = 10
        self.turn_number: int = 0
        self.start_on_resume: bool = start_on_resume
        self.complete_on_resume: bool = complete_on_resume

    def _goal(self) -> dict[str, JsonValue]:
        return {
            "status": self.goal_status,
            "threadId": "thread-1",
            "createdAt": self.goal_created_at,
            "objective": "finish",
            "tokenBudget": None,
        }

    def finish_active_turn(self, outcome: TurnOutcome = TurnOutcome.COMPLETED) -> str:
        """Complete one turn and model Goal auto-continuation only while active."""
        assert self.active is not None
        finished = self.active
        self.completed.add(finished)
        self.turn_outcomes[finished] = outcome
        self.active = None
        if self.goal_status == "active" and self.start_on_resume:
            self.turn_number += 1
            self.active = f"turn-goal-{self.turn_number}"
        return finished

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
        self.calls.append(method)
        if method == "thread/goal/get":
            return {"goal": self._goal()}
        if method == "thread/goal/set":
            status = params.get("status")
            assert isinstance(status, str)
            self.goal_status = status
            if status == "active" and self.start_on_resume and self.active is None:
                self.turn_number += 1
                self.active = f"turn-goal-{self.turn_number}"
                if self.complete_on_resume:
                    self.completed.add(self.active)
                    self.active = None
            return {"goal": self._goal()}
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
        return f"turn-goal-{self.turn_number}" if self.turn_number else None

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
        _ = previous_turn_id
        _ = timeout_seconds
        latest = self.latest_started_turn(thread_id)
        return latest if latest != previous_turn_id else None


def accept_fake_goal_turn(_rollout: Path, _cursor: RolloutCursor, _turn_id: str) -> bool:
    """Stand in for native rollout provenance in protocol-only fake tests."""
    return True


def runtime_fixture(tmp_path: Path) -> tuple[Path, Path]:
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
                "goal_companion": True,
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


def test_goal_companion_restarts_exact_turn_through_pause_and_resume(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer()
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )

    engine.initialize()
    assert client.goal_status == "paused"
    assert engine.tick() is True
    first = load_state(root, path).values
    assert first["managed_turn_id"] == "turn-goal-1"

    updated: dict[str, JsonValue] = dict(first)
    updated["restart_request"] = {
        "request_id": "request-1",
        "turn_id": "turn-goal-1",
        "target_id": None,
        "target_generation": 1,
        "progress_epoch": 0,
    }
    updated["restart_claimed"] = False
    save_state(root, path, StateDocument(values=updated))

    assert engine.tick() is True
    interrupted = load_state(root, path).values
    assert interrupted["managed_turn_id"] is None
    assert interrupted["restart_count"] == 1
    assert client.goal_status == "paused"

    assert engine.tick() is True
    restarted = load_state(root, path).values
    assert restarted["managed_turn_id"] == "turn-goal-2"
    assert client.active == "turn-goal-2"
    assert client.goal_status == "paused"
    assert client.calls == [
        "initialize",
        "thread/resume",
        "thread/goal/get",
        "thread/goal/set",
        "thread/goal/get",
        "thread/goal/set",
        "thread/goal/get",
        "thread/goal/set",
        "thread/goal/get",
        "turn/interrupt",
        "thread/backgroundTerminals/clean",
        "thread/goal/get",
        "thread/goal/set",
        "thread/goal/get",
        "thread/goal/set",
        "turn/steer",
    ]


def test_goal_companion_fails_closed_when_resume_produces_no_turn(tmp_path: Path) -> None:
    root, path = runtime_fixture(tmp_path)
    client = FakeGoalAppServer(start_on_resume=False)
    engine = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(
            watcher_launcher=lambda: None,
            goal_turn_verifier=accept_fake_goal_turn,
        ),
    )

    engine.initialize()

    assert engine.tick() is False
    runtime = load_state(root, path).values
    assert runtime["manager_error"] == "goal_resume_timeout"
    assert runtime["manager_ready"] is False
    assert client.goal_status == "active"
