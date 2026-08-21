import re

from graph_engine.canonical import canonical_digest
from graph_engine.errors import GraphEngineError


class IdentifierError(GraphEngineError):
    """Raised when an identifier is not a qualified identifier."""


_QUALIFIED_ID = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$")


def validate_qualified_id(value: str) -> str:
    if not _QUALIFIED_ID.fullmatch(value):
        raise IdentifierError(f"invalid qualified identifier: {value!r}")
    return value


def canonical_id(*parts: str) -> str:
    if not parts or any(not isinstance(part, str) for part in parts):
        raise ValueError("canonical id parts must be non-empty strings")
    return canonical_digest(list(parts))
