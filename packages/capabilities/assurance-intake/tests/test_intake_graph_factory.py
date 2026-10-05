from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from agent_runtime_contracts import (
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
    canonical_digest,
)
from agent_runtime_contracts.wire.models import AgentRunResult
from agent_runtime_contracts.ops import ArtifactListResultV1
from assurance_intake.contracts.agent import ArtifactDigestV1
from assurance_intake.ops.case_design import CaseDesignInputV1, CaseDesignOutputV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs as _build_intake_graphs
from graph_engine.attempts.context import AttemptExecutionContext, AuthorizedAttemptScope
from graph_engine.attempts.contracts import ExecutedAttemptResult, TaskAttemptContract
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef, RejectedTaskResult
from graph_engine.plugin_api import (
    DirectoryIdentity,
    ResourceClaims,
    TaskWorkspaceBinding,
    TaskWorkspaceIdentity,
)
from graph_engine.testing import GraphHarness, committed
from tests.acg_plan_fixture import install_plan

from graph_engine.testing.feature_bundle import compile_bundle


def build_intake_graphs(*args, **kwargs):
    return compile_bundle(_build_intake_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_INTAKE_ID = "assurance.intake.agent.intake.v1"
_EXPLORE_ID = "assurance.intake.agent.explore.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_REPAIR_ID = "assurance.intake.agent.case-repair.v1"
_CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_RESOLVE_PLAN_ID = "assurance.intake.task.resolve-plan"
_COVERAGE_REWORK_ID = "assurance.intake.task.coverage-rework"
_GRAPH_CONTRACT_IDS = (
    _INTAKE_ID,
    _EXPLORE_ID,
    _RESOLVE_PLAN_ID,
    _CASE_DESIGN_ID,
    _CASE_REPAIR_ID,
    _CASE_REVIEW_ID,
    _COVERAGE_REWORK_ID,
)
_PHASE_NODES = frozenset(
    {
        "prepare",
        "execute",
        "finalize",
        "finalize-inputs",
        "repair-prepare",
        "repair-execute",
        "repair-finalize-inputs",
        "repair-finalize",
    }
)
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_intake" / "graphs"


def intake_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def intake_graph_input() -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "candidate_test_families": ["api"],
        "selected_test_families": ["api"],
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "case_delta_paths": ["qa/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "budgets": {
            "review_rounds": 2,
            "coverage_rounds": 2,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
        "family_policy": {"required": [], "allowed": ["api"]},
        "product_policy": {
            "resource_id": "assurance.product.configuration.product-policy",
            "sha256": _SHA,
        },
        "capability_catalog": {
            "resource_id": "assurance.product.configuration.capability-catalog",
            "sha256": _SHA,
        },
        "data_knowledge": {
            "resource_id": "assurance.product.configuration.data-knowledge",
            "sha256": _SHA,
        },
        "rounds_used": 0,
        "rounds_budget": 2,
        "artifact_ledger": {"intake.plan": [plan_ref]},
        "coverage_epoch": 0,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            {
                "path": "qa/results/explore/exploration.json",
                "digest": _SHA,
            },
            {
                "path": "qa/results/explore/impact-inventory.json",
                "digest": _SHA,
            },
            plan_ref,
        ],
    }


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _ledger_entry(path: str) -> dict[str, object]:
    return {
        "refs": [{"path": path, "digest": _SHA}],
        "receipt": {"receipt_id": _RECEIPT_ID, "receipt_digest": _SHA},
    }


def _artifact_output() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/proposal.md",))


def _reviewed_case() -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            plan_ref,
        ],
        "case_refs": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}],
        "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
        "selection_ref": {"path": "qa/results/cases/epochs/0/selection.json", "digest": _SHA},
    }


