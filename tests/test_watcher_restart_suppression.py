from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.hook_event import process_hook
from scripts.manager_callbacks import ManagerCallbacks
from scripts.manager_engine import ManagerEngine
from scripts.state import StateDocument, load_state, save_state
from scripts.watcher_engine import WatcherEngine
from tests.manager_fixture import FakeAppServer, manager_runtime_fixture
from tests.watcher_fixture import WALL_TIME, append_terminal, state

if TYPE_CHECKING:
    from pathlib import Path


def test_parent_can_restart_after_temporarily_suppressing_child_finishes(tmp_path: Path) -> None:
    root, rollout, path = state(tmp_path, children=1, parent=True)
    values = dict(load_state(root, path).values)
    values.update(
        {
            "observe_only": False,
            "managed_mode": True,
            "manager_ready": True,
            "managed_turn_id": "turn-parent",
            "handoff_requested": False,
            "restart_request": None,
        }
    )
    save_state(root, path, StateDocument(values=values))
    engine = WatcherEngine(root)

    assert engine.tick(0.0, WALL_TIME) is True
    assert engine.tick(301.0, WALL_TIME) is True
    assert load_state(root, path).values["restart_request"] is None
    append_terminal(rollout, "child-1")
    assert engine.tick(302.0, WALL_TIME) is True
    assert engine.tick(603.0, WALL_TIME) is True
    assert engine.tick(604.0, WALL_TIME) is True

    request = load_state(root, path).values["restart_request"]
    assert isinstance(request, dict)
    assert request["target_id"] is None


def test_cancelled_whole_turn_restart_rearms_after_child_activity(tmp_path: Path) -> None:
    root, path = manager_runtime_fixture(tmp_path)
    client = FakeAppServer()
    manager = ManagerEngine(
        root,
        path.name,
        client,
        pid=123,
        callbacks=ManagerCallbacks(watcher_launcher=lambda: None),
    )
    manager.initialize()
    assert manager.tick() is True
    watcher = WatcherEngine(root)
    assert watcher.tick(0.0, WALL_TIME) is True
    assert watcher.tick(91.0, WALL_TIME) is True
    assert watcher.tick(301.0, WALL_TIME) is True
    assert load_state(root, path).values["restart_request"] is not None

    _ = process_hook(
        json.dumps(
            {
                "session_id": "thread-1",
                "hook_event_name": "SubagentStart",
                "agent_id": "child-1",
            }
        ),
        root=root,
    )
    assert manager.tick() is True
    assert load_state(root, path).values["restart_request"] is None
    assert watcher.tick(302.0, WALL_TIME) is True
    _ = process_hook(
        json.dumps(
            {
                "session_id": "thread-1",
                "hook_event_name": "SubagentStop",
                "agent_id": "child-1",
            }
        ),
        root=root,
    )
    assert watcher.tick(303.0, WALL_TIME) is True
    assert watcher.tick(604.0, WALL_TIME) is True
    assert watcher.tick(605.0, WALL_TIME) is True

    request = load_state(root, path).values["restart_request"]
    assert isinstance(request, dict)
    assert request["turn_id"] == "turn-1"
    assert "turn/interrupt" not in client.calls
