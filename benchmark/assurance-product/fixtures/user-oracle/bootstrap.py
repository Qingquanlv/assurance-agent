"""Create the stopped SQLite seed used by the managed User benchmark SUT."""

from __future__ import annotations

import sqlite3
import ast
from pathlib import Path


_DISABLED_PASSWORD = "!managed-runtime-secret-required!"


def _migration_sql(migration_file: Path) -> str:
    module = ast.parse(Path(migration_file).read_text(encoding="utf-8"))
    for item in module.body:
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "upgrade":
            for statement in item.body:
                if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant):
                    if isinstance(statement.value.value, str):
                        return statement.value.value
    raise RuntimeError("managed migration does not contain a static upgrade SQL payload")


def initialize_database(db_file: Path, migration_file: Path | None = None) -> None:
    """Create only the schema and administrator needed before SUT startup."""
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        if migration_file is not None:
            connection.executescript(_migration_sql(migration_file))
        else:
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS "user" (
                "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "username" VARCHAR(20) NOT NULL UNIQUE,
                "alias" VARCHAR(30),
                "email" VARCHAR(255) NOT NULL UNIQUE,
                "phone" VARCHAR(20),
                "password" VARCHAR(128),
                "is_active" INT NOT NULL DEFAULT 1,
                "is_superuser" INT NOT NULL DEFAULT 0,
                "last_login" TIMESTAMP,
                "dept_id" INT
            );
            """
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO "user"
                (username, email, password, is_active, is_superuser, dept_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("admin", "admin@benchmark.invalid", _DISABLED_PASSWORD, 1, 1, None),
        )


def install_runtime_password(db_file: Path, password: str) -> None:
    """Replace the disabled seed value inside the locked runtime environment."""
    from argon2 import PasswordHasher

    encoded = PasswordHasher().hash(password)
    with sqlite3.connect(db_file) as connection:
        updated = connection.execute(
            """
            UPDATE "user" SET password = ?
            WHERE username = ? AND email = ? AND password = ? AND is_superuser = 1
            """,
            (encoded, "admin", "admin@benchmark.invalid", _DISABLED_PASSWORD),
        )
        if updated.rowcount != 1:
            raise RuntimeError("managed disabled administrator identity does not match")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("db_file", type=Path)
    parser.add_argument("--migration", type=Path)
    parser.add_argument("--password-env")
    arguments = parser.parse_args()
    initialize_database(arguments.db_file, arguments.migration)
    if arguments.password_env:
        import os

        install_runtime_password(arguments.db_file, os.environ[arguments.password_env])
