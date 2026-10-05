from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Iterator, Mapping
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Literal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Checkpointer
from pydantic import ValidationError, create_model

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS as EXECUTION_JOBS
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS as EXECUTION_TASKS
from assurance_execution.graphs.factory import ExecutionGraphs, build_execution_graphs
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_JOBS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_generation.graphs.factory import GenerationGraphs, build_generation_graphs
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_JOBS
from assurance_healing.graphs.factory import HealingGraphs, build_healing_graphs
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS as IMPROVEMENT_JOBS
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
from assurance_improvement.graphs.factory import ImprovementGraphs, build_improvement_graphs
from assurance_intake.feature import AGENT_JOB_CONTRACTS as INTAKE_JOBS
from assurance_intake.feature import TASK_ATTEMPT_CONTRACTS as INTAKE_TASKS
from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs
from assurance_product.graphs.factory import (
    ProductFeatureBundles,
    ThinEntrypointGraphs,
    build_product_graphs,
    build_thin_entrypoint_graphs,
    coerce_feature_bundles,
)
from assurance_product.graphs.entrypoints import thin_root_flows
from assurance_product.feature_set import CAPABILITY_OWNERS
from assurance_product.models import (
    PRODUCT_ENTRYPOINTS,
    THIN_ENTRYPOINTS,
    ProductInputV1,
    ProductPublicOutput,
)
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_JOBS
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS as QUALITY_TASKS
from assurance_quality.graphs.factory import QualityGraphs, build_quality_graphs
from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.contracts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel, ResourceClaims
from graph_engine.stategraph.ledger import NamedWrite
from graph_engine.stategraph.checkpoint_bridge import CHECKPOINT_MARKERS_STATE_KEY
from graph_engine.testing import GraphHarness

from tests.product.test_product_input import valid_product_input

