from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def private_root_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def secure(root: Path) -> None:
        root.mkdir(exist_ok=True)

    monkeypatch.setattr("scripts.setup.ensure_private_root", secure)
    monkeypatch.setattr("scripts.manager_reuse.ensure_private_root", secure, raising=False)
    monkeypatch.setattr("scripts.hook_event.ensure_private_root", secure)
    monkeypatch.setattr("scripts.watcher.ensure_private_root", secure)
    monkeypatch.setattr("scripts.calibration_state.ensure_private_root", secure)
