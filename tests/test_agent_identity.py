from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from scripts.agent_identity import AgentIdentityResolver
from scripts.notifications import NotificationSubject, NotificationSubjectKind

if TYPE_CHECKING:
    from pathlib import Path


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        _ = connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                agent_nickname TEXT,
                agent_role TEXT
            )
            """
        )
        _ = connection.execute(
            "INSERT INTO threads (id, title, agent_nickname, agent_role) VALUES (?, ?, ?, ?)",
            ("child-1", "private child title", "Tesla", "explorer"),
        )


def test_agent_identity_resolves_task_main_and_named_subagent(tmp_path: Path) -> None:
    database = tmp_path / "state_1.sqlite"
    _database(database)
    resolver = AgentIdentityResolver(state_database=database)

    task = resolver.resolve(NotificationSubject(NotificationSubjectKind.TASK))
    main = resolver.resolve(NotificationSubject(NotificationSubjectKind.MAIN_AGENT))
    child = resolver.resolve(
        NotificationSubject(NotificationSubjectKind.SUBAGENT, target_id="child-1")
    )

    assert task == "전체 작업"
    assert main == "메인 에이전트"
    assert child == "Tesla (explorer)"


def test_missing_subagent_metadata_uses_stable_anonymous_label(tmp_path: Path) -> None:
    database = tmp_path / "state_1.sqlite"
    _database(database)
    resolver = AgentIdentityResolver(state_database=database)
    subject = NotificationSubject(NotificationSubjectKind.SUBAGENT, target_id="private-child-id")

    first = resolver.resolve(subject)
    second = resolver.resolve(subject)

    assert first == second
    assert first.startswith("서브에이전트 #")
    assert "private-child-id" not in first


def test_old_codex_database_schema_falls_back_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "state_1.sqlite"
    with sqlite3.connect(database) as connection:
        _ = connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")

    label = AgentIdentityResolver(state_database=database).resolve(
        NotificationSubject(NotificationSubjectKind.SUBAGENT, target_id="child-legacy")
    )

    assert label.startswith("서브에이전트 #")
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.OperationalError, match="no such column"),
    ):
        connection.execute("SELECT agent_nickname FROM threads").fetchone()
