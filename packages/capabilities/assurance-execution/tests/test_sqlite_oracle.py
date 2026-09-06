from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assurance_execution.operations.sqlite_oracle import observe_user


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE "user" '
            "(username TEXT, email TEXT, is_active INTEGER, is_superuser INTEGER, dept_id INTEGER)"
        )


def test_oracle_reads_only_committed_state(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    _database(db)
    writer = sqlite3.connect(db)
    try:
        writer.execute(
            'INSERT INTO "user" VALUES (?, ?, ?, ?, ?)',
            ("qa_t1", "qa_t1@example.com", 1, 0, None),
        )
        observation = observe_user(db, "qa_t1", "qa_t1@example.com")
        assert observation["state"] == "observed"
        assert observation["rows"] == []
        writer.rollback()
        assert observe_user(db, "qa_t1", "qa_t1@example.com")["rows"] == []
    finally:
        writer.close()


def test_oracle_returns_error_for_missing_database(tmp_path) -> None:
    observation = observe_user(tmp_path / "missing.sqlite3", "qa_t1", "qa_t1@example.com")

    assert observation["state"] == "error"
    assert observation["rows"] == []
    assert observation["reason"] == "database_not_found"


def test_oracle_strictly_normalizes_boolean_columns(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    _database(db)
    with sqlite3.connect(db) as connection:
        connection.execute(
            'INSERT INTO "user" VALUES (?, ?, ?, ?, ?)',
            ("qa_t1", "qa_t1@example.com", 2, 0, None),
        )

    observation = observe_user(db, "qa_t1", "qa_t1@example.com")

    assert observation["state"] == "error"
    assert observation["rows"] == []
    assert observation["reason"] == "invalid_boolean:is_active"


def test_oracle_keeps_at_most_two_rows_to_prove_cardinality(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    _database(db)
    with sqlite3.connect(db) as connection:
        connection.executemany(
            'INSERT INTO "user" VALUES (?, ?, ?, ?, ?)',
            [
                ("qa_t1", "first@example.com", 1, 0, None),
                ("second", "qa_t1@example.com", 1, 0, None),
                ("qa_t1", "third@example.com", 1, 0, None),
            ],
        )

    observation = observe_user(db, "qa_t1", "qa_t1@example.com")

    assert observation["state"] == "observed"
    assert len(observation["rows"]) == 2
    assert observation["rows"][0]["is_active"] is True
    assert observation["rows"][0]["is_superuser"] is False


def test_oracle_reports_lock_budget_as_timeout(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    _database(db)
    locker = sqlite3.connect(db)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        observation = observe_user(db, "qa_t1", "qa_t1@example.com", timeout_s=0.01)
    finally:
        locker.rollback()
        locker.close()

    assert observation["state"] == "timeout"
    assert observation["rows"] == []
    assert observation["reason"] == "database_busy"


def test_oracle_rejects_database_replacement_during_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "db.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(db)
    _database(replacement)
    original_stat = Path.stat
    db_stat_calls = 0

    def replace_before_final_stat(path: Path, *args, **kwargs):
        nonlocal db_stat_calls
        if path == db:
            db_stat_calls += 1
            if db_stat_calls == 3:
                replacement.replace(db)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replace_before_final_stat)

    observation = observe_user(db, "qa_t1", "qa_t1@example.com")

    assert observation["state"] == "error"
    assert observation["reason"] == "database_file_changed"
