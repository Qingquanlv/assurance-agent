from __future__ import annotations

import hashlib
from graph_engine.canonical import canonical_digest
from tests.phase4.conformance import execute_task

from assurance_agent.workflow.healing.operations import derive_allocation_ids as legacy_derive_allocation_ids
from assurance_healing.operations.keys import (
    derive_allocation_ids,
    derive_approval_id,
    derive_heal_record_key,
    mint_coverage_attempt_token,
)
from assurance_healing.operations.proposal import AllocateHealingAttemptHandler, FixerDispatchHandler
from assurance_healing.operations.safety import CombineFixerSafetyHandler, override_decision
from assurance_healing.operations.status import ProjectEpisodeHandler, RecordCoverageRepairStatusHandler
from healing_fixtures import as_object  # pyright: ignore[reportMissingImports]

_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64


def _legacy_allocation_key(batch_id: str, proposal_sha: str, attempt_number: int) -> str:
    return hashlib.sha256(f"{batch_id}:{proposal_sha}:{attempt_number}".encode()).hexdigest()


def _independent_allocation_ids(
    *,
    change_id: str,
    batch_id: str,
    proposal_sha: str,
    attempt_number: int,
) -> dict[str, object]:
    episode_id = hashlib.sha256(f"{change_id}:{batch_id}:{proposal_sha}".encode()).hexdigest()
    operation_id = hashlib.sha256(f"{batch_id}:{proposal_sha}:{attempt_number}".encode()).hexdigest()
    return {
        "episode_id": episode_id,
        "operation_id": operation_id,
        "attempt_id": f"ha-{episode_id[:12]}-{attempt_number}",
        "attempt_number": attempt_number,
    }


