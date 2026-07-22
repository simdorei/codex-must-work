from __future__ import annotations

import multiprocessing
from pathlib import Path

from tests.install_plugin_support import (
    UNSUPPORTED,
    ContendedArgs,
    contended_install,
    source_fixture,
)

pytest_plugins = ("tests.install_plugin_fixtures",)

def test_two_process_full_installer_serializes_beyond_eleven_seconds(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    home = tmp_path / "home"
    source = source_fixture(tmp_path)
    temp_root = tmp_path / "process-temp"
    home.mkdir()
    temp_root.mkdir()
    a_entered, a_release = context.Event(), context.Event()
    b_entered, b_release = context.Event(), context.Event()
    first_result, second_result = tmp_path / "first", tmp_path / "second"
    first = context.Process(
        target=contended_install,
        args=(
            ContendedArgs(
                home=str(home.resolve()),
                source=str(source),
                temp_root=str(temp_root),
                entered=a_entered,
                release=a_release,
                hold=True,
                result_path=str(first_result),
            ),
        ),
    )
    first.start()
    assert a_entered.wait(10)
    second = context.Process(
        target=contended_install,
        args=(
            ContendedArgs(
                home=str(home.resolve()),
                source=str(source),
                temp_root=str(temp_root),
                entered=b_entered,
                release=b_release,
                hold=False,
                result_path=str(second_result),
            ),
        ),
    )
    second.start()
    first.join(11.2)
    assert first.is_alive()
    assert not b_entered.is_set()
    a_release.set()
    first.join(10)
    second.join(10)
    assert first.exitcode == second.exitcode == 0
    assert b_entered.is_set()
    assert first_result.read_text(encoding="utf-8") == UNSUPPORTED
    assert second_result.read_text(encoding="utf-8") == UNSUPPORTED
