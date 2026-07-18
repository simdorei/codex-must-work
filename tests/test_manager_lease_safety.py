import os
from pathlib import Path

import pytest

from scripts.manager_lease import acquire_manager_lease
from scripts.state import UnsafeStatePathError


def test_manager_lease_rejects_final_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"
    lease_path = managers / f"{runtime_name}.lease"
    outside = tmp_path / "outside.lease"
    _ = outside.write_text("unchanged\n", encoding="ascii")
    try:
        lease_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    with pytest.raises(UnsafeStatePathError):
        _ = acquire_manager_lease(root, runtime_name)

    assert outside.read_text(encoding="ascii") == "unchanged\n"


def test_manager_lease_rejects_hard_link_without_truncating_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    managers = root / "managers"
    managers.mkdir(parents=True)
    runtime_name = "a" * 64 + ".json"
    lease_path = managers / f"{runtime_name}.lease"
    outside = tmp_path / "outside.lease"
    _ = outside.write_text("unchanged\n", encoding="ascii")
    try:
        os.link(outside, lease_path)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(UnsafeStatePathError):
        _ = acquire_manager_lease(root, runtime_name)

    assert outside.read_text(encoding="ascii") == "unchanged\n"
