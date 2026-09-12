from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer
from pydantic import ValidationError

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS as EXECUTION_JOBS
from assurance_execution.graphs.factory import ExecutionGraphs, build_execution_graphs
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_JOBS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_generation.graphs.factory import GenerationGraphs, build_generation_graphs
from assurance_generation.graphs.state import GenerationState
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_JOBS
from assurance_healing.graphs.factory import HealingGraphs, build_healing_graphs
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS as IMPROVEMENT_JOBS
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
from assurance_improvement.graphs.factory import ImprovementGraphs, build_improvement_graphs
from assurance_improvement.graphs.state import ImprovementState
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS as INTAKE_JOBS
from assurance_intake.contracts.attempts import TASK_ATTEMPT_CONTRACTS as INTAKE_TASKS
from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs
from assurance_intake.graphs.state import IntakeState
from assurance_product.graphs.entrypoints import publish_public_output
from assurance_product.graphs.factory import (
    ProductFeatureBundles,
    ThinEntrypointGraphs,
    build_thin_entrypoint_graphs,
    coerce_feature_bundles,
)
from assurance_product.graphs.state import ProductState
from assurance_product.models import (
    FEATURE_WORKFLOW_OWNERS,
    PRODUCT_ENTRYPOINTS,
    THIN_ENTRYPOINTS,
    ProductInputV1,
    ProductPublicOutput,
)
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_JOBS
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS as QUALITY_TASKS
from assurance_quality.graphs.factory import QualityGraphs, build_quality_graphs
from assurance_quality.graphs.state import QualityState
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeState,
)
from graph_engine.testing import GraphHarness

from tests.product.test_product_input import valid_product_input

_SHA = "a" * 64
_CASE_DELTA = "qa/cases/system/dept/case.yaml"
_THIN_EXPORTS = {
    "intake": ("assurance.intake", "prepare"),
    "init": ("assurance.generation", "init_runtime"),
    "case": ("assurance.intake", "case"),
    "archive": ("assurance.improvement", "archive"),
    "retro": ("assurance.improvement", "retro"),
    "issue-review": ("assurance.quality", "issue_review"),
    "issue-analyze": ("assurance.quality", "issue_analyze"),
    "issue-reconcile": ("assurance.quality", "issue_reconcile"),
    "improvement-review": ("assurance.improvement", "review"),
    "improvement-evaluate": ("assurance.improvement", "evaluate"),
    "improvement-export": ("assurance.improvement", "export"),
    "improvement-apply": ("assurance.improvement", "apply"),
    "improvement-rollback": ("assurance.improvement", "rollback"),
}
_CHILD_STATE = {
    "assurance.intake": IntakeState,
    "assurance.generation": GenerationState,
    "assurance.quality": QualityState,
    "assurance.improvement": ImprovementState,
}


def _job_contracts(jobs: Mapping[str, Any]) -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in jobs.values()}


def _contracts_for(owner_id: str) -> dict[str, TaskAttemptContract[Any, Any]]:
    if owner_id == "assurance.intake":
        return {
            **_job_contracts(INTAKE_JOBS),
            **{task.contract_id: task for task in INTAKE_TASKS.values()},
        }
    if owner_id == "assurance.generation":
        return {
            **_job_contracts(GENERATION_JOBS),
            **{task.contract_id: task for task in GENERATION_TASKS.values()},
        }
    if owner_id == "assurance.execution":
        return _job_contracts(EXECUTION_JOBS)
    if owner_id == "assurance.quality":
        return {
            **_job_contracts(QUALITY_JOBS),
            **{task.contract_id: task for task in QUALITY_TASKS.values()},
        }
    if owner_id == "assurance.healing":
        return _job_contracts(HEALING_JOBS)
    contracts = _job_contracts(IMPROVEMENT_JOBS)
    for task in IMPROVEMENT_TASKS.values():
        contracts[task.contract_id] = task
    return contracts


def _real_features() -> dict[str, object]:
    builders = {
        "assurance.intake": build_intake_graphs,
        "assurance.generation": build_generation_graphs,
        "assurance.execution": build_execution_graphs,
        "assurance.quality": build_quality_graphs,
        "assurance.healing": build_healing_graphs,
        "assurance.improvement": build_improvement_graphs,
    }
    features: dict[str, object] = {}
    for owner_id, builder in builders.items():
        context = GraphHarness().recording_context(
            owner_id=owner_id,
            contracts=_contracts_for(owner_id),
        )
        features[owner_id] = builder(context)
    return features


def _feature_success_status(state_schema: type) -> str:
    if state_schema is IntakeState:
        return "passed"
    if state_schema is ImprovementState:
        return "done"
    return "completed"


