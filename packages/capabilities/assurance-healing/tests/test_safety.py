from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import CandidateFile, CandidateWriteSet, ResourceClaims, ValidationContext
from tests.phase4.conformance import execute_task

from assurance_healing.plugin import HealingPlugin
from assurance_healing.validators.override import OverrideValidator
from assurance_healing.validators.repair import RepairCandidateValidator
from assurance_healing.validators.test_tree import TestTreeValidator
from assurance_healing.operations.authority import FixerAuthorityReadyHandler
from assurance_healing.operations.safety import CombineFixerSafetyHandler, ComputeCoverageRepairSafetyHandler
from assurance_healing.operations.status import (
    AllocateCoverageRepairAttemptHandler,
    ProjectEpisodeHandler,
    RecordCoverageRepairStatusHandler,
    RecordHealingStatusHandler,
)
from healing_fixtures import as_object  # pyright: ignore[reportMissingImports]

_HEX_A = "a" * 64
_HEX_B = "b" * 64


def candidate(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_HEX_A) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def test_test_tree_validator_permits_only_approved_test_changes() -> None:
    validator = TestTreeValidator(
        allowed_test_roots=("tests",),
        forbidden_product_roots=("app", "src"),
        require_approval=True,
        approved=False,
    )
    rejected = validator.validate(candidate("tests/api/test_users.py"), validation_context())
    assert rejected.accepted is False
    approved = TestTreeValidator(
        allowed_test_roots=("tests",),
        forbidden_product_roots=("app", "src"),
        require_approval=True,
        approved=True,
    )
    assert approved.validate(candidate("tests/api/test_users.py"), validation_context()).accepted is True
    product = approved.validate(candidate("app/main.py"), validation_context())
    assert product.accepted is False


def test_override_validator_checks_exact_token() -> None:
    from assurance_healing.contracts.wire import override_token_digest

    token = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "action": "allow_test_changes",
        "reason": "approved",
        "policy_digest": _HEX_A,
        "candidate_digest": _HEX_B,
        "token_digest": override_token_digest(
            change_id="CH-DEMO-001",
            policy_digest=_HEX_A,
            candidate_digest=_HEX_B,
        ),
    }
    accepted = OverrideValidator(
        expected_change_id="CH-DEMO-001",
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
        file_bytes={"healing/override-token.json": __import__("json").dumps(token).encode()},
    ).validate(candidate("healing/override-token.json"), validation_context())
    assert accepted.accepted is True
    forged = dict(token)
    forged["change_id"] = "CH-OTHER"
    rejected = OverrideValidator(
        expected_change_id="CH-DEMO-001",
        policy_digest=_HEX_A,
        candidate_digest=_HEX_B,
        file_bytes={"healing/override-token.json": __import__("json").dumps(forged).encode()},
    ).validate(candidate("healing/override-token.json"), validation_context())
    assert rejected.accepted is False


def test_default_repair_and_override_validators_fail_closed() -> None:
    context = validation_context()
    repair = RepairCandidateValidator().validate(candidate("tests/api/x.py"), context)
    assert repair.accepted is False
    override = OverrideValidator().validate(candidate("healing/token.json"), context)
    assert override.accepted is False


def test_override_token_parse_failure_is_rejected() -> None:
    rejected = OverrideValidator(file_bytes={"healing/token.json": b"not-json"}).validate(
        candidate("healing/token.json"), validation_context()
    )
    assert rejected.accepted is False


def test_repair_candidate_authenticates_apply_summary() -> None:
    proposal = {
        "status": "approved",
        "proposals": [
            {
                "proposal_id": "P1",
                "files_to_modify": ["tests/api/test_other.py", "tests/api/test_users.py"],
            }
        ],
    }
    summary = {
        "schema_version": "1",
        "outcome": "applied",
        "proposal_ids": ["P1"],
        "claimed_modified_paths": ["tests/api/test_users.py"],
        "intent_sha256": f"sha256:{_HEX_A}",
        "write_set_id": "ws-1",
        "applied": True,
    }
    validator = RepairCandidateValidator(approved_proposal=proposal, apply_summary=summary)
    context = validation_context()
    assert validator.validate(candidate("tests/api/test_users.py"), context).accepted is True
    extra = validator.validate(candidate("tests/api/test_other.py"), context)
    assert extra.accepted is False


def test_repair_apply_summary_parse_failure_is_rejected() -> None:
    rejected = RepairCandidateValidator(
        approved_proposal={
            "status": "approved",
            "proposals": [{"proposal_id": "P1", "files_to_modify": ["tests/api/test_users.py"]}],
        },
        file_bytes={"healing/apply-summary.json": b"{"},
    ).validate(candidate("tests/api/test_users.py"), validation_context())
    assert rejected.accepted is False


