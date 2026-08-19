import json

import pytest

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.identifiers import IdentifierError, validate_qualified_id


def test_public_engine_api_version_is_explicit() -> None:
    assert ENGINE_API_VERSION == "1.0"


def test_canonical_json_is_order_independent() -> None:
    left = canonical_json_bytes({"b": 2, "a": [1, True, None]})
    right = canonical_json_bytes({"a": [1, True, None], "b": 2})
    assert left == right == b'{"a":[1,true,null],"b":2}'
    assert canonical_digest(json.loads(left)) == canonical_digest(json.loads(right))


@pytest.mark.parametrize("value", ["assurance.execution", "agent.opencode.execute"])
def test_qualified_ids_require_a_namespace(value: str) -> None:
    assert validate_qualified_id(value) == value


@pytest.mark.parametrize("value", ["run", "Operation:run", "a..b", "a/b", "a_b.c"])
def test_invalid_qualified_ids_fail(value: str) -> None:
    with pytest.raises(IdentifierError):
        validate_qualified_id(value)