def _public_outcome(
    decision: str,
    *,
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> str:
    if decision == "pass":
        return "pass"
    if decision == "reject":
        return "reject"
    if human_review_required or decision == "needs_human_review":
        return "needs_human"
    if decision == "needs_fix" and auto_fix_allowed:
        return "needs_fix"
    return "needs_human"


def _review_output(
    *,
    decision: str = "pass",
    auto_fix_allowed: bool = False,
    human_review_required: bool = False,
    rounds_used: int = 0,
    rounds_budget: int = 2,
) -> dict[str, object]:
    del rounds_used, rounds_budget
    return {
        "decision": decision,
        "public_outcome": _public_outcome(
            decision,
            auto_fix_allowed=auto_fix_allowed,
            human_review_required=human_review_required,
        ),
        "auto_fix_allowed": auto_fix_allowed,
        "human_review_required": human_review_required,
        "artifacts": [
            {
                "path": "qa/cases/reviewed-case.json",
                "digest": _SHA,
            },
            {
                "path": "qa/results/review/case-review.json",
                "digest": _SHA,
            },
            {
                "path": "qa/results/cases/epochs/0/selection.json",
                "digest": _SHA,
            },
        ],
        "reviewed_case": _reviewed_case(),
    }


def _design_output() -> dict[str, object]:
    return {
        "output_files": ["qa/proposal.md"],
        "artifacts": [
            {
                "path": "qa/cases/menus/case.yaml",
                "digest": _SHA,
            },
            {"path": "qa/proposal.md", "digest": _SHA},
        ],
    }


def _repair_output() -> dict[str, object]:
    return {"artifacts": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}]}


def _node_names(graph: object) -> set[str]:
    names: set[str] = set()
    nodes = getattr(graph, "nodes", {})
    if not isinstance(nodes, dict):
        return names
    for name, node in nodes.items():
        if name in {"__start__", "__end__"}:
            continue
        names.add(str(name))
        nested = getattr(node, "nodes", None)
        if nested is not None:
            names.update(_node_names(node))
        runnable = getattr(node, "runnable", None)
        if runnable is not None:
            names.update(_node_names(runnable))
    return names


def _top_level_names(graph: object) -> set[str]:
    nodes = getattr(graph, "nodes", {})
    if not isinstance(nodes, dict):
        return set()
    return {str(name) for name in nodes if name not in {"__start__", "__end__"}}


def _is_compiled_subgraph(graph: object, name: str) -> bool:
    nodes = getattr(graph, "nodes", {})
    node = nodes[name]
    return isinstance(getattr(node, "bound", None), CompiledStateGraph)


def _walk_graph_python() -> Iterator[Path]:
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.intake",
        contracts=intake_contracts(),
    )


def test_intake_factory_exports_prepare_and_case(recording_context, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object, object]] = []
    original = recording_context.attempt

    def record_attempt(
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        calls.append((contract_id, semantic_node_id, activation, select))
        return original(
            contract_id,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=select,
            publish=publish,
        )

    monkeypatch.setattr(recording_context, "attempt", record_attempt)
    bundle = build_intake_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == ("prepare", "case", "coverage_rework")
    assert isinstance(bundle, IntakeGraphs)
    assert recording_context.bound_contract_ids == (
        _INTAKE_ID,
        _EXPLORE_ID,
        _RESOLVE_PLAN_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
        _CASE_REPAIR_ID,
        _COVERAGE_REWORK_ID,
    )
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert recording_context.bound_contract_ids.count(_CASE_DESIGN_ID) == 1
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)
    assert calls[3][:2] == (_CASE_DESIGN_ID, "intake.case-design")
    assert calls[4][:2] == (_CASE_REVIEW_ID, "intake.case-review")
    assert calls[5][:2] == (_CASE_REPAIR_ID, "intake.case-repair")


def test_prepare_contains_only_preparation_nodes(recording_context) -> None:
    bundle = build_intake_graphs(recording_context)
    names = _node_names(bundle.prepare)
    assert {"intake", "explore", "resolve-plan", "prepared"} <= names
    assert not {"case-design", "case-review", "human-review"} & names
    for leaf in ("intake", "explore", "resolve-plan"):
        assert not _is_compiled_subgraph(bundle.prepare, leaf), leaf


def test_shared_case_contains_complete_review_flow(recording_context) -> None:
    bundle = build_intake_graphs(recording_context)
    assert _top_level_names(bundle.case) == {
        "__flow_entry__",
        "case-design",
        "case-repair",
        "case-review",
        "human-review",
        "reviewed",
        "rejected",
        "exhausted",
        "failed",
    }
    assert not _is_compiled_subgraph(bundle.case, "case-design")
    assert not _is_compiled_subgraph(bundle.case, "case-review")
    assert not _is_compiled_subgraph(bundle.case, "case-repair")


