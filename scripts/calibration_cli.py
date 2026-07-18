# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Persist an explicit first-thread calibration answer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.calibration_state import (
    CalibrationDecision,
    CalibrationSnapshot,
    CalibrationStateError,
    record_decision,
)
from scripts.state import state_root


class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.answer: str = ""
        self.plugin_version: str = ""


def apply_decision(root: Path, plugin_version: str, answer: str) -> CalibrationSnapshot:
    """Apply only one explicit, allowlisted user answer."""
    if answer == "apply":
        decision = CalibrationDecision.ACCEPT
    elif answer == "reject":
        decision = CalibrationDecision.REJECT
    else:
        raise CalibrationStateError(root / "calibration.json", "answer_invalid")
    return record_decision(root, plugin_version, decision)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("answer", choices=("apply", "reject"))
    _ = parser.add_argument("--plugin-version", required=True)
    return parser


def _main(argv: list[str] | None = None) -> int:
    arguments = _Arguments()
    _ = _parser().parse_args(argv, namespace=arguments)
    try:
        snapshot = apply_decision(
            state_root(),
            arguments.plugin_version,
            arguments.answer,
        )
    except CalibrationStateError as error:
        _ = sys.stderr.write(f"{error}\n")
        return 1
    _ = sys.stdout.write(
        json.dumps(
            {
                "plugin_version": snapshot.plugin_version,
                "status": snapshot.status.value,
                "sample_count": snapshot.sample_count,
                "warning_after_ms": snapshot.warning_after_ms,
                "restart_after_ms": snapshot.restart_after_ms,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