def test_repair_candidate_requires_named_proposal_files() -> None:
    proposal = {
        "status": "approved",
        "proposals": [{"proposal_id": "P1", "files_to_modify": ["tests/api/test_users.py"]}],
    }
    validator = RepairCandidateValidator(approved_proposal=proposal, require_approval=True)
    assert validator.validate(candidate("tests/api/test_users.py"), validation_context()).accepted is True
    extra = validator.validate(candidate("tests/api/test_other.py"), validation_context())
    assert extra.accepted is False
    product = validator.validate(candidate("app/main.py"), validation_context())
    assert product.accepted is False


def test_plugin_validators_are_path_only() -> None:
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    tree = contribution.commit_validators["assurance.healing.validator.test-tree.v1"]
    override = contribution.commit_validators["assurance.healing.validator.override.v1"]
    repair = contribution.commit_validators["assurance.healing.validator.repair-candidate.v1"]
    context = validation_context()
    allowed = candidate("tests/api/test_users.py")
    assert tree.validate(allowed, context).accepted is True
    assert override.validate(candidate("healing/override-token.json"), context).accepted is True
    assert repair.validate(allowed, context).accepted is True
    assert tree.validate(candidate("src/app.py"), context).accepted is False
    assert repair.validate(candidate("app/main.py"), context).accepted is False


async def test_authority_ready_and_safety_combine() -> None:
    stop = await execute_task(
        FixerAuthorityReadyHandler(),
        {"authority": {"schema_version": "1", "change_id": "CH-DEMO-001", "targets": []}, "proposal": {}},
    )
    assert stop.status == "stopped"
    combine = await execute_task(
        CombineFixerSafetyHandler(),
        {
            "fragments": [
                {
                    "schema_version": "1",
                    "passed": True,
                    "needs_review": False,
                    "product_code_modified": False,
                    "skip_or_xfail_added": False,
                    "unrelated_tests_modified": False,
                    "assertion_expected_value_changes_detected": False,
                    "high_risk_proposal_applied": False,
                    "applied_proposal_count": 1,
                }
            ]
        },
    )
    assert combine.status == "succeeded"
    assert as_object(combine.output)["passed"] is True


async def test_status_and_coverage_repair_handlers() -> None:
    status = await execute_task(
        RecordHealingStatusHandler(),
        {"change_id": "CH-DEMO-001", "status": "pending", "attempts_used": 1},
    )
    assert status.status == "succeeded"
    assert as_object(status.output)["status"] == "pending"
    brief = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "batch_id": "batch-1",
        "probe_verdict": "pass",
        "eligible": True,
        "allowed_test_files": ["tests/api/test_users.py"],
        "repair_items": [
            {
                "kind": "uncovered_required_case",
                "locator": {"case_id": "TC_A"},
                "metric": "case_coverage",
            }
        ],
    }
    allocated = await execute_task(
        AllocateCoverageRepairAttemptHandler(),
        {
            "change_id": "CH-DEMO-001",
            "brief": brief,
            "test_tree_sha256": _HEX_A,
            "test_files_sha256": {"tests/api/test_users.py": _HEX_B},
            "product_tree_sha256": _HEX_A,
            "product_files_sha256": {},
            "declaration_tree_sha256": _HEX_A,
            "declaration_files_sha256": {},
            "prior_attempts_used": 0,
        },
    )
    assert allocated.status == "succeeded"
    assert as_object(allocated.output)["attempts_used"] == 1
    recorded = await execute_task(
        RecordCoverageRepairStatusHandler(),
        {"change_id": "CH-DEMO-001", "status": "repaired", "attempts_used": 1},
    )
    assert recorded.status == "succeeded"
    safety = await execute_task(
        ComputeCoverageRepairSafetyHandler(),
        {
            "change_id": "CH-DEMO-001",
            "attempt": 1,
            "brief": brief,
            "baseline": {
                "schema_version": "1",
                "change_id": "CH-DEMO-001",
                "attempt": 1,
                "attempt_token": "token-1",
                "test_tree_sha256": _HEX_A,
                "test_files_sha256": {"tests/api/test_users.py": _HEX_B},
                "product_tree_sha256": _HEX_A,
                "product_files_sha256": {},
                "declaration_tree_sha256": _HEX_A,
                "declaration_files_sha256": {},
            },
            "current_test_files": {"tests/api/test_users.py": _HEX_B},
            "current_product_files": {},
            "current_declaration_files": {},
            "summary": {
                "schema_version": "1",
                "change_id": "CH-DEMO-001",
                "attempt": 1,
                "attempt_token": "token-1",
                "applied": False,
                "files_modified": [],
            },
        },
    )
    assert safety.status == "succeeded"
    assert as_object(safety.output)["passed"] is True
    projected = await execute_task(
        ProjectEpisodeHandler(),
        {
            "events": [
                {
                    "type": "healing_attempt_allocated_v2",
                    "seq": 1,
                    "episode_id": "ep-1",
                    "attempt_id": "ha-1",
                    "attempt_number": 1,
                    "operation_id": "op-1",
                    "source_batch_id": "batch-1",
                    "entry_batch_id": "batch-1",
                    "baseline_digest": _HEX_A,
                    "baseline_embedded": True,
                }
            ]
        },
    )
    assert projected.status == "succeeded"
    assert as_object(projected.output)["attempts_used"] == 1
