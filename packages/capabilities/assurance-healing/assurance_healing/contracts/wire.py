"""Shared wire helpers for healing-owned contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from graph_engine.canonical import canonical_digest

HEX_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
HexDigest = Annotated[str, Field(pattern=HEX_DIGEST_PATTERN)]
_SHA256_PREFIXED_LENGTH = len("sha256:") + 64

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class StrictWireModel(BaseModel):
    """Immutable, non-coercing base for canonical JSON wire artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FrozenContract(BaseModel):
    model_config = _FROZEN


def validate_repo_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("must be a normalized repository-relative POSIX path")
    return value


def validate_prefixed_sha256(value: str) -> str:
    if (
        len(value) != _SHA256_PREFIXED_LENGTH
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value.removeprefix("sha256:"))
    ):
        raise ValueError("must be a lowercase sha256:<64-hex> digest")
    return value


def validate_canonical_strings(values: Sequence[str], *, label: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{label} must not contain empty values")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    if list(values) != sorted(values):
        raise ValueError(f"{label} must be canonically sorted")


def override_token_digest(
    *,
    policy_digest: str,
    candidate_digest: str,
    change_id: str,
) -> str:
    return canonical_digest(
        {
            "candidate_digest": candidate_digest,
            "change_id": change_id,
            "policy_digest": policy_digest,
        }
    )


def heal_apply_intent_digest(payload: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"idempotency_key", "intent_digest", "settlement_key"}
    }
    return canonical_digest(body)


def execution_evidence_binding_digest(
    *,
    baseline_tree_id: str,
    mapping_digest: str,
    receipt_digest: str,
) -> str:
    return canonical_digest(
        {
            "baseline_tree_id": baseline_tree_id,
            "mapping_digest": mapping_digest,
            "receipt_digest": receipt_digest,
        }
    )
