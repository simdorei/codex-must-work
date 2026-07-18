from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from scripts.calibration import CalibrationRecommendation, CalibrationStatus
from scripts.calibration_state import (
    CalibrationDecision,
    CalibrationEnvironment,
    CalibrationSnapshot,
    load_or_calibrate,
    record_decision,
)
from scripts.durations import Milliseconds

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def _write_manifest(plugin_root: Path, version: str) -> None:
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    _ = manifest.write_text(json.dumps({"version": version}), encoding="utf-8")


def test_load_or_calibrate_when_version_is_unchanged_scans_once(tmp_path: Path) -> None:
    # Given: a plugin version and a deterministic history scanner.
    plugin_root = tmp_path / "plugin"
    root = tmp_path / "state"
    _write_manifest(plugin_root, "1.0.0")
    calls: list[Path] = []

    def scanner(codex_home: Path, _now: datetime) -> CalibrationRecommendation:
        calls.append(codex_home)
        return CalibrationRecommendation(25, Milliseconds(120_000), Milliseconds(300_000))

    environment = CalibrationEnvironment(root, plugin_root, tmp_path / "codex", _NOW)

    # When: two threads start on the same installed version.
    first = load_or_calibrate(environment, scanner)
    second = load_or_calibrate(environment, scanner)

    # Then: only the first thread announces it and the expensive scan runs once.
    assert first.status is CalibrationStatus.PENDING
    assert first.should_announce is True
    assert second.status is CalibrationStatus.PENDING
    assert second.should_announce is False
    assert second.warning_after_ms == first.warning_after_ms
    assert second.restart_after_ms == first.restart_after_ms
    assert calls == [tmp_path / "codex"]


def test_load_or_calibrate_when_version_changes_scans_again(tmp_path: Path) -> None:
    # Given: one stored version-specific recommendation.
    plugin_root = tmp_path / "plugin"
    root = tmp_path / "state"
    calls = 0

    def scanner(_codex_home: Path, _now: datetime) -> CalibrationRecommendation:
        nonlocal calls
        calls += 1
        return CalibrationRecommendation(20, Milliseconds(60_000), Milliseconds(120_000))

    _write_manifest(plugin_root, "1.0.0")
    environment = CalibrationEnvironment(root, plugin_root, tmp_path / "codex", _NOW)
    _ = load_or_calibrate(environment, scanner)
    _write_manifest(plugin_root, "1.0.1")

    # When: the first thread for the updated version starts.
    updated = load_or_calibrate(environment, scanner)

    # Then: a fresh recommendation replaces the old version's pending state.
    assert updated.plugin_version == "1.0.1"
    assert calls == 2


def test_load_or_calibrate_when_threads_race_scans_and_announces_once(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugin"
    root = tmp_path / "state"
    _write_manifest(plugin_root, "1.0.0")
    calls = 0

    def scanner(_codex_home: Path, _now: datetime) -> CalibrationRecommendation:
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return CalibrationRecommendation(20, Milliseconds(60_000), Milliseconds(120_000))

    environment = CalibrationEnvironment(root, plugin_root, tmp_path / "codex", _NOW)

    def load(_index: int) -> CalibrationSnapshot:
        return load_or_calibrate(environment, scanner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshots = list(executor.map(load, range(2)))

    assert calls == 1
    assert sum(snapshot.should_announce for snapshot in snapshots) == 1


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (CalibrationDecision.ACCEPT, CalibrationStatus.ACCEPTED),
        (CalibrationDecision.REJECT, CalibrationStatus.REJECTED),
    ],
)
def test_record_decision_when_recommendation_is_pending_persists_answer(
    tmp_path: Path,
    decision: CalibrationDecision,
    expected: CalibrationStatus,
) -> None:
    # Given: a pending recommendation for the installed version.
    plugin_root = tmp_path / "plugin"
    root = tmp_path / "state"
    _write_manifest(plugin_root, "1.0.0")
    environment = CalibrationEnvironment(root, plugin_root, tmp_path / "codex", _NOW)
    _ = load_or_calibrate(
        environment,
        lambda _home, _now: CalibrationRecommendation(
            20,
            Milliseconds(60_000),
            Milliseconds(120_000),
        ),
    )

    # When: the user explicitly accepts or rejects it.
    result = record_decision(root, "1.0.0", decision)

    # Then: the choice is durable without changing the recommended numbers.
    assert result.status is expected
    assert result.warning_after_ms == 60_000
    assert result.restart_after_ms == 120_000
