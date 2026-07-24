"""Capability presence checks for plan-review gates (spec C7)."""

from __future__ import annotations

from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import (
    AccountLeaf,
    AuthLeaf,
    CapabilityLeaf,
    CleanupLeaf,
    EntityLeaf,
)


def _get_nested(doc: dict, parts: list[str]) -> object | None:
    cur: object = doc
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def is_leaf_present(dk: dict, dotted: str) -> bool:
    """Return whether a dotted key resolves to a valid canonical leaf in L1."""
    parts = dotted.split(".")
    value = _get_nested(dk, parts)
    if not isinstance(value, dict):
        return False
    try:
        if dotted.startswith("auth."):
            AuthLeaf.model_validate(value)
        elif dotted.startswith("accounts."):
            AccountLeaf.model_validate(value)
        elif dotted.startswith("entities."):
            EntityLeaf.model_validate(value)
        elif dotted.startswith("capabilities.cleanup."):
            CleanupLeaf.model_validate(value)
        elif dotted.startswith("capabilities.domain_factories.") or dotted.startswith(
            "capabilities.adapters."
        ):
            CapabilityLeaf.model_validate(value)
        else:
            return False
    except ValidationError:
        return False
    return True


def compute_missing_capabilities(review_doc: dict, dk_doc: dict) -> list[str]:
    """Compare review.required_capabilities against typed leaves in L1."""
    required = review_doc.get("required_capabilities")
    if not isinstance(required, list):
        return []
    missing: list[str] = []
    for item in required:
        if not isinstance(item, str) or not item.strip():
            missing.append(str(item))
            continue
        key = item.strip()
        if not is_leaf_present(dk_doc, key):
            missing.append(key)
    return missing


def capabilities_present(review_doc: object, dk_doc: object) -> bool:
    if not isinstance(review_doc, dict) or not isinstance(dk_doc, dict):
        return False
    return len(compute_missing_capabilities(review_doc, dk_doc)) == 0


def plan_review_route(node_result: object) -> str:
    """Split needs_human_review into knowledge_remediation when L1 leaves are missing."""
    if not isinstance(node_result, dict):
        return "stop"
    gate = node_result.get("gate")
    if not isinstance(gate, dict):
        return "stop"
    verdict = gate.get("verdict")
    if not isinstance(verdict, str):
        return "stop"
    details = gate.get("details")
    missing: object = None
    if isinstance(details, dict):
        missing = details.get("missing_capabilities")
    if verdict == "needs_human_review" and isinstance(missing, list) and len(missing) > 0:
        return "knowledge_remediation"
    return verdict
