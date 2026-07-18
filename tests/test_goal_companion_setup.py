from dataclasses import replace
from pathlib import Path

import pytest

from scripts.setup import ActivationError, enable_session
from scripts.state import load_state, runtime_path
from tests.rollout_fixture import SESSION_ID
from tests.test_setup import managed_report, ready_report, request


def test_goal_companion_is_persisted_with_managed_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    activation = replace(request(root, observe_only=False), goal_companion=True)

    result = enable_session(root, activation, managed_report())

    runtime = load_state(root, runtime_path(root, SESSION_ID)).values
    assert result.effective_auto_restart is True
    assert runtime["goal_companion"] is True


def test_goal_companion_rejects_non_managed_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    activation = replace(request(root, observe_only=False), goal_companion=True)

    with pytest.raises(ActivationError, match="goal_companion_requires_managed_restart"):
        _ = enable_session(root, activation, ready_report())

    assert not root.exists()
