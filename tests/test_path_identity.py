import os
from pathlib import Path

import pytest

from scripts.path_identity import UnsupportedLocalPathError, resolve_local_path


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace semantics")
def test_windows_extended_drive_path_matches_regular_path(tmp_path: Path) -> None:
    # Given: one local file expressed with and without the extended drive prefix.
    regular = tmp_path / "rollout.jsonl"
    regular.touch()
    extended = Path("\\\\?\\" + str(regular))

    # When: both spellings are resolved to their platform identity.
    resolved_regular = resolve_local_path(regular)
    resolved_extended = resolve_local_path(extended)

    # Then: both spellings identify the same local file.
    assert resolved_extended == resolved_regular


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace semantics")
@pytest.mark.parametrize(
    "raw",
    [
        "\\\\?\\UNC\\server\\share\\rollout.jsonl",
        "\\\\?\\unc\\server\\share\\rollout.jsonl",
        "\\\\.\\C:\\rollout.jsonl",
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\rollout.jsonl",
        "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\rollout.jsonl",
        "\\\\?\\C:rollout.jsonl",
        "\\\\?\\1:\\rollout.jsonl",
        "\\\\?\\?:\\rollout.jsonl",
        "//./C:/rollout.jsonl",
        "//server/share/rollout.jsonl",
        "//?/C:/rollout.jsonl",
        "\\\\?\\C:\\name. ",
        "\\\\?\\C:\\CON",
    ],
)
def test_windows_non_drive_namespaces_are_rejected(raw: str) -> None:
    # Given: an unsupported device, UNC, volume, or drive-relative namespace.
    # When/Then: path identity rejects it instead of resolving it against the current directory.
    with pytest.raises(UnsupportedLocalPathError):
        _ = resolve_local_path(raw)
