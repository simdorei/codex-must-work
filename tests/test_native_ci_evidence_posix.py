from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tests.native_posix_install_checks import cache_membership_exact
from tests.native_posix_tree_snapshot import tree_snapshot

if TYPE_CHECKING:
    import pytest


def test_tree_snapshot_never_reads_control_key_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a control key whose content-read methods are tripwired.
    key = tmp_path / "control.key"
    _ = key.write_bytes(b"k" * 32)
    ordinary = tmp_path / "ordinary.txt"
    _ = ordinary.write_text("public", encoding="utf-8")
    original = Path.read_bytes

    def tripwire(path: Path) -> bytes:
        if path == key:
            msg = "control.key content read"
            raise AssertionError(msg)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tripwire)

    # When: the reinstall snapshot treats the exact key as metadata-only.
    snapshot = tree_snapshot(tmp_path, metadata_only=frozenset((key,)))

    # Then: public files retain digests while the key records metadata only.
    rows = {row.relative: row for row in snapshot}
    assert rows["control.key"].digest is None
    assert rows["control.key"].size == 32
    assert rows["control.key"].links == 1
    assert rows["ordinary.txt"].digest is not None


def test_cache_membership_rejects_unexpected_empty_directory(tmp_path: Path) -> None:
    # Given: a manifested file plus one unexpected empty cache directory.
    cache = tmp_path / "cache"
    expected_file = cache / "runtime" / "manifest.json"
    expected_file.parent.mkdir(parents=True)
    _ = expected_file.write_text("{}", encoding="utf-8")
    (cache / "unexpected-empty").mkdir()

    # When/Then: exact membership rejects the extra directory.
    assert not cache_membership_exact(cache, ("runtime/manifest.json",))
