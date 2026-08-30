from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from graph_engine.canonical import canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import IdentifierError, validate_qualified_id

_ENV_VAR_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class RuntimeAuthorizationError(GraphEngineError):
    """Raised when runtime secret authorization is invalid or incomplete."""


def _canonical_environment_locator(value: str) -> str:
    if not _ENV_VAR_PATTERN.fullmatch(value):
        raise ValueError("environment source locator must be a canonical variable name")
    return value


def _canonical_file_locator(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("file source locator must be an absolute path")
    return str(path.resolve())


def _validate_secret_handle(value: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid secret handle: {value!r}") from error


def _canonical_secret_sources(
    secret_sources: tuple[SecretSourceBinding, ...],
) -> tuple[tuple[str, Literal["environment", "file"], str], ...]:
    canonical: list[tuple[str, Literal["environment", "file"], str]] = []
    handles: list[str] = []
    for item in secret_sources:
        handle = _validate_secret_handle(item.handle)
        handles.append(handle)
        if item.source_kind == "environment":
            locator = _canonical_environment_locator(item.source_locator)
        elif item.source_kind == "file":
            locator = _canonical_file_locator(item.source_locator)
        else:  # pragma: no cover - closed literal
            raise ValueError(f"unsupported secret source kind: {item.source_kind!r}")
        canonical.append((handle, item.source_kind, locator))
    if len(handles) != len(set(handles)):
        raise ValueError("runtime authorization secret handles must be unique")
    return tuple(sorted(canonical))


def runtime_authorization_digest(
    secret_sources: tuple[SecretSourceBinding, ...],
) -> str:
    pairs = _canonical_secret_sources(secret_sources)
    if not pairs:
        return canonical_digest([])
    return canonical_digest([list(pair) for pair in pairs])


@dataclass(frozen=True)
class SecretSourceBinding:
    handle: str
    source_kind: Literal["environment", "file"]
    source_locator: str


@dataclass(frozen=True)
class InvocationRuntimeAuthorization:
    schema_version: Literal["1"]
    secret_sources: tuple[SecretSourceBinding, ...]
    digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("unsupported runtime authorization schema version")
        expected = runtime_authorization_digest(self.secret_sources)
        if self.digest != expected:
            raise ValueError("runtime authorization digest mismatch")
        canonical_pairs = _canonical_secret_sources(self.secret_sources)
        normalized = tuple(
            SecretSourceBinding(handle=handle, source_kind=source_kind, source_locator=locator)
            for handle, source_kind, locator in canonical_pairs
        )
        object.__setattr__(self, "secret_sources", normalized)

    @property
    def authorized_handles(self) -> tuple[str, ...]:
        return tuple(item.handle for item in self.secret_sources)


def empty_runtime_authorization() -> InvocationRuntimeAuthorization:
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(),
        digest=runtime_authorization_digest(()),
    )


def authorize_binding_secret_handles(
    binding_handles: tuple[str, ...],
    authorization: InvocationRuntimeAuthorization,
) -> tuple[str, ...]:
    """Return canonical binding handles after verifying authorization coverage."""

    declared = tuple(sorted(_validate_secret_handle(handle) for handle in binding_handles))
    if len(declared) != len(set(declared)):
        raise RuntimeAuthorizationError("binding secret handles must be unique")
    authorized = set(authorization.authorized_handles)
    missing = tuple(handle for handle in declared if handle not in authorized)
    if missing:
        raise RuntimeAuthorizationError(
            f"missing authorized secret handle(s) for binding: {', '.join(missing)}"
        )
    return declared


def resolve_secret_source(binding: SecretSourceBinding) -> bytes:
    """Resolve one authorized secret source at dispatch time; never persisted."""

    if binding.source_kind == "environment":
        value = os.environ.get(binding.source_locator)
        if value is None:
            raise RuntimeAuthorizationError(f"environment secret source is unset: {binding.source_locator}")
        return value.encode()
    if binding.source_kind == "file":
        return Path(binding.source_locator).read_bytes()
    raise RuntimeAuthorizationError(f"unsupported secret source kind: {binding.source_kind!r}")


EMPTY_RUNTIME_AUTHORIZATION = empty_runtime_authorization()
EMPTY_RUNTIME_AUTHORIZATION_DIGEST = EMPTY_RUNTIME_AUTHORIZATION.digest


__all__ = [
    "EMPTY_RUNTIME_AUTHORIZATION",
    "EMPTY_RUNTIME_AUTHORIZATION_DIGEST",
    "InvocationRuntimeAuthorization",
    "RuntimeAuthorizationError",
    "SecretSourceBinding",
    "authorize_binding_secret_handles",
    "empty_runtime_authorization",
    "resolve_secret_source",
    "runtime_authorization_digest",
]
