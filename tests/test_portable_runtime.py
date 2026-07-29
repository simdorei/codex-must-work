from __future__ import annotations

from pathlib import Path

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_ROOT = Path(__file__).parents[1]
_ARCHIVES = {
    "cpython-3.12.13+20260510-windows-x64.tar.gz": (
        "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0"
    ),
    "cpython-3.12.13+20260510-linux-x64.tar.gz": (
        "d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88"
    ),
    "cpython-3.12.13+20260510-macos-arm64.tar.gz": (
        "55bc1a5edbc8ac4da0081f4f5731ed2d1ed10c57cb37a820b2a0dbc7cad742e9"
    ),
}

ROOT = _ROOT
ARCHIVES = _ARCHIVES
