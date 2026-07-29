from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.session_hook import process_session_start

if TYPE_CHECKING:
    from pathlib import Path


def test_removed_session_start_hook_emits_no_context_or_state(
    tmp_path: Path,
) -> None:
    # Given: a valid general SessionStart payload and clean isolated roots.
    root = tmp_path / "state"
    plugin_data = tmp_path / "plugin-data"
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": "unrelated-session",
            "transcript_path": str(tmp_path / "rollout.jsonl"),
        }
    )

    # When: the compatibility function receives the old event directly.
    result = process_session_start(payload, root=root, plugin_data=plugin_data)

    # Then: no CMW context or filesystem state is produced.
    assert result is None
    assert not root.exists()
    assert not plugin_data.exists()