def _stub_export(state_schema: type, marker: str, *, status: str | None = None) -> CompiledStateGraph:
    builder = StateGraph(state_schema)
    terminal = status or _feature_success_status(state_schema)

    def echo(state: object) -> dict[str, object]:
        del state
        update: dict[str, object] = {"status": terminal}
        if state_schema is IntakeState:
            update["artifacts"] = [{"path": marker, "digest": _SHA}]
        elif state_schema is QualityState:
            update["evidence_refs"] = [{"path": marker, "digest": _SHA}]
            if marker == "quality.fact_baseline":
                update["fact_baseline_ref"] = {
                    "path": "qa/results/facts/fact-baseline.json",
                    "digest": _SHA,
                }
        elif state_schema is ImprovementState:
            update["receipt_refs"] = [{"receipt_id": marker, "receipt_digest": _SHA}]
        else:
            update["receipts"] = [{"receipt_id": marker, "receipt_digest": _SHA}]
        return update

    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=None)


def _stub_features() -> dict[str, object]:
    return {
        "assurance.intake": IntakeGraphs(
            prepare=_stub_export(IntakeState, "intake.prepare"),
            load_plan=_stub_export(IntakeState, "intake.load-plan"),
            case=_stub_export(IntakeState, "intake.case"),
        ),
        "assurance.generation": GenerationGraphs(
            generation=_stub_export(dict, "generation.generation"),
            api=_stub_export(dict, "generation.api"),
            e2e=_stub_export(dict, "generation.e2e"),
            fuzz=_stub_export(dict, "generation.fuzz"),
            performance=_stub_export(dict, "generation.performance"),
            init_runtime=_stub_export(dict, "generation.init_runtime"),
            resolve_inputs=_stub_export(dict, "generation.resolve_inputs"),
        ),
        "assurance.execution": ExecutionGraphs(
            execute=_stub_export(dict, "execution.execute"),
            rerun=_stub_export(dict, "execution.rerun"),
        ),
        "assurance.quality": QualityGraphs(
            assess=_stub_export(QualityState, "quality.assess"),
            issue_review=_stub_export(QualityState, "quality.issue_review"),
            issue_analyze=_stub_export(QualityState, "quality.issue_analyze"),
            issue_reconcile=_stub_export(QualityState, "quality.issue_reconcile"),
            report=_stub_export(QualityState, "quality.report"),
            fact_baseline=_stub_export(QualityState, "quality.fact_baseline"),
        ),
        "assurance.healing": HealingGraphs(
            repair_failure=_stub_export(dict, "healing.repair_failure"),
            repair_coverage=_stub_export(dict, "healing.repair_coverage"),
        ),
        "assurance.improvement": ImprovementGraphs(
            archive=_stub_export(ImprovementState, "improvement.archive"),
            retro=_stub_export(ImprovementState, "improvement.retro"),
            review=_stub_export(ImprovementState, "improvement.review"),
            evaluate=_stub_export(ImprovementState, "improvement.evaluate"),
            export=_stub_export(ImprovementState, "improvement.export"),
            apply=_stub_export(ImprovementState, "improvement.apply"),
            rollback=_stub_export(ImprovementState, "improvement.rollback"),
        ),
    }


def _build_context(checkpointer: Checkpointer = None) -> EngineGraphBuildContext:
    return EngineGraphBuildContext(
        contracts={},
        checkpointer=checkpointer,
        approved_source_roots=(),
    )


def _public_input(entrypoint: str) -> dict[str, object]:
    case_delta = (_CASE_DELTA,) if entrypoint in {"intake", "case"} else ()
    candidate = ("api",) if entrypoint == "intake" else ()
    resolved_plan_ref = (
        {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        }
        if entrypoint == "case"
        else None
    )
    return ProductInputV1.model_validate(
        valid_product_input(
            case_delta_paths=case_delta,
            candidate_test_families=candidate,
            resolved_plan_ref=resolved_plan_ref,
        )
    ).model_dump(mode="json")


class _DuplicateOwnerMapping(Mapping[str, object]):
    def __init__(self, items: tuple[tuple[str, object], ...]) -> None:
        self._items = items

    def __getitem__(self, key: str) -> object:
        for owner, bundle in self._items:
            if owner == key:
                return bundle
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (owner for owner, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)


@pytest.fixture(scope="module")
def real_features() -> dict[str, object]:
    return _real_features()


@pytest.fixture(scope="module")
def thin_graphs(real_features: dict[str, object]) -> ThinEntrypointGraphs:
    return build_thin_entrypoint_graphs(context=_build_context(), features=real_features)


