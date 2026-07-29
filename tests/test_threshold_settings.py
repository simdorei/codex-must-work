from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.durations import Milliseconds
from scripts.threshold_settings import (
    DEFAULT_CRITICAL_MS,
    DEFAULT_WARNING_MS,
    ThresholdMode,
    ThresholdSettingsError,
    ThresholdSettingsStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_missing_settings_use_five_and_ten_minute_defaults(tmp_path: Path) -> None:
    snapshot = ThresholdSettingsStore(tmp_path / "state").load()

    assert snapshot.mode is ThresholdMode.DEFAULT
    assert snapshot.warning_after_ms == DEFAULT_WARNING_MS == 300_000
    assert snapshot.critical_after_ms == DEFAULT_CRITICAL_MS == 600_000


def test_custom_settings_persist_across_store_instances(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = ThresholdSettingsStore(root)

    saved = store.set_custom(Milliseconds(420_000), Milliseconds(900_000))
    loaded = ThresholdSettingsStore(root).load()

    assert saved == loaded
    assert loaded.mode is ThresholdMode.CUSTOM
    assert loaded.warning_after_ms == 420_000
    assert loaded.critical_after_ms == 900_000


def test_recommended_settings_copy_the_current_recommendation(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = ThresholdSettingsStore(
        root,
        recommendation=lambda: (Milliseconds(180_000), Milliseconds(480_000)),
    )

    saved = store.set_recommended()

    assert saved.mode is ThresholdMode.RECOMMENDED
    assert saved.warning_after_ms == 180_000
    assert saved.critical_after_ms == 480_000
    assert ThresholdSettingsStore(root).load() == saved


def test_recommended_settings_fail_when_no_recommendation_exists(tmp_path: Path) -> None:
    store = ThresholdSettingsStore(tmp_path / "state", recommendation=lambda: None)

    with pytest.raises(
        ThresholdSettingsError,
        match="threshold_recommendation_unavailable",
    ):
        _ = store.set_recommended()


def test_default_settings_replace_a_custom_choice(tmp_path: Path) -> None:
    store = ThresholdSettingsStore(tmp_path / "state")
    _ = store.set_custom(Milliseconds(420_000), Milliseconds(900_000))

    reset = store.set_default()

    assert reset.mode is ThresholdMode.DEFAULT
    assert reset.warning_after_ms == 300_000
    assert reset.critical_after_ms == 600_000
