"""Domain key derivation for allocation, approval, apply, and coverage repair."""

from __future__ import annotations

import hashlib

from graph_engine.canonical import canonical_digest


def derive_allocation_ids(
    *,
    change_id: str,
    source_batch_id: str,
    entry_batch_id: str,
    candidate_digest: str,
    attempt_number: int,
) -> dict[str, object]:
    episode_id = hashlib.sha256(f"{change_id}:{entry_batch_id}:{candidate_digest}".encode()).hexdigest()
    operation_id = hashlib.sha256(
        f"{source_batch_id}:{candidate_digest}:{attempt_number}".encode()
    ).hexdigest()
    return {
        "episode_id": episode_id,
        "operation_id": operation_id,
        "attempt_id": f"ha-{episode_id[:12]}-{attempt_number}",
        "attempt_number": attempt_number,
        "source_batch_id": source_batch_id,
        "entry_batch_id": entry_batch_id,
    }


def derive_approval_id(
    *,
    owner_id: str,
    candidate_digest: str,
    baseline_digest: str,
    policy_digest: str,
    proposal_digest: str,
) -> str:
    return canonical_digest(
        {
            "baseline_digest": baseline_digest,
            "candidate_digest": candidate_digest,
            "owner_id": owner_id,
            "policy_digest": policy_digest,
            "proposal_digest": proposal_digest,
        }
    )


def derive_heal_record_key(
    *,
    owner_id: str,
    write_set_id: str,
    candidate_digest: str,
    safety_payload_digest: str,
    target: str,
) -> str:
    return canonical_digest(
        {
            "candidate_digest": candidate_digest,
            "owner_id": owner_id,
            "safety_payload_digest": safety_payload_digest,
            "target": target,
            "write_set_id": write_set_id,
        }
    )


def mint_coverage_attempt_token(
    *,
    change_id: str,
    attempt: int,
    test_tree_sha256: str,
    product_tree_sha256: str,
    declaration_tree_sha256: str,
) -> str:
    preimage = f"{change_id}:{attempt}:{test_tree_sha256}:{product_tree_sha256}:{declaration_tree_sha256}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]
