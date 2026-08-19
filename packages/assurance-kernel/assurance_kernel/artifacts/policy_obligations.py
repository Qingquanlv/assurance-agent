"""Code-owned deferred policy obligations that are not gate consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DeferredPolicyObligation:
    field: str
    owner: str
    status: Literal["deferred"]
    reason: str


DEFERRED_POLICY_OBLIGATIONS = {
    "fuzz.required_when_endpoint_has_auth": DeferredPolicyObligation(
        field="fuzz.required_when_endpoint_has_auth",
        owner="fuzz-auth-applicability",
        status="deferred",
        reason="no_typed_endpoint_auth_fact",
    )
}


__all__ = ["DEFERRED_POLICY_OBLIGATIONS", "DeferredPolicyObligation"]
