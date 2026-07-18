from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SESSION_ID = "019ba8f0-7b5a-7000-8000-000000000001"


def write_session_meta(path: Path, session_id: str = SESSION_ID) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-07-18T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": session_id},
    }
    _ = path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