def test_factory_accepts_exactly_six_owner_ids(
    real_features: dict[str, object], thin_graphs: ThinEntrypointGraphs
) -> None:
    assert tuple(sorted(real_features)) == tuple(sorted(FEATURE_WORKFLOW_OWNERS))
    typed = coerce_feature_bundles(real_features)
    assert isinstance(typed, ProductFeatureBundles)
    assert {field.name for field in fields(ProductFeatureBundles)} == {
        "intake",
        "generation",
        "execution",
        "quality",
        "healing",
        "improvement",
    }
    assert set(thin_graphs.entrypoints) == set(THIN_ENTRYPOINTS)
    assert len(thin_graphs.entrypoints) == 13


def test_factory_rejects_missing_extra_duplicate_and_mistyped_bundles(
    real_features: dict[str, object],
) -> None:
    missing = {owner: bundle for owner, bundle in real_features.items() if owner != "assurance.intake"}
    with pytest.raises((TypeError, ValueError), match="missing|owner"):
        coerce_feature_bundles(missing)

    extra = dict(real_features)
    extra["assurance.rogue"] = real_features["assurance.intake"]
    with pytest.raises((TypeError, ValueError), match="extra|owner"):
        coerce_feature_bundles(extra)

    duplicate = _DuplicateOwnerMapping(
        tuple(real_features.items()) + (("assurance.intake", real_features["assurance.intake"]),)
    )
    with pytest.raises((TypeError, ValueError), match="duplicate"):
        coerce_feature_bundles(duplicate)

    mistyped = dict(real_features)
    mistyped["assurance.intake"] = real_features["assurance.quality"]
    with pytest.raises((TypeError, ValueError), match="typed|type|IntakeGraphs"):
        coerce_feature_bundles(mistyped)


def test_boot_factory_signature_is_generic() -> None:
    signature = inspect.signature(build_thin_entrypoint_graphs)
    assert tuple(signature.parameters) == ("context", "features")
    for name, parameter in signature.parameters.items():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert name not in {"intake", "case", "quality", "improvement"}
    source = inspect.getsource(build_thin_entrypoint_graphs)
    assert "intake=" not in source


def test_thin_roots_are_independently_compiled_not_a_dispatcher(
    thin_graphs: ThinEntrypointGraphs,
) -> None:
    assert set(thin_graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS) - {"full", "execute"}
    assert "full" not in thin_graphs.entrypoints
    assert "execute" not in thin_graphs.entrypoints
    graphs_root = (
        Path(__file__).resolve().parents[2] / "packages/products/assurance-product/assurance_product/graphs"
    )
    for path in graphs_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and ast.unparse(node).endswith("['entrypoint']"):
                raise AssertionError(f"{path.name} inspects an entrypoint value inside state")
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                if (
                    func.endswith(".get")
                    and node.args
                    and ast.unparse(node.args[0]) in {"'entrypoint'", '"entrypoint"'}
                ):
                    raise AssertionError(f"{path.name} inspects an entrypoint value inside state")
    names = {id(graph) for graph in thin_graphs.entrypoints.values()}
    assert len(names) == 13


_REPRESENTATIVE_THIN_ENTRYPOINTS = ("intake", "archive", "issue-review")


@pytest.mark.parametrize("entrypoint", _REPRESENTATIVE_THIN_ENTRYPOINTS)
def test_each_thin_root_validates_invokes_declared_export_and_publishes(
    entrypoint: str,
) -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    root = graphs.entrypoints[entrypoint]
    owner_id, export = _THIN_EXPORTS[entrypoint]
    marker = f"{owner_id.split('.')[-1]}.{export}"
    result = root.invoke(_public_input(entrypoint))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.change_id == "CH-DEMO-001"
    assert output.status == "completed"
    assert CHECKPOINT_MARKERS_STATE_KEY not in output.model_dump(mode="json")
    if owner_id == "assurance.improvement":
        assert tuple(item.receipt_id for item in output.receipts) == (marker,)
    else:
        assert output.receipts == ()


def test_thin_root_rejects_non_public_input() -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    with pytest.raises((ValueError, ValidationError, TypeError)):
        graphs.entrypoints["intake"].invoke({"change_id": "CH-DEMO-001"})


def test_product_state_inherits_checkpoint_bridge_and_public_io_omits_markers() -> None:
    hints = get_type_hints(ProductState, include_extras=True)
    assert issubclass(ProductState, dict)
    assert CHECKPOINT_MARKERS_STATE_KEY in hints
    assert CheckpointBridgeState.__annotations__
    assert CHECKPOINT_MARKERS_STATE_KEY not in ProductPublicOutput.model_fields
    assert CHECKPOINT_MARKERS_STATE_KEY not in ProductInputV1.model_fields