_SHA = "a" * 64
_CASE_DELTA = "qa/cases/system/dept/case.yaml"
_THIN_EXPORTS = {
    "intake": ("assurance.intake", "prepare"),
    "init": ("assurance.generation", "init_runtime"),
    "retro": ("assurance.improvement", "retro"),
    "issue-review": ("assurance.quality", "issue_review"),
    "issue-analyze": ("assurance.quality", "issue_analyze"),
    "issue-reconcile": ("assurance.quality", "issue_reconcile"),
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
        return {
            **_job_contracts(EXECUTION_JOBS),
            **{task.contract_id: task for task in EXECUTION_TASKS.values()},
        }
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


class _ToyInput(FrozenModel):
    change_id: str = ""
    source_artifacts: object = None
    coverage_epoch: int = 0
    healing_rounds_used: int = 0
    plan_ref: object = None
    ui_exploration_ref: object = None
    api_discovery_ref: object = None
    rounds_budget: object = None
    rounds_used: int = 0
    artifact_paths: object = None
    source_refs: object = None
    window: object = None


class _CaseToyInput(_ToyInput):
    plan_digest: str = ""


def _toy_output(outcomes: tuple[str, ...]) -> type[FrozenModel]:
    return create_model(
        "ToyOutcome",
        __base__=FrozenModel,
        public_outcome=(Literal[*outcomes], ...),
        plan_digest=(str, "a" * 64),
    )


def _toy_task(outcomes: tuple[str, ...]) -> TaskAttemptContract[Any, Any]:
    return TaskAttemptContract(
        contract_id="assurance.product.test.toy-echo",
        owner_id="assurance.product",
        handler_id="assurance.product.toy-echo",
        input_model=_ToyInput,
        output_model=_toy_output(outcomes),
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


class _PrepareToyOp:
    contract_id = "assurance.product.test.toy-echo"
    input_model = _ToyInput

    def __init__(self, outcomes: tuple[str, ...]) -> None:
        self.output_model = _toy_output(outcomes)

    def ledger_namespace(self) -> str:
        return "intake"

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return (NamedWrite(name="plan", root="qa/results/plan"),)

    def input_bindings(self) -> tuple[object, ...]:
        return ()


class _SucceedContext:
    def __init__(self, terminal: str) -> None:
        self.terminal = terminal

    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: object,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        del contract_id, semantic_node_id, activation, select
        terminal = self.terminal

        async def _ok(state: object, runtime: object = None) -> dict[str, object]:
            del runtime
            raw: dict[str, object] = {"public_outcome": terminal, "plan_digest": "a" * 64}
            if callable(publish):
                payload = state if isinstance(state, Mapping) else {}
                published = publish(
                    payload,
                    raw,
                    None,
                    committed=(ArtifactRef(path="qa/results/plan", digest=_SHA),),
                )
                if isinstance(published, Mapping):
                    return {str(name): value for name, value in published.items()}
            return raw

        return _ok

    def compile_subgraph(self, builder: object) -> object:
        return builder.compile(checkpointer=None)  # type: ignore[union-attr]


def _feature_success_status(marker: str) -> str:
    if marker.startswith("intake."):
        return "passed"
    if marker.startswith("improvement."):
        return "done"
    return "completed"


_STUB_OUTCOMES = {
    "intake.prepare": ("prepared", "failed"),
    "intake.case": ("reviewed", "rejected", "exhausted", "failed"),
    "generation.generation": ("completed", "failed"),
    "generation.init_runtime": ("completed", "failed"),
    "execution.execute": ("completed", "failed"),
    "execution.rerun": ("completed", "failed"),
    "quality.assess": ("completed", "failed"),
    "quality.issue_review": ("fix_eligible", "report_issue", "unclassified", "failed"),
    "quality.issue_analyze": ("fix_eligible", "report_issue", "unclassified", "failed"),
    "quality.issue_reconcile": ("ready", "failed"),
    "quality.report": ("completed", "failed"),
    "quality.fact_baseline": ("completed", "failed"),
    "quality.surface_baseline": ("completed", "failed"),
    "healing.repair_failure": ("completed", "failed"),
    "improvement.archive": ("done", "failed"),
    "improvement.retro": ("done", "failed"),
    "improvement.review": ("done", "failed"),
    "improvement.evaluate": ("done", "failed"),
    "improvement.export": ("done", "failed"),
    "improvement.apply": ("done", "failed", "rejected", "rework", "superseded"),
    "improvement.rollback": ("done", "failed"),
    "improvement.runtime_snapshot": ("done", "failed"),
    "intake.coverage-rework": ("done", "failed"),
    "archive.archive": ("done", "failed"),
    "improvement-apply.apply": ("done", "failed", "rejected", "rework", "superseded"),
    "issue-review.issue_review": ("fix_eligible", "report_issue", "unclassified", "failed"),
    "issue-reconcile.issue_reconcile": ("ready", "failed"),
}


def _stub_export(marker: str, *, status: str | None = None) -> BoundFlow:
    outcomes = _STUB_OUTCOMES[marker]
    terminal = status or _feature_success_status(marker)
    model = _CaseToyInput if marker == "intake.case" else _ToyInput
    flow = Flow(marker.replace(".", "-").replace("_", "-"), input=model, outcomes=outcomes)
    flow.step(
        "echo",
        _PrepareToyOp(outcomes) if marker == "intake.prepare" else _toy_task(outcomes),
        on_failure="failed" if "failed" in outcomes else outcomes[-1],
        route_on="public_outcome",
        routes={name: name for name in outcomes},
    )
    if marker == "intake.prepare":
        flow.control("echo", plan_digest="plan_digest")
    return flow.bind(_SucceedContext(terminal))


def _stub_features() -> dict[str, object]:
    return {
        "assurance.intake": IntakeGraphs(
            prepare=_stub_export("intake.prepare", status="prepared"),
            case=_stub_export("intake.case", status="reviewed"),
            coverage_rework=_stub_export("intake.coverage-rework", status="done"),
        ),
        "assurance.generation": GenerationGraphs(
            generation=_stub_export("generation.generation"),
            init_runtime=_stub_export("generation.init_runtime", status="completed"),
        ),
        "assurance.execution": ExecutionGraphs(
            execute=_stub_export("execution.execute"),
            rerun=_stub_export("execution.rerun"),
        ),
        "assurance.quality": QualityGraphs(
            assess=_stub_export("quality.assess"),
            issue_review=_stub_export("quality.issue_review", status="fix_eligible"),
            issue_analyze=_stub_export("quality.issue_analyze", status="fix_eligible"),
            issue_reconcile=_stub_export("quality.issue_reconcile", status="ready"),
            report=_stub_export("quality.report"),
            fact_baseline=_stub_export("quality.fact_baseline"),
            surface_baseline=_stub_export("quality.surface_baseline"),
        ),
        "assurance.healing": HealingGraphs(
            repair_failure=_stub_export("healing.repair_failure"),
        ),
        "assurance.improvement": ImprovementGraphs(
            archive=_stub_export("improvement.archive"),
            retro=_stub_export("improvement.retro"),
            review=_stub_export("improvement.review"),
            evaluate=_stub_export("improvement.evaluate"),
            export=_stub_export("improvement.export"),
            apply=_stub_export("improvement.apply"),
            rollback=_stub_export("improvement.rollback"),
            runtime_snapshot=_stub_export("improvement.runtime_snapshot", status="done"),
        ),
    }


def _build_context(checkpointer: Checkpointer = None) -> EngineGraphBuildContext:
    return EngineGraphBuildContext(
        contracts={},
        checkpointer=checkpointer,
        approved_source_roots=(),
    )


def _family_policy() -> dict[str, object]:
    from assurance_intake.contracts import TestFamilyPolicyV1

    return TestFamilyPolicyV1(required=("api",), allowed=("api",)).model_dump(mode="json")


def _invoke(graph: object, payload: Mapping[str, object]) -> dict[str, Any]:
    result = asyncio.run(graph.ainvoke(payload))  # type: ignore[union-attr]
    if not isinstance(result, dict):
        raise TypeError("root invoke must return a mapping")
    return result


def _public_input(entrypoint: str) -> dict[str, object]:
    case_delta = (_CASE_DELTA,) if entrypoint == "intake" else ()
    candidate = ("api",) if entrypoint == "intake" else ()
    payload = ProductInputV1.model_validate(
        valid_product_input(
            case_delta_paths=case_delta,
            candidate_test_families=candidate,
        )
    ).model_dump(mode="json")
    if entrypoint == "intake":
        payload["family_policy"] = _family_policy()
    return payload


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
def thin_graphs() -> ThinEntrypointGraphs:
    return build_thin_entrypoint_graphs(context=_build_context(), features=_real_features())


def test_factory_accepts_exactly_six_owner_ids(
    real_features: dict[str, object], thin_graphs: ThinEntrypointGraphs
) -> None:
    assert tuple(sorted(real_features)) == tuple(sorted(CAPABILITY_OWNERS))
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
    assert len(thin_graphs.entrypoints) == 6


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
    assert set(thin_graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS) - {"full"}
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
    assert len(names) == 6


@pytest.mark.parametrize("entrypoint", tuple(sorted(THIN_ENTRYPOINTS)))
def test_each_thin_root_validates_invokes_declared_export_and_publishes(
    entrypoint: str,
) -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    root = graphs.entrypoints[entrypoint]
    owner_id, export = _THIN_EXPORTS[entrypoint]
    marker = f"{owner_id.split('.')[-1]}.{export}"
    result = _invoke(root, _public_input(entrypoint))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.change_id == "CH-DEMO-001"
    assert output.status == "completed"
    assert CHECKPOINT_MARKERS_STATE_KEY not in output.model_dump(mode="json")
    del marker
    assert output.receipts == ()


def test_every_product_root_compiles_and_thin_roots_run() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert set(graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    for name in PRODUCT_ENTRYPOINTS:
        assert graphs.entrypoints[name] is not None
    thin = build_thin_entrypoint_graphs(context=_build_context(), features=_stub_features())
    for name in THIN_ENTRYPOINTS:
        result = _invoke(thin.entrypoints[name], _public_input(name))
        assert result["status"] in {"completed", "failed"}


def test_thin_root_rejects_non_public_input() -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    with pytest.raises((ValueError, ValidationError, TypeError)):
        _invoke(graphs.entrypoints["intake"], {"change_id": "CH-DEMO-001"})


def test_compiled_root_state_keeps_checkpoint_markers_off_public_io() -> None:
    from assurance_product.graphs.factory import declared_root_flows
    from assurance_product.graphs.revisions import contract_for_root
    from typing import get_type_hints

    flow = declared_root_flows()["init"]
    contract = contract_for_root("init", flow)
    from graph_engine.flow import root_schemas

    state_type, _, _, _ = root_schemas(flow)
    hints = get_type_hints(state_type, include_extras=True)
    assert CHECKPOINT_MARKERS_STATE_KEY in hints
    assert CHECKPOINT_MARKERS_STATE_KEY not in ProductPublicOutput.model_fields
    assert CHECKPOINT_MARKERS_STATE_KEY not in ProductInputV1.model_fields
    assert contract.state_schema_version == "6"


def test_thin_roots_compile_dry_and_runtime_with_matching_projections() -> None:
    saver = InMemorySaver()
    features = _real_features()
    dry = build_thin_entrypoint_graphs(context=_build_context(None), features=features)
    runtime = build_thin_entrypoint_graphs(context=_build_context(saver), features=features)
    assert set(dry.entrypoints) == set(runtime.entrypoints) == set(THIN_ENTRYPOINTS)
    for name in THIN_ENTRYPOINTS:
        dry_graph = dry.entrypoints[name]
        runtime_graph = runtime.entrypoints[name]
        assert set(dry_graph.nodes) == set(runtime_graph.nodes)
        assert dry_graph.checkpointer is None
        assert runtime_graph.checkpointer is saver


def test_thin_root_uses_schema_different_child_state() -> None:
    from assurance_product.graphs.factory import declared_root_flows
    from graph_engine.flow.declare import SubflowNode, declared_flow

    roots = declared_root_flows()
    for entrypoint, (owner_id, _export) in _THIN_EXPORTS.items():
        root = roots[entrypoint]
        child = next(declared_flow(node.child) for node in root.nodes if isinstance(node, SubflowNode))
        assert child is not None
        assert child.input is not root.input
        del owner_id


@pytest.mark.parametrize(
    ("entrypoint", "field", "feature_status", "expected"),
    [
        ("issue-review", "issue_review", "fix_eligible", "completed"),
        ("issue-review", "issue_review", "report_issue", "completed"),
        ("issue-review", "issue_review", "unclassified", "completed"),
        ("issue-review", "issue_review", "failed", "failed"),
        ("issue-reconcile", "issue_reconcile", "ready", "completed"),
    ],
)
def test_thin_root_publishes_real_feature_terminals(
    entrypoint: str,
    field: str,
    feature_status: str,
    expected: str,
) -> None:
    features = _stub_features()
    child = _stub_export(f"{entrypoint}.{field}", status=feature_status)
    owner_id, _export = _THIN_EXPORTS[entrypoint]
    bundle = features[owner_id]
    features[owner_id] = replace(bundle, **{field: child})  # type: ignore[arg-type]
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    result = _invoke(graphs.entrypoints[entrypoint], _public_input(entrypoint))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == expected
    assert result["status"] == expected
    assert result["terminal"] == {"status": expected, "reason": expected}


def test_thin_root_does_not_publish_input_or_feature_artifacts_as_receipts() -> None:
    features = _stub_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    payload = _public_input("intake")
    payload["artifacts"] = [{"path": "input.case", "digest": _SHA}]
    result = _invoke(graphs.entrypoints["intake"], payload)
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == "completed"
    assert output.receipts == ()
    quality = _invoke(graphs.entrypoints["issue-review"], _public_input("issue-review"))
    assert ProductPublicOutput.model_validate(quality["output"]).receipts == ()


def test_intake_root_passes_the_application_family_policy_to_prepare() -> None:
    from assurance_intake.contracts import TestFamilyPolicyV1

    seen: list[object] = []

    class _PrepareProbe(FrozenModel):
        family_policy: TestFamilyPolicyV1
        source_artifacts: object = None
        coverage_epoch: int = 0
        healing_rounds_used: int = 0

    class _RecordContext(_SucceedContext):
        def attempt(
            self,
            contract_id: str,
            *,
            semantic_node_id: object,
            activation: object,
            select: object,
            publish: object,
        ) -> object:
            del contract_id, semantic_node_id, activation, select

            async def _ok(state: object, runtime: object = None) -> dict[str, object]:
                del runtime
                payload = state if isinstance(state, Mapping) else {}
                if isinstance(payload, Mapping):
                    seen.append(payload.get("family_policy"))
                raw: dict[str, object] = {"public_outcome": "prepared", "plan_digest": "a" * 64}
                if callable(publish):
                    published = publish(
                        payload,
                        raw,
                        None,
                        committed=(ArtifactRef(path="qa/results/plan", digest=_SHA),),
                    )
                    if isinstance(published, Mapping):
                        return {str(name): value for name, value in published.items()}
                return raw

            return _ok

    outcomes = ("prepared", "failed")
    flow = Flow("prepare", input=_PrepareProbe, outcomes=outcomes)
    flow.step(
        "record",
        _PrepareToyOp(outcomes),
        on_failure="failed",
        route_on="public_outcome",
        routes={name: name for name in outcomes},
    )
    flow.control("record", plan_digest="plan_digest")
    features = _stub_features()
    intake = features["assurance.intake"]
    assert isinstance(intake, IntakeGraphs)
    features["assurance.intake"] = replace(intake, prepare=flow.bind(_RecordContext("prepared")))
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    payload = _public_input("intake")

    result = _invoke(graphs.entrypoints["intake"], payload)

    assert result["status"] == "completed"
    assert seen == [payload["family_policy"]]
    assert payload["family_policy"] == _family_policy()


def test_thin_root_publishes_a_child_receipt() -> None:
    from pydantic import BaseModel

    from graph_engine.attempts.contracts import AttemptRetryPolicy, AttemptTimeoutPolicy
    from graph_engine.attempts.resolutions import ReceiptRef
    from graph_engine.flow import Flow
    from graph_engine.plugin_api import ResourceClaims
    from graph_engine.testing import committed

    class ChangeInput(BaseModel):
        change_id: str = "CH-DEMO-001"

    class MarkerOutput(BaseModel):
        marker: str = "ok"

    task = TaskAttemptContract(
        contract_id="assurance.generation.task.init-receipt",
        owner_id="assurance.generation",
        handler_id="assurance.generation.init-receipt",
        input_model=ChangeInput,
        output_model=MarkerOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )
    child = Flow("init-runtime", input=ChangeInput, outcomes=("completed", "failed"))
    child.step("init-runtime", task, on_failure="failed", then="completed")
    child.publish_receipt("init-runtime")
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.generation",
        contracts={task.contract_id: task},
    )
    features = _stub_features()
    generation = features["assurance.generation"]
    assert isinstance(generation, GenerationGraphs)
    features["assurance.generation"] = replace(generation, init_runtime=child.bind(context))
    graph = thin_root_flows(coerce_feature_bundles(features))["init"].compile(_build_context())
    receipt = ReceiptRef(receipt_id="receipt-init", receipt_digest=_SHA)
    ran = asyncio.run(
        harness.run(
            graph,
            input=_public_input("init"),
            script={"generation.init-runtime": [committed(MarkerOutput(), receipt)]},
        )
    )
    assert isinstance(ran.terminal, Mapping)
    output = ProductPublicOutput.model_validate(ran.terminal["output"])
    assert [item.model_dump(mode="json") for item in output.receipts] == [
        {"receipt_id": "receipt-init", "receipt_digest": _SHA}
    ]


def test_issue_review_thin_entry_publishes_failed_when_the_attempt_fails() -> None:
    features = _real_features()
    graphs = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    result = asyncio.run(graphs.entrypoints["issue-review"].ainvoke(_public_input("issue-review")))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == "failed"
    assert result["status"] == "failed"
