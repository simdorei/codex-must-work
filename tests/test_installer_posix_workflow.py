from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Protocol

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

_ROOT: Final = Path(__file__).parents[1]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _load_json(loader: _JsonLoader, data: str) -> JsonValue:
    return loader(data)


def test_posix_workflow_is_push_only_and_candidate_bound() -> None:
    workflow = _load_json(
        json.loads,
        (_ROOT / ".github" / "workflows" / "installer-posix.yml").read_text(encoding="utf-8"),
    )
    expected = {
        "name": "Native POSIX installer",
        "on": {"push": {"branches": ["**"]}},
        "permissions": {"contents": "read"},
        "jobs": {
            "ubuntu-x64": {
                "runs-on": "ubuntu-latest",
                "steps": [
                    {
                        "name": "Checkout candidate",
                        "uses": "actions/checkout@v4",
                        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
                    },
                    {
                        "name": "Verify candidate SHA",
                        "shell": "bash",
                        "run": (
                            'test "$(git rev-parse HEAD)" = "${{ github.sha }}"\n'
                            'test "$(uname -s)" = "Linux"\n'
                            'test "$(uname -m)" = "x86_64"\n'
                            "sh -n install.sh\n"
                        ),
                    },
                    {
                        "name": "Run native installer smoke",
                        "shell": "bash",
                        "run": "python3.12 tests/native_posix_install_smoke.py\n",
                    },
                ],
            },
            "macos-arm64": {
                "runs-on": "macos-14",
                "steps": [
                    {
                        "name": "Checkout candidate",
                        "uses": "actions/checkout@v4",
                        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
                    },
                    {
                        "name": "Verify candidate SHA",
                        "shell": "bash",
                        "run": (
                            'test "$(git rev-parse HEAD)" = "${{ github.sha }}"\n'
                            'test "$(uname -s)" = "Darwin"\n'
                            'test "$(uname -m)" = "arm64"\n'
                            "sh -n install.sh\n"
                        ),
                    },
                    {
                        "name": "Run native installer smoke",
                        "shell": "bash",
                        "run": "python3.12 tests/native_posix_install_smoke.py\n",
                    },
                ],
            },
        },
    }

    assert workflow == expected
