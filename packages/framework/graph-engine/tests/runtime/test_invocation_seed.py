from __future__ import annotations

import pytest
from pydantic import ValidationError

from graph_engine.canonical import canonical_digest
from graph_engine.attempts.secret_sources import empty_runtime_authorization, runtime_authorization_digest
from graph_engine.runtime.seed import (
    EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
    InvocationSeed,
    empty_invocation_seed,
)


def test_seed_models_reject_root_input_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="root_input_digest does not match root_input"):
        InvocationSeed(
            schema_version="2",
            root_input={"value": 1},
            root_input_digest="0" * 64,
        )


def test_empty_invocation_seed_is_canonical() -> None:
    seed = empty_invocation_seed(root_input={"hello": "world"})
    assert seed.schema_version == "2"
    assert seed.root_input_digest == canonical_digest({"hello": "world"})
    assert not hasattr(seed, "workspace")
    assert EMPTY_RUNTIME_AUTHORIZATION_DIGEST == runtime_authorization_digest(())
    assert EMPTY_RUNTIME_AUTHORIZATION_DIGEST == empty_runtime_authorization().digest