def test_target_graphs_contain_no_phase_nodes_or_send(recording_context) -> None:
    bundle = build_intake_graphs(recording_context)
    names = _node_names(bundle.prepare) | _node_names(bundle.case)
    assert names.isdisjoint(_PHASE_NODES)
    send_hits: list[str] = []
    fanout_hits: list[str] = []
    for path in _walk_graph_python():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Send":
                send_hits.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in {"fanout", "min_matches"}:
                fanout_hits.append(f"{path}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"Send", "fanout"}:
                send_hits.append(f"{path}:{node.lineno}:{node.attr}")
    assert send_hits == []
    assert fanout_hits == []


async def test_case_graph_runs_design_then_review() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-review": [committed(_review_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
    ]
    primary = result.select_values[0]
    assert isinstance(primary, dict)
    assert "validation_attempt" not in primary
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "reviewed"
    assert terminal["artifact_ledger"] == {
        "intake.plan": [
            {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            }
        ],
        "intake.case": _ledger_entry("qa/cases/menus/case.yaml"),
        "intake.proposal": _ledger_entry("qa/proposal.md"),
        "intake.review": _ledger_entry("qa/results/review/case-review.json"),
        "intake.reviewed_case": _ledger_entry("qa/cases/reviewed-case.json"),
    }
    assert "requirement" not in terminal
    assert "case_receipt" not in terminal
    assert "receipt" not in terminal
    assert terminal["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in terminal


async def test_case_rejection_is_an_explicit_unsuccessful_terminal() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-review": [committed(_review_output(decision="reject"), receipt)],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "rejected"
    assert "decision" not in terminal
    assert terminal["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in terminal
    assert "case_receipt" not in terminal


async def test_case_auto_fix_runs_repair_and_review_again() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-repair": [committed(_repair_output(), receipt)],
            "intake.case-review": [
                committed(
                    _review_output(decision="needs_fix", auto_fix_allowed=True),
                    receipt,
                ),
                committed(_review_output(rounds_used=1), receipt),
            ],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
        "intake.case-repair",
        "intake.case-review",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
        _CASE_REPAIR_ID,
        _CASE_REVIEW_ID,
    ]
    repair = result.select_values[2]
    assert isinstance(repair, dict)
    assert repair.get("review_repair") is None
    assert "validation_attempt" not in repair
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "reviewed"
    review_rounds = [
        item["review_round"]
        for item in result.select_values
        if isinstance(item, dict) and "review_round" in item
    ]
    assert review_rounds == [0, 1]


async def test_case_repair_failure_exhausts_the_case_flow() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-repair": [RejectedTaskResult(reason="repair changed fields outside allowed_paths")],
            "intake.case-review": [
                committed(_review_output(decision="needs_fix", auto_fix_allowed=True), receipt),
            ],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
        "intake.case-repair",
    ]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "exhausted"