def _independent_approval_id(
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


def _independent_record_key(
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


def _independent_safety(flags: dict[str, bool]) -> dict[str, bool]:
    needs_review = flags["skip_or_xfail_added"] or flags["high_risk_proposal_applied"]
    passed = not (
        flags["product_code_modified"]
        or flags["skip_or_xfail_added"]
        or flags["unrelated_tests_modified"]
        or flags["assertion_expected_value_changes_detected"]
        or flags["high_risk_proposal_applied"]
    )
    return {"passed": passed, "needs_review": needs_review}


async def test_allocation_keys_match_independent_and_legacy_formulas() -> None:
    change_id = "CH-DEMO-001"
    batch_id = "batch-1"
    proposal_sha = "c" * 64
    attempt_number = 2
    independent = _independent_allocation_ids(
        change_id=change_id,
        batch_id=batch_id,
        proposal_sha=proposal_sha,
        attempt_number=attempt_number,
    )
    current = derive_allocation_ids(
        change_id=change_id,
        source_batch_id=batch_id,
        entry_batch_id=batch_id,
        candidate_digest=proposal_sha,
        attempt_number=attempt_number,
    )
    legacy = legacy_derive_allocation_ids(
        change_id=change_id,
        batch_id=batch_id,
        proposal_sha=proposal_sha,
        attempt_number=attempt_number,
    )
    assert current["operation_id"] == independent["operation_id"]
    assert current["episode_id"] == independent["episode_id"]
    assert current["attempt_id"] == independent["attempt_id"]
    assert legacy["operation_id"] == _legacy_allocation_key(batch_id, proposal_sha, attempt_number)
    assert current["operation_id"] == legacy["operation_id"]
    outcome = await execute_task(
        AllocateHealingAttemptHandler(),
        {
            "change_id": change_id,
            "owner_id": "assurance.healing",
            "attempt_number": attempt_number,
            "source_batch_id": batch_id,
            "entry_batch_id": batch_id,
            "candidate_digest": proposal_sha,
            "baseline_digest": _HEX_A,
            "policy_digest": _HEX_B,
            "execution_evidence_digest": _HEX_C,
            "prior_operation_ids": ["prior"],
        },
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["operation_id"] == independent["operation_id"]


def test_approval_and_record_keys_match_independent_formulas() -> None:
    approval = derive_approval_id(
        owner_id="assurance.healing",
        candidate_digest=_HEX_A,
        baseline_digest=_HEX_B,
        policy_digest=_HEX_C,
        proposal_digest="d" * 64,
    )
    assert approval == _independent_approval_id(
        owner_id="assurance.healing",
        candidate_digest=_HEX_A,
        baseline_digest=_HEX_B,
        policy_digest=_HEX_C,
        proposal_digest="d" * 64,
    )
    record = derive_heal_record_key(
        owner_id="assurance.healing",
        write_set_id="ws-1",
        candidate_digest=_HEX_A,
        safety_payload_digest=_HEX_B,
        target="api",
    )
    assert record == _independent_record_key(
        owner_id="assurance.healing",
        write_set_id="ws-1",
        candidate_digest=_HEX_A,
        safety_payload_digest=_HEX_B,
        target="api",
    )


async def test_safety_verdict_and_override_decision_match_independent_rules() -> None:
    flags = {
        "product_code_modified": False,
        "skip_or_xfail_added": True,
        "unrelated_tests_modified": False,
        "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
    }
    independent = _independent_safety(flags)
    outcome = await execute_task(
        CombineFixerSafetyHandler(),
        {
            "fragments": [
                {
                    **flags,
                    "schema_version": "1",
                    "applied_proposal_count": 1,
                    "passed": False,
                    "needs_review": True,
                }
            ]
        },
    )
    assert outcome.status == "succeeded"
    assert as_object(outcome.output)["passed"] == independent["passed"]
    assert as_object(outcome.output)["needs_review"] == independent["needs_review"]
    assert override_decision(require_approval=True, token_valid=True) == "allow"
    assert override_decision(require_approval=True, token_valid=False) == "deny"
    assert override_decision(require_approval=False, token_valid=False) == "allow"


async def test_proposal_completion_dispatch_and_episode_projection() -> None:
    dispatch = await execute_task(
        FixerDispatchHandler(),
        {
            "proposal": {
                "schema_version": "1",
                "summary": {"eligible_count": 1},
                "proposals": [
                    {
                        "proposal_id": "P1",
                        "target": "api",
                        "eligible": True,
                        "risk_level": "low",
                        "needs_review": False,
                        "files_to_modify": ["tests/api/test_users.py"],
                    },
                    {
                        "proposal_id": "P2",
                        "target": "e2e",
                        "eligible": False,
                        "risk_level": "high",
                        "needs_review": True,
                        "files_to_modify": [],
                    },
                ],
            }
        },
    )
    assert dispatch.status == "succeeded"
    assert as_object(dispatch.output)["active_targets"] == ["api"]
    projected = await execute_task(
        ProjectEpisodeHandler(),
        {
            "events": [
                {
                    "type": "healing_attempt_allocated_v2",
                    "seq": 2,
                    "episode_id": "ep-1",
                    "attempt_id": "ha-1",
                    "attempt_number": 1,
                    "operation_id": "op-1",
                    "source_batch_id": "batch-1",
                    "entry_batch_id": "batch-1",
                    "baseline_sha256": _HEX_A,
                    "baseline_embedded": True,
                }
            ]
        },
    )
    assert as_object(projected.output)["episode_id"] == "ep-1"
    assert as_object(projected.output)["attempts_used"] == 1


async def test_repair_status_and_attempt_token_match_independent_rules() -> None:
    token = mint_coverage_attempt_token(
        change_id="CH-DEMO-001",
        attempt=1,
        test_tree_sha256=_HEX_A,
        product_tree_sha256=_HEX_B,
        declaration_tree_sha256=_HEX_C,
    )
    independent = hashlib.sha256(f"CH-DEMO-001:1:{_HEX_A}:{_HEX_B}:{_HEX_C}".encode()).hexdigest()[:16]
    assert token == independent
    recorded = await execute_task(
        RecordCoverageRepairStatusHandler(),
        {"change_id": "CH-DEMO-001", "status": "exhausted", "attempts_used": 3},
    )
    assert as_object(recorded.output)["status"] == "exhausted"
    assert as_object(recorded.output)["attempts_used"] == 3


def test_characterization_does_not_import_quality() -> None:
    from healing_fixtures import imported_symbols  # pyright: ignore[reportMissingImports]

    names = imported_symbols("assurance_healing")
    assert "assurance_quality" not in names
    assert "quality" not in names
