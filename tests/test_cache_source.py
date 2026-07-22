from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from scripts.install_cache import publish_cache

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST = "runtime/package-files.json"


def _source(root: Path) -> tuple[Path, tuple[str, ...]]:
    files = {
        ".codex-plugin/plugin.json": b'{"name":"fixture"}\n',
        "hooks/hooks.json": b'{"hooks":{}}\n',
        "payload/a.txt": b"A",
    }
    paths = tuple(sorted((*files, MANIFEST), key=str.encode))
    files[MANIFEST] = json.dumps(paths, indent=2).encode() + b"\n"
    for relative, data in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
    return root.resolve(), paths


@pytest.mark.skipif(os.name != "posix", reason="POSIX source mode contract")
def test_normal_git_source_modes_are_distinct_from_private_cache_modes(tmp_path: Path) -> None:
    source, paths = _source(tmp_path / "source")
    for path in (source, *source.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o644)
    home = (tmp_path / "home").resolve()
    home.mkdir()
    target = publish_cache(source, home, "1.0.0").cache_path
    for relative in paths:
        assert target.joinpath(*relative.split("/")).stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o700
