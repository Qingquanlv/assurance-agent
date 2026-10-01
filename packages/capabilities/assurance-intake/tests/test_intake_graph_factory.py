from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any, Literal

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
from assurance_intake.domain.artifacts import ArtifactDigestV1, ArtifactListResultV1
from assurance_intake.ops.case_design import CaseDesignInputV1, CaseDesignOutputV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs
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

_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_INTAKE_ID = "assurance.intake.agent.intake.v1"
_EXPLORE_ID = "assurance.intake.agent.explore.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_REPAIR_ID = "assurance.intake.agent.case-repair.v1"
_CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_RESOLVE_PLAN_ID = "assurance.intake.task.resolve-plan"
_GRAPH_CONTRACT_IDS = (
    _INTAKE_ID,
    _EXPLORE_ID,
    _RESOLVE_PLAN_ID,
    _CASE_DESIGN_ID,
    _CASE_REPAIR_ID,
    _CASE_REVIEW_ID,
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


def _artifact_output() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/proposal.md",))


def _review_output(
    *,
    decision: str = "pass",
    auto_fix_allowed: bool = False,
    human_review_required: bool = False,
    rounds_used: int = 0,
    rounds_budget: int = 2,
) -> dict[str, object]:
    return {
        "decision": decision,
        "auto_fix_allowed": auto_fix_allowed,
        "human_review_required": human_review_required,
        "artifacts": [
            {
                "path": "qa/results/review/case-review.json",
                "digest": _SHA,
            },
            {
                "path": "qa/results/cases/epochs/0/selection.json",
                "digest": _SHA,
            },
        ],
        "rounds_used": rounds_used,
        "rounds_budget": rounds_budget,
    }


def _design_output(*, validation_status: str = "pass") -> dict[str, object]:
    return {
        "output_files": ["qa/proposal.md"],
        "validation_status": validation_status,
        "validation_attempt": 0 if validation_status == "pass" else 1,
        "validation_error": None if validation_status == "pass" else "authored cases failed validation",
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
    from assurance_intake.graphs.calls import (
        activation_case_design,
        activation_case_design_repair,
        activation_case_repair,
        select_case_design,
        select_case_design_repair,
        select_case_repair,
    )

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
    assert tuple(item.name for item in fields(bundle)) == ("prepare", "case")
    assert isinstance(bundle, IntakeGraphs)
    assert recording_context.bound_contract_ids == (
        _INTAKE_ID,
        _EXPLORE_ID,
        _RESOLVE_PLAN_ID,
        _CASE_DESIGN_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
        _CASE_REPAIR_ID,
    )
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert recording_context.bound_contract_ids.count(_CASE_DESIGN_ID) == 2
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)
    assert calls[3][:2] == (_CASE_DESIGN_ID, "intake.case-design")
    assert tuple((fn.__module__, fn.__qualname__) for fn in calls[3][2:]) == tuple(
        (fn.__module__, fn.__qualname__) for fn in (activation_case_design, select_case_design)
    )
    assert calls[4][:2] == (_CASE_DESIGN_ID, "intake.case-design-repair")
    assert tuple((fn.__module__, fn.__qualname__) for fn in calls[4][2:]) == tuple(
        (fn.__module__, fn.__qualname__) for fn in (activation_case_design_repair, select_case_design_repair)
    )
    assert calls[6][:2] == (_CASE_REPAIR_ID, "intake.case-repair")
    assert tuple((fn.__module__, fn.__qualname__) for fn in calls[6][2:]) == tuple(
        (fn.__module__, fn.__qualname__) for fn in (activation_case_repair, select_case_repair)
    )


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
        "case-design",
        "case-repair",
        "case-review",
        "review-round-advance",
        "advance-join",
        "human-review",
        "done",
        "rejected",
        "exhausted",
    }
    assert _is_compiled_subgraph(bundle.case, "case-design")
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


