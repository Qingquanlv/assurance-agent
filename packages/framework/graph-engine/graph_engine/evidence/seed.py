from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from pydantic_core import InitErrorDetails

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.attempts.secret_sources import (
    EMPTY_RUNTIME_AUTHORIZATION,
    EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validation_error(title: str, loc: tuple[str | int, ...], message: str, value: object) -> ValidationError:
    return ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(
                type="value_error",
                loc=loc,
                input=value,
                ctx={"error": ValueError(message)},
            )
        ],
    )


def _validate_sha256(value: str, *, loc: tuple[str | int, ...], title: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise _validation_error(title, loc, "invalid sha256 digest", value)
    return value


@dataclass(frozen=True)
class InvocationSeed:
    schema_version: Literal["2"]
    root_input: JSONValue
    root_input_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "2":
            raise _validation_error(
                "InvocationSeed",
                ("schema_version",),
                "unsupported invocation seed schema version",
                self.schema_version,
            )
        computed_root_input_digest = canonical_digest(self.root_input)
        _validate_sha256(computed_root_input_digest, loc=("root_input_digest",), title="InvocationSeed")
        if self.root_input_digest != computed_root_input_digest:
            raise _validation_error(
                "InvocationSeed",
                ("root_input_digest",),
                "root_input_digest does not match root_input",
                self.root_input_digest,
            )
        _validate_sha256(self.root_input_digest, loc=("root_input_digest",), title="InvocationSeed")


def empty_invocation_seed(*, root_input: JSONValue | None = None) -> InvocationSeed:
    """Deterministic root-only invocation seed for runtime tests."""

    input_value: JSONValue = {} if root_input is None else root_input
    return InvocationSeed(
        schema_version="2",
        root_input=input_value,
        root_input_digest=canonical_digest(input_value),
    )


__all__ = [
    "EMPTY_RUNTIME_AUTHORIZATION",
    "EMPTY_RUNTIME_AUTHORIZATION_DIGEST",
    "InvocationSeed",
    "empty_invocation_seed",
]
