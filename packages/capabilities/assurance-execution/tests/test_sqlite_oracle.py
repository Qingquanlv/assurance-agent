from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from assurance_execution.operations.sqlite_oracle import observe_user
from assurance_execution.operations.verification_manifest import sqlite_file_identity


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


def test_oracle_rejects_replacement_since_manifest_identity_was_frozen(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(db)
    _database(replacement)
    expected = sqlite_file_identity(db)
    replacement.replace(db)

    observation = observe_user(
        db,
        "qa_t1",
        "qa_t1@example.com",
        expected_identity=expected,
    )

    assert observation["state"] == "error"
    assert observation["reason"] == "database_identity_mismatch"


def test_oracle_checks_file_identity_immediately_after_connection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "db.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(db)
    replacement.write_bytes(b"not a database")
    original_connect = sqlite3.connect

    class ReplaceAfterOpen:
        def __init__(self, *args, **kwargs) -> None:
            self.connection = original_connect(*args, **kwargs)

        def __enter__(self):
            self.connection.__enter__()
            replacement.replace(db)
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT username"):
                raise sqlite3.DatabaseError("query must not run after replacement")
            return self.connection.execute(sql, parameters)

        def set_progress_handler(self, callback, instructions) -> None:
            self.connection.set_progress_handler(callback, instructions)

    def replace_after_open(*args, **kwargs):
        return ReplaceAfterOpen(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_after_open)

    observation = observe_user(db, "qa_t1", "qa_t1@example.com")

    assert observation["state"] == "error"
    assert observation["reason"] == "database_file_changed"


def test_oracle_deadline_includes_delayed_connection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "db.sqlite3"
    _database(db)
    original_connect = sqlite3.connect

    def delayed_connect(*args, **kwargs):
        time.sleep(0.02)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", delayed_connect)

    observation = observe_user(db, "qa_t1", "qa_t1@example.com", timeout_s=0.01)

    assert observation["state"] == "timeout"
    assert observation["reason"] == "oracle_budget_exhausted"


@pytest.mark.parametrize("timeout_s", [0, -1, 10.1])
def test_oracle_rejects_unsupported_timeout_bounds(tmp_path: Path, timeout_s: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        observe_user(tmp_path / "db.sqlite3", "qa_t1", "qa_t1@example.com", timeout_s=timeout_s)
