from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scripts.calibration import CalibrationRecommendation, CalibrationStatus
from scripts.calibration_cli import apply_decision
from scripts.calibration_state import CalibrationEnvironment, load_or_calibrate
from scripts.durations import Milliseconds

if TYPE_CHECKING:
    from pathlib import Path


def test_apply_decision_accepts_only_an_explicit_answer(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    _ = manifest.write_text('{"version":"1.0.0"}', encoding="utf-8")
    root = tmp_path / "state"
    _ = load_or_calibrate(
        CalibrationEnvironment(
            root,
            plugin_root,
            tmp_path / "codex",
            datetime(2026, 7, 19, tzinfo=UTC),
        ),
        lambda _home, _now: CalibrationRecommendation(
            20,
            Milliseconds(120_000),
            Milliseconds(300_000),
        ),
    )

    accepted = apply_decision(root, "1.0.0", "apply")

    assert accepted.status is CalibrationStatus.ACCEPTED
    assert accepted.warning_after_ms == 120_000
    assert accepted.restart_after_ms == 300_000