def test_thirteen_thin_roots_compile_dry_and_runtime_with_matching_projections(
    real_features: dict[str, object],
) -> None:
    saver = InMemorySaver()
    dry = build_thin_entrypoint_graphs(context=_build_context(None), features=real_features)
    runtime = build_thin_entrypoint_graphs(context=_build_context(saver), features=real_features)
    assert set(dry.entrypoints) == set(runtime.entrypoints) == set(THIN_ENTRYPOINTS)
    for name in THIN_ENTRYPOINTS:
        dry_graph = dry.entrypoints[name]
        runtime_graph = runtime.entrypoints[name]
        assert set(dry_graph.nodes) == set(runtime_graph.nodes)
        assert dry_graph.checkpointer is None
        assert runtime_graph.checkpointer is saver


def test_thin_root_uses_schema_different_child_state() -> None:
    for _entrypoint, (owner_id, _export) in _THIN_EXPORTS.items():
        assert _CHILD_STATE[owner_id] is not ProductState


@pytest.mark.parametrize(
    ("feature_status", "expected"),
    [
        ("passed", "completed"),
        ("rejected", "failed"),
        ("exhausted", "failed"),
        ("done", "completed"),
        ("rework", "failed"),
        ("superseded", "failed"),
        ("completed", "completed"),
        ("failed", "failed"),
    ],
)
def test_publish_adapts_feature_terminals_to_product_status(feature_status: str, expected: str) -> None:
    result = publish_public_output({"change_id": "CH-DEMO-001", "status": feature_status})
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == expected
    assert result["status"] == expected


@pytest.mark.parametrize(
    ("entrypoint", "state_schema", "feature_status", "expected"),
    [
        ("intake", IntakeState, "passed", "completed"),
        ("intake", IntakeState, "rejected", "failed"),
        ("intake", IntakeState, "exhausted", "failed"),
        ("archive", ImprovementState, "done", "completed"),
        ("archive", ImprovementState, "rejected", "failed"),
        ("archive", ImprovementState, "rework", "failed"),
        ("archive", ImprovementState, "superseded", "failed"),
    ],
)
def test_thin_root_publishes_real_feature_terminals(
    entrypoint: str,
    state_schema: type,
    feature_status: str,
    expected: str,
) -> None:
    owner_id, export = _THIN_EXPORTS[entrypoint]
    features = _stub_features()
    marker = f"{owner_id.split('.')[-1]}.{export}"
    child = _stub_export(state_schema, marker, status=feature_status)
    if owner_id == "assurance.intake":
        features[owner_id] = IntakeGraphs(prepare=child, load_plan=child, case=child)
    else:
        features[owner_id] = ImprovementGraphs(
            archive=child,
            retro=child,
            review=child,
            evaluate=child,
            export=child,
            apply=child,
            rollback=child,
        )
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    result = graphs.entrypoints[entrypoint].invoke(_public_input(entrypoint))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == expected


def test_publish_keeps_only_real_receipt_refs() -> None:
    result = publish_public_output(
        {
            "change_id": "CH-DEMO-001",
            "status": "done",
            "artifacts": [{"path": "input.artifact", "digest": _SHA}],
            "evidence_refs": [{"path": "quality.evidence", "digest": _SHA}],
            "receipt_refs": [{"receipt_id": "real.receipt", "receipt_digest": _SHA}],
        }
    )
    output = ProductPublicOutput.model_validate(result["output"])
    assert tuple(item.receipt_id for item in output.receipts) == ("real.receipt",)


def test_publish_does_not_harvest_artifacts_or_evidence_as_receipts() -> None:
    result = publish_public_output(
        {
            "change_id": "CH-DEMO-001",
            "status": "passed",
            "artifacts": [
                {"path": "input.artifact", "digest": _SHA},
                {"path": "intake.prepare", "digest": _SHA},
            ],
            "evidence_refs": [{"path": "quality.issue_review", "digest": _SHA}],
        }
    )
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.receipts == ()


def test_thin_root_does_not_publish_input_or_feature_artifacts_as_receipts() -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    payload = _public_input("intake")
    payload["artifacts"] = [{"path": "input.case", "digest": _SHA}]
    result = graphs.entrypoints["intake"].invoke(payload)
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == "completed"
    assert output.receipts == ()
    quality = graphs.entrypoints["issue-review"].invoke(_public_input("issue-review"))
    assert ProductPublicOutput.model_validate(quality["output"]).receipts == ()
