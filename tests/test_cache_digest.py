from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

DOMAIN = b"codex-must-work-package-v1\0"


def _digest(path: str, data: bytes) -> str:
    encoded = path.encode()
    digest = hashlib.sha256(DOMAIN + struct.pack(">I", 1))
    digest.update(struct.pack(">I", len(encoded)))
    digest.update(encoded)
    digest.update(struct.pack(">Q", len(data)))
    digest.update(data)
    return digest.hexdigest()


def test_digest_framing_separates_legacy_concatenation_collision(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "ab"
    _ = first.write_bytes(b"bc")
    _ = second.write_bytes(b"c")
    assert first.name.encode() + first.read_bytes() == second.name.encode() + second.read_bytes()
    assert _digest(first.name, first.read_bytes()) != _digest(second.name, second.read_bytes())
