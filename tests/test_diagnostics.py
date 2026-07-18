from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scripts.diagnostics import (
    DiagnosticCode,
    DiagnosticEvent,
    MonitorState,
    UnsafeDiagnosticPathError,
    append_diagnostic,
)


def _event() -> DiagnosticEvent:
    return DiagnosticEvent(
        occurred_at=datetime(2026, 7, 17, tzinfo=UTC),
        code=DiagnosticCode.OBSERVABLE_PROGRESS_SILENCE,
        state=MonitorState.ACTIVE,
        session_hash="a" * 64,
        child_hash="b" * 64,
        elapsed_ms=90_000,
    )


def test_append_diagnostic_when_limit_is_crossed_keeps_two_backups() -> None:
    # Given: a tiny diagnostic size limit.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"

        # When: enough sanitized events are appended to rotate repeatedly.
        for _ in range(12):
            append_diagnostic(root, _event(), max_bytes=400)

        # Then: the active log plus exactly two bounded backups remain.
        logs = sorted((root / "logs").glob("diagnostic.jsonl*"))
        assert len(logs) == 3
        assert all(path.stat().st_size <= 400 for path in logs)


def test_append_diagnostic_writes_only_sanitized_fields() -> None:
    # Given: a diagnostic event whose API has no raw text field.
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory) / "codex-must-work"

        # When: the event is persisted.
        append_diagnostic(root, _event())

        # Then: only the fixed metadata contract is serialized.
        serialized = (root / "logs" / "diagnostic.jsonl").read_text(encoding="utf-8")
        assert "observable_progress_silence" in serialized
        assert "session_hash" in serialized
        assert "child_hash" in serialized
        assert "prompt" not in serialized
        assert "message" not in serialized
        assert "error" not in serialized


def test_append_diagnostic_rejects_final_symlink_without_touching_target() -> None:
    with TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        root = temporary / "codex-must-work"
        logs = root / "logs"
        logs.mkdir(parents=True)
        victim = temporary / "victim.txt"
        _ = victim.write_text("unchanged\n", encoding="utf-8")
        (logs / "diagnostic.jsonl").symlink_to(victim)

        with pytest.raises(UnsafeDiagnosticPathError):
            append_diagnostic(root, _event())

        assert victim.read_text(encoding="utf-8") == "unchanged\n"
