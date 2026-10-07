"""Validate retained dispatch evidence before releasing an exited owner's resources."""

from __future__ import annotations
import json
from pathlib import Path
from assurance_product.worker_state import ExecutionConflict, WorkerRecord


def validated_stop_checkpoint(owner: WorkerRecord) -> Path | None:
    """Reject unsafe paths and lost dispatch evidence before stop opens SQLite."""
    import sqlite3
    import stat
    from assurance_product.change_workspace import require_real_directory

    workspace = require_real_directory(Path(owner["workspace"]))
    db = workspace / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    evidence = bool(owner.get("attempts") or owner.get("calls") or owner.get("children"))
    for path in (workspace / "qa", db.parent.parent, db.parent, db):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if evidence:
                raise ExecutionConflict(
                    "registered execution persistence is missing; unsupported cleanup"
                ) from None
            return None
        required = stat.S_ISREG if path == db else stat.S_ISDIR
        if not required(mode):
            raise ExecutionConflict("stop checkpoint and runtime ancestors must be real paths")
    if owner.get("attempts") or owner.get("calls"):
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            generations = {
                str(row[0])
                for row in connection.execute(
                    "SELECT attempt_key_digest FROM assurance_attempt_generations WHERE owner_nonce = ?",
                    (owner["nonce"],),
                )
            }
            calls = {
                str(row[0])
                for row in connection.execute(
                    "SELECT call_digest FROM assurance_host_calls WHERE owner_nonce = ?", (owner["nonce"],)
                )
            }
            if not set(owner.get("attempts", [])).issubset(generations) or not set(
                owner.get("calls", [])
            ).issubset(calls):
                raise ExecutionConflict("registered execution rows are missing; unsupported cleanup")
        except sqlite3.DatabaseError as error:
            raise ExecutionConflict(
                "registered execution persistence is incomplete; unsupported cleanup"
            ) from error
        finally:
            connection.close()
    return db


def cleanup_owned_resources(owner: WorkerRecord) -> None:
    import sqlite3
    from assurance_product.bootstrap.resources import release_active_resource_authorizations

    db = validated_stop_checkpoint(owner)
    if db is None:
        return
    connection = sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    try:
        rows = connection.execute(
            "SELECT scope, attempt_key_digest, ordinal, input_payload FROM assurance_attempt_generations WHERE owner_nonce = ?",
            (owner["nonce"],),
        ).fetchall()
        attempts = set()
        for scope, key, ordinal, input_payload in rows:
            decoded = json.loads(bytes(scope))
            if decoded.get("invocation_id") != owner["invocation"]:
                raise ExecutionConflict("generation owner disagrees with Invocation")
            from graph_engine.attempts.keys import AttemptIdentity

            try:
                identity = AttemptIdentity.from_scope(decoded)
                retained_key = identity.derive_key(
                    json.loads(bytes(input_payload)), technical_attempt=ordinal
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ExecutionConflict(
                    "generation activation identity is unreconstructible; unsupported cleanup"
                ) from error
            if retained_key.digest != key:
                raise ExecutionConflict("generation key disagrees with retained activation/input")
            attempts.add(str(key))
        calls = connection.execute(
            "SELECT attempt_key_digest FROM assurance_host_calls WHERE owner_nonce = ?", (owner["nonce"],)
        ).fetchall()
        attempts.update(str(row[0]) for row in calls)
    finally:
        connection.close()
    validated_stop_checkpoint(owner)
    release_active_resource_authorizations(db, attempt_key_digests=attempts)
    validated_stop_checkpoint(owner)
    connection = sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    try:
        connection.execute(
            "UPDATE assurance_attempt_generations SET abandoned = 1 WHERE owner_nonce = ?", (owner["nonce"],)
        )
        connection.commit()
    finally:
        connection.close()


def has_owned_generations(owner: WorkerRecord) -> bool:
    import sqlite3

    if owner.get("attempts"):
        return True

    db = Path(owner["workspace"]) / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    if db.is_symlink() or not db.is_file():
        # Runtime validation rejects this before dispatch; never open a rejected
        # symlink here (even SQLite read-only connections may create WAL files).
        return False
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM assurance_attempt_generations WHERE owner_nonce = ? LIMIT 1", (owner["nonce"],)
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError:
        return True
    finally:
        connection.close()


def has_retained_calls(db: Path, owner: WorkerRecord) -> bool:
    """Read stop evidence without opening or migrating the runtime backend."""
    import sqlite3

    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM assurance_host_calls WHERE owner_nonce = ? LIMIT 1", (owner["nonce"],)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def assert_no_legacy_attempts(workspace: Path) -> None:
    """An ownerless workspace cannot adopt earlier unverified execution history."""
    db = workspace / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    if db.is_file():
        import sqlite3

        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'assurance_attempt_batches'"
            ).fetchone()
            if exists and connection.execute("SELECT 1 FROM assurance_attempt_batches LIMIT 1").fetchone():
                raise ExecutionConflict(
                    "legacy execution has no verifiable owner; unsupported resume, use a fresh isolated run"
                )
        finally:
            connection.close()
