"""Healing topology mutation table against in-test decoded fixtures."""

from __future__ import annotations

from typing import Literal

from assurance_agent.workflow.graph.contracts import ExecutionContract, ExecutionContractCatalog
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
)
from assurance_agent.workflow.graph.healing_conformance import find_current_healing_conformance_issues
from assurance_agent.workflow.graph.precommit import CODEGEN_FIX_CANDIDATE_V1
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2


def _record_outputs(target: Literal["api", "e2e"]) -> list[str]:
    return [
        f"change:healing/{target}-apply-summary.json",
        f"change:healing/{target}-fixer-safety-check.json",
    ]


def _approved_schema(
    *,
    active_targets: tuple[Literal["api", "e2e"], ...] = ("api", "e2e"),
    allocate_outputs: list[str] | None = None,
    include_interrupt: bool = True,
    join_mode: str = "all_active",
    include_join: bool = True,
    omit_record_outputs: bool = False,
) -> WorkflowSchemaV2:
    outputs = allocate_outputs or [
        "change:healing/entry-baseline.json",
        "change:healing/fixer-authority.json",
    ]
    nodes: dict[str, object] = {
        "allocate": {
            "uses": "operation:allocate-healing-attempt",
            "outputs": outputs,
        },
        "authority": {"uses": "operation:fixer-authority-ready"},
        "approval": {
            "uses": "operation:record-fixer-approval",
            "outputs": ["change:healing/fixer-proposal-approval.json"],
        },
        "dispatch": {"uses": "operation:fixer-dispatch"},
        "combine": {
            "uses": "operation:combine-fixer-safety",
            "outputs": ["change:healing/fixer-safety-check.json"],
        },
    }
    record_ids: list[str] = []
    for target in active_targets:
        node_id = f"record-{target}"
        record_ids.append(node_id)
        nodes[node_id] = {
            "uses": "operation:record-codegen-fix-apply",
            "with": {"target": target},
            "outputs": [] if omit_record_outputs else _record_outputs(target),
        }
    if include_interrupt:
        nodes["approval-gate"] = {
            "uses": "operation:fixer-proposal-approval",
            "interrupt": {
                "reason": "healing.fixer_approval",
                "checkpoint": "healing.fixer_approval",
                "bind": "audited_gate_read",
                "actions": ["approve_and_apply", "stop"],
            },
        }
    if include_join and record_ids:
        nodes["fixer-join"] = {
            "uses": "operation:combine-fixer-safety",
            "outputs": ["change:healing/fixer-safety-check.json"],
            "join": {
                "sources": record_ids,
                "mode": join_mode,
            },
        }
        # Prefer join node as the combiner; drop the duplicate combine when join present.
        nodes.pop("combine", None)
    return WorkflowSchemaV2.model_validate(
        {
            "name": "healing-target",
            "params": {},
            "entrypoints": {"healing": {"graph": "healing"}},
            "graphs": {
                "healing": {
                    "max_supersteps": 32,
                    "nodes": nodes,
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
            "operation:fixer-proposal-approval": ExecutionContract(
                target="operation:fixer-proposal-approval",
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

    no_interrupt = _approved_schema(include_interrupt=False)
    issues = find_current_healing_conformance_issues(no_interrupt, contracts)
    assert any(issue.code == "missing_approval_interrupt" for issue in issues)

    no_join = _approved_schema(include_join=False)
    issues = find_current_healing_conformance_issues(no_join, contracts)
    assert any(issue.code == "missing_record_join" for issue in issues)

    no_outputs = _approved_schema(omit_record_outputs=True)
    issues = find_current_healing_conformance_issues(no_outputs, contracts)
    assert any(issue.code == "missing_hard_outputs" for issue in issues)

    wrong_join = _approved_schema(join_mode="all")
    issues = find_current_healing_conformance_issues(wrong_join, contracts)
    assert any(issue.code == "inactive_target_required" for issue in issues)

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
    contracts = _approved_contracts()
    cases: tuple[tuple[Literal["api", "e2e"], ...], ...] = (
        ("api",),
        ("e2e",),
        ("api", "e2e"),
    )
    for active in cases:
        schema = _approved_schema(active_targets=active)
        issues = find_current_healing_conformance_issues(schema, contracts, active_targets=active)
        assert issues == (), (active, issues)

    # API-only fixture must not hard-require e2e record outputs.
    api_only = _approved_schema(active_targets=("api",))
    assert find_current_healing_conformance_issues(api_only, contracts, active_targets=("api",)) == ()
    # Inject inactive e2e hard output into allocate — must emit inactive_target_required.
    polluted = _approved_schema(
        active_targets=("api",),
        allocate_outputs=[
            "change:healing/entry-baseline.json",
            "change:healing/fixer-authority.json",
            "change:healing/e2e-apply-summary.json",
        ],
    )
    issues = find_current_healing_conformance_issues(polluted, contracts, active_targets=("api",))
    assert any(issue.code == "inactive_target_required" for issue in issues)