async def test_case_graph_runs_primary_and_repair_through_one_composite_attempt_each() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(validation_status="needs_fix"), receipt)],
            "intake.case-design-repair": [committed(_design_output(validation_status="pass"), receipt)],
            "intake.case-review": [committed(_review_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-design-repair",
        "intake.case-review",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _CASE_DESIGN_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
    ]
    primary, repair, _ = result.select_values
    assert isinstance(primary, dict)
    assert isinstance(repair, dict)
    assert primary["validation_attempt"] == 0
    assert repair["validation_attempt"] == 1
    assert repair["validation_error"] == "authored cases failed validation"
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "reviewed"
    reviewed = terminal["reviewed_case"]
    assert isinstance(reviewed, dict)
    assert reviewed["coverage_epoch"] == 0
    assert reviewed["case_refs"] == [
        {
            "path": "qa/cases/menus/case.yaml",
            "digest": _SHA,
        }
    ]
    assert terminal["receipt"] == {
        "receipt_id": _RECEIPT_ID,
        "receipt_digest": _SHA,
    }


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
    assert terminal["decision"] == "reject"


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
    assert repair["review_repair"] is None
    assert "validation_attempt" not in repair
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "reviewed"
    assert terminal["rounds_used"] == 1


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
    graph_input = {**intake_graph_input(), "rounds_used": 2, "rounds_budget": 2}
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
    assert terminal["rounds_used"] == 2


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


def _case_design_input(*, validation_attempt: int = 0) -> CaseDesignInputV1:
    payload: dict[str, object] = {
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
        "validation_attempt": validation_attempt,
    }
    if validation_attempt == 1:
        payload["validation_error"] = "authored cases failed validation"
    return CaseDesignInputV1.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "semantic_node_id", "validation_attempt"),
    [
        ("primary", "intake.case-design", 0),
        ("repair", "intake.case-design-repair", 1),
    ],
)
async def test_prepared_value_and_agent_result_reach_finalize_through_one_composite_attempt(
    path: str,
    semantic_node_id: str,
    validation_attempt: Literal[0, 1],
    tmp_path: Path,
) -> None:
    del path
    prepared: dict[str, object] = {"prompt": "design cases", "path": semantic_node_id}
    agent_result = ArtifactListResultV1(output_files=("qa/proposal.md",))
    finalized = CaseDesignOutputV1(
        validation_status="pass",
        validation_attempt=validation_attempt,
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
        _case_design_input(validation_attempt=validation_attempt),
        _attempt_scope(semantic_node_id, tmp_path),
    )
    assert prepare.seen_input is not None
    assert prepare.seen_input.validation_attempt == validation_attempt
    assert runtime.seen_prepared == prepared
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared
    assert finalize.seen.agent_result == agent_result
    assert isinstance(output, ExecutedAttemptResult)
    assert output.output == finalized
    assert isinstance(output.output, BaseModel)


def test_publish_plan_rebinds_exploration_to_the_plan_digest(tmp_path: Path) -> None:
    from assurance_intake.graphs.calls import publish_plan

    plan, plan_ref = install_plan(
        tmp_path,
        "CH-DEMO-001",
        capability_leafs=("entities.item.create",),
    )
    bound = plan.exploration_ref.model_dump(mode="json")
    stale = {"path": bound["path"], "digest": "b" * 64}
    inventory = plan.impact_inventory_ref.model_dump(mode="json")
    published = publish_plan(
        {"artifacts": [stale, inventory, stale], "preparation_refs": [stale]},
        {"plan": plan.model_dump(mode="json"), "plan_ref": plan_ref},
        None,
    )
    assert published["artifacts"] == [bound, inventory]
    preparation = published["preparation_refs"]
    assert isinstance(preparation, list)
    assert bound in preparation
    assert stale not in preparation


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
    result = await harness.run(
        bundle.prepare,
        input=prepare_input,
        script={
            "intake.intake": [committed(_artifact_output(), receipt)],
            "intake.explore": [committed(_artifact_output(), receipt)],
            "intake.resolve-plan": [
                committed(
                    {"plan": plan.model_dump(mode="json"), "plan_ref": plan_ref},
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
