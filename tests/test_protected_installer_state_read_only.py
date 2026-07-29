from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.install_errors import InstallPluginError
from scripts.installer_lock import installer_lock
from scripts.protected_installer_state import read_signed_record

if TYPE_CHECKING:
    from pathlib import Path


def test_missing_clean_home_receipt_lookup_creates_no_path(
    tmp_path: Path,
) -> None:
    # Given: a clean CODEX_HOME containing no installer state.
    home = (tmp_path / "clean-home").resolve()
    home.mkdir()
    before = tuple(home.rglob("*"))

    # When: uninstall authorization performs a read-only protected-state lookup.
    with (
        installer_lock(home) as lease,
        pytest.raises(
            InstallPluginError,
            match="uninstall_receipt_reinstall_required",
        ),
    ):
        _ = read_signed_record(lease, "install-receipt-v1.json")

    # Then: the lookup created no installer-state path or other bytes.
    assert tuple(home.rglob("*")) == before
