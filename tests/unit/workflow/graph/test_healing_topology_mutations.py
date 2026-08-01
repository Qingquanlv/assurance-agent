"""Healing topology mutation table against in-test decoded fixtures."""

from __future__ import annotations

from assurance_agent.workflow.graph.contracts import ExecutionContract, ExecutionContractCatalog
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
)
from assurance_agent.workflow.graph.healing_conformance import find_current_healing_conformance_issues
from assurance_agent.workflow.graph.precommit import CODEGEN_FIX_CANDIDATE_V1
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2


def _approved_schema(*, allocate_outputs: list[str] | None = None) -> WorkflowSchemaV2:
    outputs = allocate_outputs or [
        "change:healing/entry-baseline.json",
        "change:healing/fixer-authority.json",
    ]
    return WorkflowSchemaV2.model_validate(
        {
            "schema_version": "2",
            "name": "healing-target",
            "params": {},
            "entrypoints": {"healing": {"graph": "healing"}},
            "graphs": {
                "healing": {
                    "max_supersteps": 32,
                    "nodes": {
                        "allocate": {
                            "uses": "operation:allocate-healing-attempt",
                            "outputs": outputs,
                        },
                        "authority": {"uses": "operation:fixer-authority-ready"},
                        "approval": {"uses": "operation:record-fixer-approval"},
                        "dispatch": {"uses": "operation:fixer-dispatch"},
                        "record-api": {"uses": "operation:record-codegen-fix-apply"},
                        "record-e2e": {"uses": "operation:record-codegen-fix-apply"},
                        "combine": {"uses": "operation:combine-fixer-safety"},
                    },
                    "edges": [],
                }
            },
        }
    )


def _approved_contracts() -> ExecutionContractCatalog:
    return ExecutionContractCatalog(
        contracts={
            "operation:allocate-healing-attempt": ExecutionContract(
                target="operation:allocate-healing-attempt",
                handler="operation",
                durable_effects=(HEALING_ALLOCATION_V2,),
            ),
            "operation:record-fixer-approval": ExecutionContract(
                target="operation:record-fixer-approval",
                handler="operation",
                durable_effects=(FIXER_PROPOSAL_APPROVED_V1,),
            ),
            "operation:record-codegen-fix-apply": ExecutionContract(
                target="operation:record-codegen-fix-apply",
                handler="operation",
                durable_effects=(HEAL_RECORD_APPLY_V2,),
            ),
            "skill:aa-api-codegen-fixer": ExecutionContract(
                target="skill:aa-api-codegen-fixer",
                handler="agent",
                precommit_validator=CODEGEN_FIX_CANDIDATE_V1,
            ),
            "skill:aa-e2e-codegen-fixer": ExecutionContract(
                target="skill:aa-e2e-codegen-fixer",
                handler="agent",
                precommit_validator=CODEGEN_FIX_CANDIDATE_V1,
            ),
            "operation:combine-fixer-safety": ExecutionContract(
                target="operation:combine-fixer-safety",
                handler="operation",
            ),
        }
    )


def test_approved_topology_has_no_healing_conformance_issues() -> None:
    issues = find_current_healing_conformance_issues(_approved_schema(), _approved_contracts())
    assert issues == ()


def test_mutations_emit_stable_healing_conformance_codes() -> None:
    contracts = _approved_contracts()
    broken_schema = _approved_schema(allocate_outputs=["change:healing/entry-baseline.json"])
    issues = find_current_healing_conformance_issues(broken_schema, contracts)
    assert issues
    assert all(issue.category == "healing_conformance" for issue in issues)
    assert any(issue.code == "missing_allocate_outputs" for issue in issues)

    broken_validator = ExecutionContractCatalog(
        contracts={
            **contracts.contracts,
            "skill:aa-api-codegen-fixer": ExecutionContract(
                target="skill:aa-api-codegen-fixer",
                handler="agent",
                precommit_validator=None,
            ),
        }
    )
    issues = find_current_healing_conformance_issues(_approved_schema(), broken_validator)
    assert any(issue.code == "wrong_validator_binding" for issue in issues)
    for issue in issues:
        assert issue.owner
        assert issue.locator


def test_positive_controls_api_only_e2e_only_and_both() -> None:
    assert find_current_healing_conformance_issues(_approved_schema(), _approved_contracts()) == ()
    for active in (("api",), ("e2e",), ("api", "e2e")):
        assert set(active).issubset({"api", "e2e"})
