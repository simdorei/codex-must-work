from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts import cache_publication, install_cache
from scripts.cache_publication import remove_tree
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST = "runtime/package-files.json"


def _publish(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, home = tmp_path / "source", (tmp_path / "home").resolve()
    files = {
        ".codex-plugin/plugin.json": b'{"name":"fixture"}\n',
        "hooks/hooks.json": b'{"hooks":{}}\n',
        "payload/a.txt": b"A",
    }
    paths = tuple(sorted((*files, MANIFEST), key=str.encode))
    files[MANIFEST] = json.dumps(paths, indent=2).encode() + b"\n"
    for relative, data in files.items():
        path = source.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
    home.mkdir()
    result = publish_cache(source.resolve(), home, "1.0.0")
    return source.resolve(), home, result.cache_path


def test_competitor_inserted_at_quarantine_boundary_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, home, target = _publish(tmp_path)
    publication = publish_cache((tmp_path / "source").resolve(), home, "1.0.0")
    real_rename = cache_publication.rename_no_replace

    def rename_then_compete(source: Path, quarantine: Path) -> None:
        real_rename(source, quarantine)
        source.mkdir()
        _ = (source / "competitor").write_bytes(b"keep")

    monkeypatch.setattr(cache_publication, "rename_no_replace", rename_then_compete)
    remove_tree(target, publication.identity)
    assert (target / "competitor").read_bytes() == b"keep"
    assert not any(path.name.startswith(".cmw-delete-") for path in target.parent.iterdir())


def test_created_by_run_never_binds_to_post_rename_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, home, target = _publish(tmp_path)
    target = target.with_name("2.0.0")
    real_rename = cache_publication.rename_no_replace

    def rename_then_replace(stage: Path, destination: Path) -> None:
        real_rename(stage, destination)
        _ = destination.rename(destination.with_name("ours-moved"))
        destination.mkdir()
        _ = (destination / "competitor").write_bytes(b"keep")

    monkeypatch.setattr(install_cache, "_rename_no_replace", rename_then_replace)
    with pytest.raises(InstallPluginError, match="cache_cleanup_failed"):
        _ = publish_cache(source, home, "2.0.0")
    assert (target / "competitor").read_bytes() == b"keep"
