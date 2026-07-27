from dataclasses import replace
from pathlib import Path

import pytest

from scripts.control import CapabilityReport
from scripts.goal_control import GoalControlError
from scripts.setup import enable_session
from tests.test_setup import managed_report, ready_report, request

_UNAVAILABLE = "goal_companion_atomic_update_unavailable"


@pytest.mark.parametrize("capabilities", [managed_report(), ready_report()])
def test_goal_companion_is_rejected_before_setup_mutation(
    tmp_path: Path,
    capabilities: CapabilityReport,
) -> None:
    root = tmp_path / "state"
    activation = replace(request(root, observe_only=False), goal_companion=True)

    with pytest.raises(GoalControlError, match=f"^{_UNAVAILABLE}$"):
        _ = enable_session(root, activation, capabilities)

    assert not root.exists()