async def test_case_budget_exhaustion_is_explicit() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    graph_input = intake_graph_input()
    budgets = graph_input["budgets"]
    assert isinstance(budgets, dict)
    graph_input["budgets"] = {**budgets, "review_rounds": 0}
    result = await harness.run(
        bundle.case,
        input=graph_input,
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-review": [
                committed(
                    _review_output(
                        decision="needs_fix",
                        auto_fix_allowed=True,
                        rounds_used=2,
                        rounds_budget=2,
                    ),
                    receipt,
                )
            ],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "exhausted"
    assert "rounds_used" not in terminal
    assert terminal["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in terminal
    assert "case_receipt" not in terminal


async def test_case_design_failure_is_failed_not_exhausted() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={"intake.case-design": [RejectedTaskResult(reason="case design failed")]},
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"
    assert terminal.get("reviewed_case") is None
    assert "case_receipt" not in terminal


async def test_case_starts_from_the_input_plan_ref_when_the_ledger_is_empty() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    payload = intake_graph_input()
    payload["artifact_ledger"] = {}
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=payload,
        script={
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-review": [committed(_review_output(), receipt)],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "reviewed"
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
    ]


def _review_at(epoch: int) -> dict[str, object]:
    output = _review_output()
    reviewed = dict(output["reviewed_case"]) if isinstance(output["reviewed_case"], dict) else {}
    reviewed["coverage_epoch"] = epoch
    selection = dict(reviewed["selection_ref"]) if isinstance(reviewed.get("selection_ref"), dict) else {}
    selection["path"] = f"qa/results/cases/epochs/{epoch}/selection.json"
    reviewed["selection_ref"] = selection
    return {**output, "reviewed_case": reviewed}


async def test_coverage_epoch_changes_the_case_activation_key() -> None:
    async def once(epoch: int) -> tuple[str, int]:
        harness = GraphHarness()
        context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
        bundle = build_intake_graphs(context)
        receipt = _receipt()
        payload = intake_graph_input()
        payload["coverage_epoch"] = epoch
        payload["rounds_used"] = 4
        result = await harness.run(
            bundle.case,
            input=payload,
            script={
                "intake.case-design": [committed(_design_output(), receipt)],
                "intake.case-review": [committed(_review_at(epoch), receipt)],
            },
        )
        terminal = result.terminal
        assert isinstance(terminal, dict)
        assert terminal["status"] == "reviewed"
        design = next(call for call in result.semantic_calls if call.semantic_node_id == "intake.case-design")
        review = next(
            item["review_round"]
            for item in result.select_values
            if isinstance(item, dict) and "review_round" in item
        )
        assert isinstance(review, int)
        return design.input_digest, review

    first_digest, first_round = await once(0)
    second_digest, second_round = await once(1)
    assert first_digest != second_digest
    assert first_round == second_round == 0


class _RecordingPrepare:
    def __init__(self, prepared: dict[str, object]) -> None:
        self.prepared = prepared
        self.seen_input: CaseDesignInputV1 | None = None

    async def execute(self, validated_input: CaseDesignInputV1, scope: object) -> dict[str, object]:
        del scope
        self.seen_input = validated_input
        return self.prepared


class _RecordingRuntime:
    def __init__(self, result: ArtifactListResultV1) -> None:
        self.result = result
        self.seen_prepared: dict[str, object] | None = None

    async def execute(self, prepared: dict[str, object], scope: object) -> RawAgentRuntimeOutcome:
        del scope
        self.seen_prepared = prepared
        payload = self.result.model_dump(mode="json")
        return RawAgentRuntimeOutcome(
            run_result=AgentRunResult.model_validate(
                {
                    "result_payload": payload,
                    "result_digest": canonical_digest(payload),
                    "evidence_digest": _SHA,
                    "adapter_id": "agent-runtime-fixture",
                    "adapter_version": "1.0.0",
                }
            ),
            raw_workspace=ReadOnlyRawWorkspace(Path(".")),
        )


class _RecordingFinalize:
    def __init__(self, output: CaseDesignOutputV1) -> None:
        self.output = output
        self.seen: RawFinalizeBundle[CaseDesignInputV1, dict[str, object], ArtifactListResultV1] | None = None

    async def execute(
        self,
        bundle: RawFinalizeBundle[CaseDesignInputV1, dict[str, object], ArtifactListResultV1],
        scope: object,
    ) -> CaseDesignOutputV1:
        del scope
        self.seen = bundle
        return self.output


def _attempt_scope(semantic_node_id: str, tmp_path: Path) -> AuthorizedAttemptScope:
    project = tmp_path / "project"
    write = tmp_path / "write"
    project.mkdir()
    write.mkdir()
    identity = TaskWorkspaceIdentity.model_construct(
        task_id="task",
        attempt=1,
        attempt_id="attempt-1",
        output_paths=(),
        baseline_files=(),
        project_digest=_SHA,
        write_root_digest=_SHA,
        identity_digest=_SHA,
        layout_schema_version="1",
    )
    directory = DirectoryIdentity.model_construct(
        path_digest=_SHA,
        device=1,
        inode=1,
        identity_digest=_SHA,
    )
    return AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv-1",
            public_entrypoint="case",
            semantic_node_id=semantic_node_id,
            attempt_key=AttemptKey(digest=_SHA),
            fencing_token=1,
        ),
        workspace=TaskWorkspaceBinding(
            identity=identity,
            project_root=project,
            write_root=write,
            project_root_identity=directory,
            write_root_identity=directory,
        ),
    )


def _case_design_input() -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            "change_id": "CH-DEMO-001",
            "plan_digest": _SHA,
            "plan_ref": {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
            "capability_leafs": ["entities.item.create"],
            "artifact_paths": [
                "qa/.qa.yaml",
                "qa/cases",
                "qa/fixtures",
                "qa/proposal.md",
                "qa/requirement.md",
                "qa/results",
                "qa/tests",
            ],
            "selected_test_families": ["api"],
            "case_delta_paths": ["qa/cases/menus/case.yaml"],
        }
    )


