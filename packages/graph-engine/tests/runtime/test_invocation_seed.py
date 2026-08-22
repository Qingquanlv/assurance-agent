from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from graph_engine.canonical import canonical_digest
from graph_engine.runtime.seed import (
    EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
    InvocationSeed,
    SeedFile,
    WorkspaceSeed,
    empty_invocation_seed,
    workspace_tree_id,
)


def test_seed_models_reject_duplicate_and_unsafe_paths() -> None:
    with pytest.raises(ValidationError, match="relative canonical path"):
        SeedFile(path="../secret", sha256="0" * 64, content=b"x")


def test_seed_models_reject_sha256_mismatch() -> None:
    with pytest.raises(ValidationError, match="sha256 does not match content"):
        SeedFile(path="note.txt", sha256="0" * 64, content=b"actual")


def test_seed_models_reject_duplicate_workspace_paths() -> None:
    file_a = SeedFile(path="a.txt", sha256=hashlib.sha256(b"a").hexdigest(), content=b"a")
    file_b = SeedFile(path="a.txt", sha256=hashlib.sha256(b"b").hexdigest(), content=b"b")
    with pytest.raises(ValidationError, match="duplicate workspace seed path"):
        WorkspaceSeed(
            schema_version="1",
            tree_id=workspace_tree_id({file_a.path: file_a.sha256}),
            files=(file_a, file_b),
        )


def test_seed_models_reject_tree_id_mismatch() -> None:
    file = SeedFile(path="a.txt", sha256=hashlib.sha256(b"a").hexdigest(), content=b"a")
    with pytest.raises(ValidationError, match="tree_id does not match files"):
        WorkspaceSeed(schema_version="1", tree_id="0" * 64, files=(file,))


def test_seed_models_reject_root_input_digest_mismatch() -> None:
    tree_id = workspace_tree_id({})
    with pytest.raises(ValidationError, match="root_input_digest does not match root_input"):
        InvocationSeed(
            schema_version="1",
            root_input={"value": 1},
            root_input_digest="0" * 64,
            workspace=WorkspaceSeed(schema_version="1", tree_id=tree_id, files=()),
        )


def test_empty_invocation_seed_is_canonical() -> None:
    seed = empty_invocation_seed(root_input={"hello": "world"})
    assert seed.root_input_digest == canonical_digest({"hello": "world"})
    assert seed.workspace.tree_id == workspace_tree_id({})
    assert EMPTY_RUNTIME_AUTHORIZATION_DIGEST == canonical_digest(
        {"schema_version": "1", "bindings": []}
    )
