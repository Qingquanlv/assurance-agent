from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.attempts.resources.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    runtime_authorization_digest,
)


def test_runtime_authorization_rejects_duplicate_handles() -> None:
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="environment",
        source_locator="OPENCODE_TOKEN",
    )
    with pytest.raises(ValueError, match="unique"):
        InvocationRuntimeAuthorization(
            schema_version="1",
            secret_sources=(binding, binding),
            digest=runtime_authorization_digest((binding,)),
        )


def test_runtime_authorization_rejects_digest_mismatch() -> None:
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="environment",
        source_locator="OPENCODE_TOKEN",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        InvocationRuntimeAuthorization(
            schema_version="1",
            secret_sources=(binding,),
            digest="0" * 64,
        )


def test_serialized_artifacts_contain_handles_not_secret_bytes(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_bytes(b"actual-secret")
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="file",
        source_locator=str(secret_path.resolve()),
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(binding,),
        digest=runtime_authorization_digest((binding,)),
    )
    encoded = repr(
        {
            "schema_version": authorization.schema_version,
            "secret_sources": [
                {
                    "handle": item.handle,
                    "source_kind": item.source_kind,
                    "source_locator": item.source_locator,
                }
                for item in authorization.secret_sources
            ],
            "digest": authorization.digest,
        }
    ).encode()
    assert b"actual-secret" not in encoded
    assert b"opencode.token" in encoded
