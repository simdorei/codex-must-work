"""Atomic redacted-output writer for native CI evidence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tests.native_ci_evidence_models import EvidenceError

if TYPE_CHECKING:
    from tests.native_ci_evidence_json import JsonValue

_OUTPUT_PATH: Final = "output_path"
_OUTPUT_FAILED: Final = "output_failed"


def write_summary(output: Path, summary: dict[str, JsonValue]) -> None:
    """Atomically write validated public evidence with stable failures."""
    try:
        parent = output.absolute().parent
        if not parent.is_dir() or output.is_symlink():
            raise EvidenceError(_OUTPUT_PATH)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    summary,
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                _ = handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            _ = temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise EvidenceError(_OUTPUT_FAILED) from None