async def test_prepared_value_and_agent_result_reach_finalize_through_one_composite_attempt(
    tmp_path: Path,
) -> None:
    semantic_node_id = "intake.case-design"
    prepared: dict[str, object] = {"prompt": "design cases", "path": semantic_node_id}
    agent_result = ArtifactListResultV1(output_files=("qa/proposal.md",))
    finalized = CaseDesignOutputV1(
        artifacts=(ArtifactDigestV1(path="qa/proposal.md", digest=_SHA),),
    )
    prepare = _RecordingPrepare(prepared)
    runtime = _RecordingRuntime(agent_result)
    finalize = _RecordingFinalize(finalized)
    contract = AGENT_JOB_CONTRACTS["case-design"]
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )
    writable = ResourceClaims()
    del writable
    output = await executor.execute(
        _case_design_input(),
        _attempt_scope(semantic_node_id, tmp_path),
    )
    assert prepare.seen_input is not None
    assert prepare.seen_input.validation_error is None
    assert runtime.seen_prepared == prepared
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared
    assert finalize.seen.agent_result == agent_result
    assert isinstance(output, ExecutedAttemptResult)
    assert output.output == finalized
    assert isinstance(output.output, BaseModel)


def test_preparation_refs_replace_a_stale_exploration_digest(tmp_path: Path) -> None:
    from assurance_intake.contracts.plan import ResolvePlanInputV1
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
    from assurance_intake.ops.resolve_plan.hooks.artifacts import _preparation_refs

    plan, plan_ref = install_plan(
        tmp_path,
        "CH-DEMO-001",
        capability_leafs=("entities.item.create",),
    )
    bound = EvidenceArtifactRefV1.model_validate(plan.exploration_ref.model_dump(mode="json"))
    stale = EvidenceArtifactRefV1(path=bound.path, digest="b" * 64)
    inventory = EvidenceArtifactRefV1.model_validate(plan.impact_inventory_ref.model_dump(mode="json"))
    plan_ref_model = EvidenceArtifactRefV1.model_validate(plan_ref)
    request = ResolvePlanInputV1.model_construct(
        artifacts=(stale, inventory, stale),
        impact_inventory_ref=inventory,
        exploration_ref=stale,
    )
    preparation = _preparation_refs(
        request,
        project_root=tmp_path,
        exploration_ref=bound,
        plan_ref=plan_ref_model,
    )
    assert bound in preparation
    assert stale not in preparation
    assert plan_ref_model in preparation


async def test_prepare_graph_runs_intake_explore_and_plan_resolution(tmp_path: Path) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    plan, plan_ref = install_plan(
        tmp_path,
        "CH-DEMO-001",
        capability_leafs=("entities.item.create",),
    )
    prepare_input = intake_graph_input()
    prepare_input.pop("plan_digest")
    prepare_input.pop("plan_ref")
    prepare_input.pop("selected_test_families")
    prepare_input["artifact_ledger"] = {
        "intake.exploration": [plan.exploration_ref.model_dump(mode="json")],
        "intake.inventory": [plan.impact_inventory_ref.model_dump(mode="json")],
    }
    result = await harness.run(
        bundle.prepare,
        input=prepare_input,
        script={
            "intake.intake": [committed(_artifact_output(), receipt)],
            "intake.explore": [committed(_artifact_output(), receipt)],
            "intake.resolve-plan": [
                committed(
                    {
                        "plan": plan.model_dump(mode="json"),
                        "plan_ref": plan_ref,
                        "preparation_refs": [plan_ref],
                    },
                    receipt,
                )
            ],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.intake",
        "intake.explore",
        "intake.resolve-plan",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _INTAKE_ID,
        _EXPLORE_ID,
        _RESOLVE_PLAN_ID,
    ]
    assert result.promotion_decision == "committed"
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal.get("status") == "prepared"
    assert terminal.get("flow_outcome") == "prepared"
    assert "plan_digest" in terminal
    assert "preparation_refs" in terminal
    assert "intake.exploration" in terminal["artifact_ledger"]
    assert "requirement" not in terminal
