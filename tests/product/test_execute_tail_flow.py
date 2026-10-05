"""Execute-tail flow, and the wheel fills that replace handwritten adapter math."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from assurance_execution.contracts.agent import ExecutionPrepareInputV1, RerunPrepareInputV1
from assurance_execution.graphs.factory import build_execution_graphs
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_generation.graphs.factory import build_generation_graphs
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_healing.graphs.factory import build_healing_graphs
from assurance_improvement.graphs.factory import build_improvement_graphs
from assurance_intake.handoff import REVIEWED_CASE
from assurance_product.graphs.execute_tail import ExecuteTailFlowInput, build_execute_tail_flow
from assurance_quality.graphs.factory import build_quality_graphs
from assurance_quality.ops.issue_analysis.hooks import require_issue_analysis_ready
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.flow.sources import LoopTarget
from graph_engine.testing.graph_harness import GraphHarness, _prepare_anchored_backend, committed
from tests.product.test_product_stategraph_flow import (
    _PLAN_DIGEST,
    _SHA,
    _analysis_result,
    _execution,
    _generation,
    _inspection,
    _plan_ref,
    _public_input,
    _ref,
    _report,
    _reviewed,
)
from tests.product.test_stategraph_entrypoints import _contracts_for

pytestmark = pytest.mark.usefixtures("installed_sources")

_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _json(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, tuple):
        return [_json(item) for item in value]
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _same(left: object, right: object) -> None:
    assert _json(left) == _json(right)


def _upstream(*, disposition: str = "repairable_execution_failure") -> dict[str, object]:
    public = _public_input("full", capability_leafs=("auth.session",))
    inspection = _inspection(disposition=disposition)
    execution = cast(dict[str, object], _execution()["execution_result"])
    generation = cast(dict[str, object], _generation()["generation_result"])
    return {
        **public,
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "reviewed_refs": [item.model_dump(mode="json") for item in _reviewed().preparation_refs],
        "coverage_epoch": 0,
        "selected_test_families": ["api"],
        "source_artifacts": list(cast(list[object], generation["source_refs"])),
        "generation_result": generation,
        "execution_result": execution,
        "execution_receipt": execution["receipt"],
        "inspection_outcome": inspection["inspection_outcome"],
        "assessment_inputs": inspection["assessment_inputs"],
        "fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA},
        "healing_rounds_used": 0,
        "evidence_refs": [],
    }


def _without(payload: Mapping[str, object], *names: str) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key not in names}


def test_only_repairable_and_fix_eligible_enter_repair() -> None:
    flow = build_execute_tail_flow(_bundles(_real_features_one()))
    entered = [
        (node.name, outcome)
        for node in flow.nodes
        for outcome, route in getattr(node, "routes", {}).items()
        if isinstance(route, LoopTarget) and route.target == "repair"
    ]
    assert entered == [
        ("quality", "repairable_execution_failure"),
        ("issue-analyze", "fix_eligible"),
    ]


def test_execute_tail_compiles_with_the_product_context() -> None:
    flow = build_execute_tail_flow(_bundles(_real_features_one()))
    from langgraph.checkpoint.memory import MemorySaver

    compiled = flow.compile(
        EngineGraphBuildContext(contracts={}, checkpointer=MemorySaver(), approved_source_roots=())
    )
    assert compiled is not None
    ExecuteTailFlowInput.model_validate(_tail_input())
    with pytest.raises(Exception, match="reviewed_refs"):
        ExecuteTailFlowInput.model_validate({**_tail_input(), "reviewed_refs": []})


class _Run:
    def __init__(
        self,
        result: object,
        captured: list[tuple[str, object]],
        compiled: object,
        config: RunnableConfig,
    ) -> None:
        self.result = result
        self.captured = captured
        self.compiled = compiled
        self.config = config

    @property
    def outcome(self) -> object:
        if isinstance(self.result, Mapping):
            return self.result.get("flow_outcome")
        return None

    async def resume(self, decision: Mapping[str, object]) -> _Run:
        try:
            self.result = await self.compiled.ainvoke(Command(resume=dict(decision)), self.config)  # type: ignore[attr-defined]
        except GraphInterrupt as error:
            self.result = {"__interrupt__": error.args}
        return self


def _real_features_one() -> dict[str, object]:
    harness = GraphHarness()
    return _features(harness)


def _features(harness: GraphHarness) -> dict[str, object]:
    builders = {
        "assurance.generation": build_generation_graphs,
        "assurance.execution": build_execution_graphs,
        "assurance.quality": build_quality_graphs,
        "assurance.healing": build_healing_graphs,
        "assurance.improvement": build_improvement_graphs,
    }
    return {
        owner: builder(harness.recording_context(owner_id=owner, contracts=_contracts_for(owner)))
        for owner, builder in builders.items()
    }


def _bundles(features: Mapping[str, object]) -> object:
    class Bundles:
        quality = features["assurance.quality"]
        generation = features["assurance.generation"]
        execution = features["assurance.execution"]
        healing = features["assurance.healing"]
        improvement = features["assurance.improvement"]

    return Bundles()


def _tail_input(**overrides: object) -> dict[str, object]:
    public = _public_input("full", capability_leafs=("auth.session",))
    payload: dict[str, object] = {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "reviewed_refs": [item.model_dump(mode="json") for item in _reviewed().preparation_refs],
        "source_artifacts": [],
        "selected_test_families": ["api"],
        "capability_leafs": ["auth.session"],
        "allowed_artifact_paths": list(cast(tuple[str, ...], public["allowed_artifact_paths"])),
        "budgets": public["budgets"],
        "product_policy": public["product_policy"],
        "allowed_origins": [],
        "timeout_seconds": 3600,
        "preparation_refs": [],
        "retro_window": None,
    }
    payload.update(overrides)
    return payload


def _failure() -> PermanentTaskFailure:
    return PermanentTaskFailure(kind="invalid_output", message="scripted failure")


def _generation_script() -> dict[str, list[object]]:
    published = {"generation_result": _generation()["generation_result"]}
    cycle = {"path": "qa/results/codegen/generation-cycle.json", "digest": _SHA}
    return {
        "generation.resolve-inputs": [committed(_reviewed().model_dump(mode="json"), _RECEIPT)],
        "generation.api.codegen": [committed({"schema_version": "1"}, _RECEIPT)],
        "generation.api.codegen-review": [committed({"route": "codegen"}, _RECEIPT)],
        "generation.publish-cycle": [committed(published, _RECEIPT, artifacts=[cycle])],
    }


def _execution_output(*, repair_round: int = 0) -> dict[str, object]:
    execution = dict(cast(dict[str, object], _execution(repair_round=repair_round)["execution_result"]))
    return {
        "admission": "committed",
        "batch_id": execution["batch_id"],
        "execution_evidence": {"status": "PASS"},
        "execution_digest": _SHA,
        "execution_semantic_node_id": "execution.execute" if repair_round == 0 else "execution.run",
        "family_outcomes": [{"family": "api", "state": "executed"}],
        "execution_result": execution,
    }


def _inspect_output(
    disposition: str, *, repair_round: int = 0, owned: list[str] | None = None
) -> dict[str, object]:
    inspection = _inspection(disposition=disposition, repair_round=repair_round)
    if owned is not None:
        assessment = dict(cast(dict[str, object], inspection["assessment_inputs"]))
        assessment["owned_evidence_ids"] = owned
        inspection = dict(inspection)
        inspection["assessment_inputs"] = assessment
        inspection["owned_evidence_ids"] = owned
    return {
        "disposition": disposition,
        "inspection_outcome": inspection["inspection_outcome"],
        "assessment": inspection["assessment_inputs"],
        "evidence_refs": [{"path": "qa/results/inspect/extra-evidence.json", "digest": "1" * 64}],
        "coverage_state": inspection["coverage_state"],
        "observations_ref": inspection["observations_ref"],
        "issue_evidence_manifest_ref": inspection["issue_evidence_manifest_ref"],
        "owned_evidence_ids": inspection["owned_evidence_ids"],
        "evidence_bundle_digest": inspection["evidence_bundle_digest"],
    }


def _report_output(publication: str) -> dict[str, object]:
    report = _report()
    return {
        "publication": publication,
        "report_outcome": report["report_outcome"],
        "report_refs": report["report_refs"],
        "coverage_state": "satisfied" if publication == "reported" else "repair_required",
    }


def _committed_inspect(*args: object, **kwargs: object) -> object:
    return committed(
        _inspect_output(*args, **kwargs),  # type: ignore[arg-type]
        _RECEIPT,
        artifacts=[{"path": "qa/results/inspect/inspection-outcome.json", "digest": _SHA}],
    )


def _materialize() -> object:
    from assurance_quality.contracts.assessment import ASSESSMENT_INPUTS_PATH

    return committed(
        {},
        _RECEIPT,
        artifacts=[{"path": ASSESSMENT_INPUTS_PATH, "digest": _SHA}],
    )


def _through_execute() -> dict[str, list[object]]:
    script: dict[str, list[object]] = {
        "quality.fact-baseline": [
            committed(
                {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
                _RECEIPT,
                artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
            )
        ],
    }
    script.update(_generation_script())
    script["execution.execute"] = [
        committed(
            _execution_output(),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        )
    ]
    return script


def _continue_diagnostic(script: dict[str, list[object]]) -> None:
    from assurance_improvement.contracts.handoff import CANDIDATES, COLLECTED, CONTEXT, SLICE_PATH

    snapshot_ref = {"path": "qa/results/issues/snapshot.json", "digest": "f" * 64}
    runtime_ref = {
        "path": "qa/results/workflow/" + "c" * 64 + "/pre-retro/workflow-evidence.json",
        "digest": "d" * 64,
    }
    script["quality.issue-reconcile"] = [
        committed(
            {
                "classification": "unknown",
                "fix_eligible": False,
                "evidence_refs": [{"path": "qa/results/inspect/issue-analysis.json", "digest": _SHA}],
                "issue_snapshot_ref": snapshot_ref,
            },
            _RECEIPT,
            artifacts=[snapshot_ref],
        )
    ]
    script["improvement.retro-runtime-snapshot"] = [
        committed({"evidence_ref": runtime_ref}, _RECEIPT, artifacts=[runtime_ref])
    ]
    slice_artifacts = [{"path": path, "digest": _SHA} for path in SLICE_PATH.values()]
    for semantic, output, artifacts in (
        ("improvement.retro-build-slices", {}, slice_artifacts),
        ("improvement.retro-collect", {}, [{"path": COLLECTED, "digest": _SHA}]),
        (
            "improvement.retro-eval-analysis",
            {},
            [{"path": "qa/results/retro/retro-eval-analysis.json", "digest": _SHA}],
        ),
        (
            "improvement.retro-issue-analysis",
            {},
            [{"path": "qa/results/retro/retro-issue-analysis.json", "digest": _SHA}],
        ),
        (
            "improvement.retro-workflow-analysis",
            {},
            [{"path": "qa/results/retro/retro-workflow-analysis.json", "digest": _SHA}],
        ),
        ("improvement.retro-synthesize", {"route": "empty"}, [{"path": CONTEXT, "digest": _SHA}]),
        ("improvement.retro", {"analysis_status": "ok"}, [{"path": CANDIDATES, "digest": _SHA}]),
        ("improvement.retro-reconcile", {"status": "done", "artifact_refs": []}, []),
    ):
        script[semantic] = [committed(output, _RECEIPT, artifacts=artifacts)]


_ANALYSIS_ARTIFACT = {"path": "qa/results/inspect/issue-analysis.json", "digest": _SHA}
_HANDOFF_ARTIFACT = {"path": "qa/results/healing/issue-analysis-handoff.json", "digest": _SHA}


def _issue_output(route: str) -> dict[str, object]:
    analysis = _analysis_result("test" if route == "fix_eligible" else "unknown")
    ref = dict(_ANALYSIS_ARTIFACT)
    return {
        "route": route,
        "classification": "test" if route == "fix_eligible" else "unknown",
        "fix_eligible": route == "fix_eligible",
        "evidence_refs": [ref],
        "issue_analysis": analysis["issue_analysis"],
        "issue_analysis_ref": ref,
    }


def _committed_issue(route: str, *, handoff: bool = False) -> object:
    artifacts = [_ANALYSIS_ARTIFACT, _HANDOFF_ARTIFACT] if handoff else [_ANALYSIS_ARTIFACT]
    return committed(_issue_output(route), _RECEIPT, artifacts=artifacts)


async def _invoke(script: Mapping[str, list[object]], **overrides: object) -> _Run:
    harness = GraphHarness()
    flow = build_execute_tail_flow(_bundles(_features(harness)))
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    compiled = flow.compile(
        EngineGraphBuildContext(contracts={}, checkpointer=backend, approved_source_roots=())
    )
    harness._kernel.load_script(cast(Any, script))
    captured: list[tuple[str, object]] = []
    original = harness._kernel.execute_or_recover

    async def spy(attempt_key: Any, contract: Any, validated_input: Any, context: Any) -> Any:
        captured.append((str(getattr(context, "semantic_node_id")), validated_input))
        return await original(attempt_key, contract, validated_input, context)

    harness._kernel.execute_or_recover = spy  # type: ignore[method-assign]
    config: RunnableConfig = {
        "recursion_limit": 8192,
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "execute",
        },
    }
    try:
        payload = _tail_input(**overrides)
        payload["artifact_ledger"] = {
            REVIEWED_CASE.ledger_key: [
                {"path": "qa/cases/reviewed-case.json", "digest": _SHA},
            ],
        }
        result = await compiled.ainvoke(payload, config=config)
    except GraphInterrupt as error:
        result = {"__interrupt__": error.args}
    return _Run(result, captured, compiled, config)


def _budgets(healing_rounds: int) -> dict[str, int]:
    return {
        "review_rounds": 1,
        "coverage_rounds": 1,
        "healing_rounds": healing_rounds,
        "execution_retries": 1,
    }


def test_execute_receives_caller_origins_and_timeout() -> None:
    asyncio.run(_origins_and_timeout())


async def _origins_and_timeout() -> None:
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize()]
    script["quality.inspect"] = [_committed_inspect("satisfied")]
    script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    run = await _invoke(
        script,
        allowed_origins=["https://app.example"],
        timeout_seconds=120,
    )
    prepared = ExecutionPrepareInputV1.model_validate(
        next(item for name, item in run.captured if name == "execution.execute")
    )
    assert list(prepared.allowed_origins) == ["https://app.example"]
    assert prepared.timeout_seconds == 120


def test_each_outcome_and_each_failed_stage() -> None:
    asyncio.run(_each_outcome())


async def _each_outcome() -> None:
    satisfied = _through_execute()
    satisfied["quality.materialize-assessment-inputs"] = [_materialize()]
    satisfied["quality.inspect"] = [_committed_inspect("satisfied")]
    satisfied["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    reported = await _invoke(satisfied)
    assert reported.outcome == "reported"

    coverage = _through_execute()
    coverage["quality.materialize-assessment-inputs"] = [_materialize()]
    coverage["quality.inspect"] = [_committed_inspect("coverage_insufficient")]
    assert (await _invoke(coverage)).outcome == "coverage_insufficient"

    human = _through_execute()
    human["quality.materialize-assessment-inputs"] = [_materialize()]
    human["quality.inspect"] = [_committed_inspect("needs_human")]
    assert (await _invoke(human)).outcome == "needs_human"

    exhausted = _through_execute()
    exhausted["quality.materialize-assessment-inputs"] = [_materialize()]
    exhausted["quality.inspect"] = [_committed_inspect("repairable_execution_failure")]
    assert (await _invoke(exhausted, budgets=_budgets(0))).outcome == "needs_human"

    unclassified = _through_execute()
    unclassified["quality.materialize-assessment-inputs"] = [_materialize()]
    unclassified["quality.inspect"] = [_committed_inspect("analysis_required")]
    unclassified["quality.issue-analyze"] = [_committed_issue("unclassified")]
    assert (await _invoke(unclassified)).outcome == "needs_human"

    fix_exhausted = _through_execute()
    fix_exhausted["quality.materialize-assessment-inputs"] = [_materialize()]
    fix_exhausted["quality.inspect"] = [_committed_inspect("analysis_required")]
    fix_exhausted["quality.issue-analyze"] = [_committed_issue("fix_eligible", handoff=True)]
    assert (await _invoke(fix_exhausted, budgets=_budgets(0))).outcome == "needs_human"

    blocked_evidence = _through_execute()
    blocked_evidence["quality.materialize-assessment-inputs"] = [_materialize()]
    blocked_evidence["quality.inspect"] = [_committed_inspect("blocked", owned=[])]
    blocked_evidence["quality.issue-analyze"] = [_failure()]
    blocked = await _invoke(blocked_evidence)
    assert blocked.outcome == "blocked"
    assert [name for name, _input in blocked.captured].count("quality.issue-analyze") == 1
    from assurance_quality.contracts.agent import QualitySkillInputV1
    from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1

    inspection = InspectionOutcomeV1.model_validate(_inspection(disposition="blocked")["inspection_outcome"])
    assessment = AssessmentInputsV1.model_validate(_inspection(disposition="blocked")["assessment_inputs"])
    assessment = assessment.model_copy(update={"owned_evidence_ids": []})
    with pytest.raises(Exception, match="owned observations"):
        require_issue_analysis_ready(
            QualitySkillInputV1.model_construct(
                owned_evidence_ids=(), evidence_bundle_digest=assessment.evidence_bundle_digest
            ),
            inspection,
            assessment,
        )

    review = _through_execute()
    review["quality.materialize-assessment-inputs"] = [_materialize()]
    review["quality.inspect"] = [_committed_inspect("repairable_execution_failure")]
    review["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    paused = await _invoke(review, budgets=_budgets(1))
    assert isinstance(paused.result, Mapping)
    interrupt_args = paused.result["__interrupt__"]
    first = interrupt_args[0]
    payload = first[0].value if isinstance(first, tuple) else getattr(first, "value", first)
    assert payload["interrupt_id"] == "execute-tail.approval"
    rejected = await paused.resume({"action": "reject"})
    assert rejected.outcome == "needs_human"

    diagnostic = _through_execute()
    diagnostic["quality.materialize-assessment-inputs"] = [_materialize()]
    diagnostic["quality.inspect"] = [_committed_inspect("analysis_required")]
    diagnostic["quality.issue-analyze"] = [_committed_issue("report_issue")]
    diagnostic["quality.report"] = [committed(_report_output("diagnostic"), _RECEIPT)]
    snapshot_ref = {"path": "qa/results/issues/snapshot.json", "digest": "f" * 64}
    runtime_ref = {
        "path": "qa/results/workflow/" + "c" * 64 + "/pre-retro/workflow-evidence.json",
        "digest": "d" * 64,
    }
    diagnostic["quality.issue-reconcile"] = [
        committed(
            {
                "classification": "unknown",
                "fix_eligible": False,
                "evidence_refs": [{"path": "qa/results/inspect/issue-analysis.json", "digest": _SHA}],
                "issue_snapshot_ref": snapshot_ref,
            },
            _RECEIPT,
            artifacts=[snapshot_ref],
        )
    ]
    diagnostic["improvement.retro-runtime-snapshot"] = [
        committed({"evidence_ref": runtime_ref}, _RECEIPT, artifacts=[runtime_ref])
    ]
    from assurance_improvement.contracts.handoff import (
        CANDIDATES,
        COLLECTED,
        CONTEXT,
        SLICE_PATH,
    )

    slice_artifacts = [{"path": path, "digest": _SHA} for path in SLICE_PATH.values()]
    for semantic, output, artifacts in (
        ("improvement.retro-build-slices", {}, slice_artifacts),
        ("improvement.retro-collect", {}, [{"path": COLLECTED, "digest": _SHA}]),
        (
            "improvement.retro-eval-analysis",
            {},
            [{"path": "qa/results/retro/retro-eval-analysis.json", "digest": _SHA}],
        ),
        (
            "improvement.retro-issue-analysis",
            {},
            [{"path": "qa/results/retro/retro-issue-analysis.json", "digest": _SHA}],
        ),
        (
            "improvement.retro-workflow-analysis",
            {},
            [{"path": "qa/results/retro/retro-workflow-analysis.json", "digest": _SHA}],
        ),
        ("improvement.retro-synthesize", {"route": "empty"}, [{"path": CONTEXT, "digest": _SHA}]),
        ("improvement.retro", {"analysis_status": "ok"}, [{"path": CANDIDATES, "digest": _SHA}]),
        (
            "improvement.retro-reconcile",
            {"status": "done", "artifact_refs": []},
            [],
        ),
    ):
        diagnostic[semantic] = [committed(output, _RECEIPT, artifacts=artifacts)]
    diagnosed = await _invoke(diagnostic)
    assert diagnosed.outcome == "diagnostic"
    baseline = {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}
    report = cast(Any, next(item for name, item in diagnosed.captured if name == "quality.report"))
    assert report.fact_baseline_ref.model_dump(mode="json") == baseline
    assert report.purpose == "diagnostic"
    assert report.issue_analysis_ref.model_dump(mode="json") == _ANALYSIS_ARTIFACT
    slices = cast(
        Any, next(item for name, item in diagnosed.captured if name == "improvement.retro-build-slices")
    )
    delivered = {ref.path: ref.digest for ref in slices.source_refs}
    assert delivered[snapshot_ref["path"]] == snapshot_ref["digest"]
    assert delivered[runtime_ref["path"]] == runtime_ref["digest"]

    assert reported.outcome == "reported"
    assert isinstance(reported.result, Mapping)
    assert any(name == "quality.report" for name, _item in reported.captured)

    for semantic in (
        "quality.fact-baseline",
        "generation.resolve-inputs",
        "execution.execute",
        "quality.materialize-assessment-inputs",
        "quality.report",
    ):
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [_committed_inspect("satisfied")]
        script["quality.report"] = [_failure()]
        script[semantic] = [_failure()]
        assert (await _invoke(script)).outcome == "blocked"


def test_repair_then_rerun_feeds_the_next_assess(tmp_path: Path) -> None:
    asyncio.run(_repair_then_rerun(tmp_path))


async def _repair_then_rerun(tmp_path: Path) -> None:
    del tmp_path
    generation = GenerationCycleResultV1.model_validate(_generation()["generation_result"])
    changed = _ref(generation.source_refs[0].path, "d" * 64)
    mapping = _ref(generation.mapping_ref.path, "e" * 64)
    repair = AppliedTestRepairV1(
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        repair_round=1,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        status="applied",
        changed_test_refs=(changed,),
        mapping_ref=mapping,
        receipt=_RECEIPT,
    )
    verified = {
        "change_id": repair.change_id,
        "plan_digest": repair.plan_digest,
        "plan_ref": repair.plan_ref.model_dump(mode="json"),
        "coverage_epoch": repair.coverage_epoch,
        "repair_round": repair.repair_round,
        "changed_test_refs": [item.model_dump(mode="json") for item in repair.changed_test_refs],
        "mapping_ref": mapping.model_dump(mode="json"),
    }
    rerun_output = _execution_output(repair_round=1)
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize(), _materialize()]
    script["quality.inspect"] = [
        _committed_inspect("repairable_execution_failure"),
        _committed_inspect("satisfied", repair_round=1),
    ]
    script["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    script["healing.apply-test-repair"] = [
        committed(
            verified,
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        )
    ]
    script["execution.run"] = [
        committed(
            rerun_output,
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        )
    ]
    script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    paused = await _invoke(script, budgets=_budgets(1))
    assert isinstance(paused.result, Mapping)
    assert paused.outcome != "reported"
    finished = await paused.resume(
        {
            "action": "approve",
            "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
        }
    )
    assert finished.outcome == "reported"
    rerun_input = next(item for name, item in finished.captured if name == "execution.run")
    assert isinstance(rerun_input, RerunPrepareInputV1)
    assert rerun_input.generation_ref == _ref("qa/results/codegen/generation-cycle.json", _SHA)
    assert rerun_input.applied_repair_ref == _ref("qa/results/healing/applied-repair.json", _SHA)
    assert rerun_input.apply_receipt == _RECEIPT
    assess_inputs = [
        item for name, item in finished.captured if name == "quality.materialize-assessment-inputs"
    ]
    assert len(assess_inputs) == 2
    assert getattr(assess_inputs[0], "repair_round") == 0
    assert getattr(assess_inputs[1], "repair_round") == 1
    assert getattr(assess_inputs[1], "execution_ref").path == "qa/results/execution/execution-cycle.json"


def test_normal_report_after_fix_eligible_repair_reaches_reported() -> None:
    asyncio.run(_normal_report_after_fix_eligible())


async def _normal_report_after_fix_eligible() -> None:
    """Same tail: issue-analyze fix_eligible, repair, rerun, satisfied, normal report."""
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize(), _materialize()]
    script["quality.inspect"] = [
        _committed_inspect("analysis_required"),
        _committed_inspect("satisfied", repair_round=1),
    ]
    script["quality.issue-analyze"] = [_committed_issue("fix_eligible", handoff=True)]
    script["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    generated = cast(dict[str, object], _generation()["generation_result"])
    sources = generated["source_refs"]
    mapping = generated["mapping_ref"]
    assert isinstance(sources, list) and sources and isinstance(mapping, dict)
    source = sources[0]
    assert isinstance(source, dict)
    script["healing.apply-test-repair"] = [
        committed(
            {
                "change_id": "CH-DEMO-001",
                "plan_digest": _PLAN_DIGEST,
                "plan_ref": _plan_ref().model_dump(mode="json"),
                "coverage_epoch": 0,
                "repair_round": 1,
                "changed_test_refs": [{"path": source["path"], "digest": "d" * 64}],
                "mapping_ref": {"path": mapping["path"], "digest": "e" * 64},
            },
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        )
    ]
    script["execution.run"] = [
        committed(
            _execution_output(repair_round=1),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        )
    ]
    script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    paused = await _invoke(script, budgets=_budgets(1))
    done = await paused.resume(
        {"action": "approve", "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA}}
    )
    assert done.outcome == "reported"
    proposal = next(item for name, item in done.captured if name == "healing.fix-proposal")
    assert getattr(proposal, "coverage_epoch") == 0
    assert getattr(proposal, "issue_analysis_handoff_ref").model_dump(mode="json") == _HANDOFF_ARTIFACT
    normal = next(item for name, item in done.captured if name == "quality.report")
    assert getattr(normal, "purpose") == "normal"
    assert getattr(normal, "issue_analysis_ref") is None


def _repair_round_of(value: object) -> int:
    if isinstance(value, dict):
        raw = value.get("repair_round")
    else:
        raw = getattr(value, "repair_round", None)
    assert isinstance(raw, int)
    return raw


def test_quality_repair_round_matches_the_execution_file() -> None:
    asyncio.run(_quality_repair_round_matches_the_execution_file())


async def _quality_repair_round_matches_the_execution_file() -> None:
    """First entry, one repair, then issue-analyze fix_eligible and another repair."""

    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize(), _materialize(), _materialize()]
    script["quality.inspect"] = [
        _committed_inspect("repairable_execution_failure"),
        _committed_inspect("analysis_required", repair_round=1),
        _committed_inspect("satisfied", repair_round=2),
    ]
    script["quality.issue-analyze"] = [_committed_issue("fix_eligible", handoff=True)]
    script["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        ),
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        ),
    ]
    generated = cast(dict[str, object], _generation()["generation_result"])
    sources = generated["source_refs"]
    mapping = generated["mapping_ref"]
    assert isinstance(sources, list) and sources and isinstance(mapping, dict)
    source = sources[0]
    assert isinstance(source, dict)
    applied = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "coverage_epoch": 0,
        "repair_round": 1,
        "changed_test_refs": [{"path": source["path"], "digest": "d" * 64}],
        "mapping_ref": {"path": mapping["path"], "digest": "e" * 64},
    }
    second = dict(applied)
    second["repair_round"] = 2
    script["healing.apply-test-repair"] = [
        committed(
            applied,
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        ),
        committed(
            second,
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        ),
    ]
    first_file = _execution_output(repair_round=0)
    rerun_one = _execution_output(repair_round=1)
    rerun_two = _execution_output(repair_round=2)
    script["execution.run"] = [
        committed(
            rerun_one,
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
        committed(
            rerun_two,
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    ]
    script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    paused = await _invoke(script, budgets=_budgets(3))
    resumed = await paused.resume(
        {"action": "approve", "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA}}
    )
    done = await resumed.resume(
        {"action": "approve", "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA}}
    )
    bound = [
        _repair_round_of(item)
        for name, item in done.captured
        if name == "quality.materialize-assessment-inputs"
    ]
    files = [
        _repair_round_of(cast(dict[str, object], first_file["execution_result"])),
        _repair_round_of(cast(dict[str, object], rerun_one["execution_result"])),
        _repair_round_of(cast(dict[str, object], rerun_two["execution_result"])),
    ]
    assert bound == files == [0, 1, 2]


def test_retro_binds_the_current_report_receipt_and_review_history() -> None:
    from graph_engine.flow.declare import SubflowNode
    from graph_engine.flow.sources import LedgerRefs
    from graph_engine.testing.graph_harness import GraphHarness

    from assurance_intake.ops.case_review import op as case_review
    from assurance_product.graphs.execute_tail import build_execute_tail_flow

    flow = build_execute_tail_flow(_bundles(_features(GraphHarness())))
    retro = next(node for node in flow.nodes if getattr(node, "name", None) == "retro")
    assert isinstance(retro, SubflowNode)
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
    from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS as QUALITY_TASKS

    from graph_engine.flow.sources import LedgerReceipt

    receipt = retro.inputs["report_receipt"]
    assert isinstance(receipt, LedgerReceipt)
    runtime = retro.inputs["runtime_ref"]
    assert isinstance(runtime, LedgerRefs)
    assert runtime.many is False
    assert (
        runtime.key
        == IMPROVEMENT_TASKS["assurance.improvement.retro-runtime-snapshot"]
        .artifact("runtime-evidence")
        .ledger_key
    )
    snapshot = retro.inputs["issue_snapshot_ref"]
    assert isinstance(snapshot, LedgerRefs)
    assert snapshot.many is False
    assert snapshot.key == QUALITY_TASKS["reconcile-issues"].artifact("snapshot").ledger_key
    assert retro.inputs["window"] == "retro_window"
    assert retro.inputs["preparation_refs"] == "preparation_refs"
    assert retro.inputs["reviewed_refs"] == "reviewed_refs"
    carried = retro.inputs["carried_evidence_refs"]
    assert isinstance(carried, LedgerRefs)
    assert carried.many is True
    assert carried.key == QUALITY_TASKS["reconcile-issues"].artifact("snapshot").ledger_key
    history = retro.inputs["history_refs"]
    assert isinstance(history, LedgerRefs)
    assert history.many is True
    assert history.key == case_review.artifact("history").ledger_key
