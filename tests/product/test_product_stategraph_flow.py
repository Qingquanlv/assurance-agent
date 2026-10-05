from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assurance_execution.contracts.evidence import FamilyExecutionOutcomeV1
from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
)
from assurance_product.graphs.factory import (
    ProductGraphs,
    build_product_graphs,
    build_thin_entrypoint_graphs,
)
from assurance_product.graphs.revisions import ENTRYPOINT_RECURSION_LIMITS
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1
from assurance_quality.contracts.assessment import (
    InspectionOutcomeV1,
    ReportOutcomeV1,
)
from assurance_quality.contracts.surface import (
    API_DISCOVERY_PATH,
    UI_EXPLORATION_PATH,
    ApiDiscoveryDocument,
    UiExplorationDocument,
)
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.boot.boot import EngineGraphBuildContext
from tests.architecture.exclusive_route_inventory import EXCLUSIVE_ROUTE_INVENTORY
from tests.product.test_product_input import valid_product_input
from tests.product.test_stategraph_entrypoints import _real_features

_SHA = "a" * 64
_PLAN_DIGEST = "b" * 64
_CASE_DELTA = "qa/cases/system/dept/case.yaml"


def _ref(path: str, digest: str = _SHA) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=digest)


def _receipt(name: str) -> ReceiptRef:
    return ReceiptRef(receipt_id=name, receipt_digest=_SHA)


def _plan_ref() -> EvidenceArtifactRefV1:
    return _ref(
        f"qa/results/plan/{_PLAN_DIGEST}/resolved-assurance-plan.json",
        "c" * 64,
    )


def _plan_update() -> dict[str, object]:
    return {
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "selected_test_families": ["api"],
    }


def _reviewed(epoch: int = 0) -> ReviewedCaseV1:
    return ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        preparation_refs=(
            _plan_ref(),
            _ref("qa/results/preparation/context.json"),
        ),
        case_refs=(_ref(_CASE_DELTA),),
        review_ref=_ref("qa/results/review/case-review.json"),
        selection_ref=_ref(f"qa/results/cases/epochs/{epoch}/selection.json"),
    )


def _case(epoch: int = 0) -> dict[str, object]:
    reviewed = _reviewed(epoch)
    receipt = _receipt(f"case-{epoch}")
    return {
        **CaseFlowResultV1(status="reviewed", reviewed_case=reviewed, receipt=receipt).model_dump(
            mode="json"
        ),
        "case_receipt": receipt.model_dump(mode="json"),
        "decision": "pass",
    }


def _fact_baseline() -> dict[str, object]:
    return {
        "fact_baseline_ref": _ref("qa/results/facts/fact-baseline.json").model_dump(mode="json"),
        "status": "done",
    }


def _surface_baseline() -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="surface-baseline-"))
    ui = UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-DEMO-001",
            "source": "live",
            "base_url": "",
            "warnings": [],
            "features": [],
        }
    )
    api = ApiDiscoveryDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-DEMO-001",
            "source": "live",
            "base_url": "http://127.0.0.1:9999",
            "warnings": [],
            "families": [
                {
                    "name": "user",
                    "auth": "none",
                    "operations": [
                        {
                            "method": "GET",
                            "path": "/api/v1/user/list",
                            "request": {"required_headers": [], "query": [], "body_fields": []},
                            "response": {"status_codes": [200], "body_fields": []},
                        }
                    ],
                }
            ],
        }
    )
    refs: dict[str, dict[str, str]] = {}
    for relative, document in ((UI_EXPLORATION_PATH, ui), (API_DISCOVERY_PATH, api)):
        data = json.dumps(document.model_dump(mode="json"), sort_keys=True).encode()
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        refs[relative] = {"path": relative, "digest": hashlib.sha256(data).hexdigest()}
    return {
        "ui_exploration_ref": refs[UI_EXPLORATION_PATH],
        "api_discovery_ref": refs[API_DISCOVERY_PATH],
        "ui_exploration_source": ui.source,
        "api_discovery_source": api.source,
        "status": "ready",
    }


def _generation(epoch: int = 0) -> dict[str, object]:
    result = GenerationCycleResultV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        reviewed_case=_reviewed(epoch),
        mapping_ref=_ref(f"qa/results/generation/epochs/{epoch}/mapping.json"),
        source_refs=(_ref(f"qa/results/generated/epochs/{epoch}/tests/test_case.py"),),
        plan_refs=(_ref(f"qa/results/plans/epochs/{epoch}/api.json"),),
        method_plan_ref=_ref(f"qa/results/generation/epochs/{epoch}/obligation-methods.json"),
    )
    return {"generation_result": result.model_dump(mode="json"), "status": "passed"}


def _execution(epoch: int = 0, *, repair_round: int = 0, status: str = "PASS") -> dict[str, object]:
    generated = GenerationCycleResultV1.model_validate(_generation(epoch)["generation_result"])
    result = ExecutionCycleResultV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        repair_round=repair_round,
        batch_id=f"20260905T120{epoch}{repair_round}0Z",
        executed_at=datetime(2026, 9, 5, 12, epoch, repair_round, tzinfo=UTC),
        final_status=status,  # type: ignore[arg-type]
        evidence_ref=_ref(f"qa/results/execution/epochs/{epoch}/rounds/{repair_round}/result.json"),
        mapping_ref=generated.mapping_ref,
        source_refs=generated.source_refs,
        receipt=_receipt(f"execution-{epoch}-{repair_round}"),
        family_outcomes=(FamilyExecutionOutcomeV1(family="api", state="executed"),),
    )
    return {"execution_result": result.model_dump(mode="json"), "status": "committed"}


def _inspection(
    epoch: int = 0,
    disposition: str = "satisfied",
    *,
    repair_round: int = 0,
) -> dict[str, object]:
    execution = ExecutionCycleResultV1.model_validate(
        _execution(epoch, repair_round=repair_round)["execution_result"]
    )
    gaps = _ref(f"qa/results/inspect/epochs/{epoch}/gaps.json")
    observations = _ref(f"qa/results/inspect/epochs/{epoch}/batches/{execution.batch_id}/observations.json")
    issue_manifest = _ref(
        f"qa/results/inspect/epochs/{epoch}/batches/{execution.batch_id}/issue-evidence-manifest.json"
    )
    trace = _ref(f"qa/results/inspect/epochs/{epoch}/trace.json")
    metrics = _ref(f"qa/results/inspect/epochs/{epoch}/metrics.json")
    sufficiency = _ref(f"qa/results/inspect/epochs/{epoch}/trace-sufficiency.json")
    obligation = _ref(f"qa/results/inspect/epochs/{epoch}/batches/B-1/obligation-assessment.json")
    fact = _ref("qa/results/facts/fact-baseline.json")
    assessment_refs = tuple(
        sorted(
            (
                trace,
                gaps,
                metrics,
                sufficiency,
                execution.evidence_ref,
                observations,
                obligation,
                issue_manifest,
                fact,
            ),
            key=lambda item: (item.path, item.digest),
        )
    )
    coverage_state = {
        "satisfied": "satisfied",
        "coverage_insufficient": "repair_required",
    }.get(disposition)
    outcome = InspectionOutcomeV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        batch_id=execution.batch_id,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        disposition=disposition,  # type: ignore[arg-type]
        inspection_receipt=_receipt(f"inspect-{epoch}"),
        reviewed_case=_reviewed(epoch),
        mapping_ref=execution.mapping_ref,
        assessment_refs=assessment_refs,
        reason_codes=(f"inspection.{disposition}",),
        coverage_state=coverage_state,  # type: ignore[arg-type]
    )
    return {
        "inspection_outcome": outcome.model_dump(mode="json"),
        "assessment_inputs": {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": epoch,
            "batch_id": execution.batch_id,
            "plan_digest": _PLAN_DIGEST,
            "plan_ref": _plan_ref().model_dump(mode="json"),
            "scope": {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": epoch,
                "required_case_ids": ["TC-DEMO-001"],
                "selected_families": ["api"],
                "applicable_goals": ["constraint_coverage"],
                "applicability_refs": [_reviewed(epoch).review_ref.model_dump(mode="json")],
                "risk_tier": "high",
                "policy_digest": _SHA,
            },
            "policy": {
                "coverage_floor_by_tier": {
                    "low": 0.7,
                    "medium": 0.8,
                    "high": 0.9,
                    "critical": 1.0,
                }
            },
            "trace_ref": trace.model_dump(mode="json"),
            "gaps_ref": gaps.model_dump(mode="json"),
            "metrics_ref": metrics.model_dump(mode="json"),
            "sufficiency_ref": sufficiency.model_dump(mode="json"),
            "execution_ref": execution.evidence_ref.model_dump(mode="json"),
            "observations_ref": observations.model_dump(mode="json"),
            "obligation_assessment_ref": obligation.model_dump(mode="json"),
            "obligation_gate_facts": {
                "required_count": 1,
                "supported_count": 1,
                "refuted_count": 0,
                "inconclusive_count": 0,
                "repairable_gap_count": 0,
                "human_gap_count": 0,
            },
            "issue_evidence_manifest_ref": issue_manifest.model_dump(mode="json"),
            "owned_evidence_ids": ["OBS-DEMO-001"],
            "evidence_bundle_digest": f"sha256:{_SHA}",
            "healing_ref": None,
            "issue_ref": None,
        },
        "owned_evidence_ids": ["OBS-DEMO-001"],
        "evidence_bundle_digest": f"sha256:{_SHA}",
        "observations_ref": observations.model_dump(mode="json"),
        "issue_evidence_manifest_ref": issue_manifest.model_dump(mode="json"),
        "coverage_state": coverage_state or "",
        "status": disposition,
    }


def _report(epoch: int = 0, *, repair_round: int = 0) -> dict[str, object]:
    inspection = InspectionOutcomeV1.model_validate(
        _inspection(epoch, repair_round=repair_round)["inspection_outcome"]
    )
    ref = _ref(f"qa/results/report/epochs/{epoch}/report.json")
    receipt = _receipt(f"report-{epoch}")
    outcome = ReportOutcomeV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        batch_id=inspection.batch_id,
        inspection_receipt=inspection.inspection_receipt,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        report_refs=(ref,),
        report_receipt=receipt,
    )
    return {
        "report_outcome": outcome.model_dump(mode="json"),
        "report_refs": [ref.model_dump(mode="json")],
        "report_receipt": receipt.model_dump(mode="json"),
        "status": "reported",
    }


def _diagnostic_report(epoch: int = 0) -> dict[str, object]:
    ref = _ref(f"qa/results/report/epochs/{epoch}/report.json")
    receipt = _receipt(f"diagnostic-report-{epoch}")
    return {
        "report_outcome": {},
        "report_refs": [ref.model_dump(mode="json")],
        "report_receipt": receipt.model_dump(mode="json"),
        "report_purpose": "diagnostic",
        "status": "diagnostic",
    }


def _analysis_result(classification: str) -> dict[str, object]:
    inspection = cast(dict, _inspection()["inspection_outcome"])
    ref = {"path": "qa/results/inspect/issue-analysis.json", "digest": "a" * 64}
    return {
        "issue_analysis": {
            "agent_result": {
                "schema_version": "1.0",
                "change_id": "CH-DEMO-001",
                "batch_id": inspection["batch_id"],
                "evidence_bundle_digest": f"sha256:{'a' * 64}",
                "status": "completed",
                "candidate_count": 1,
                "candidates": [
                    {
                        "candidate_id": "CAND-1",
                        "observation_ids": ["OBS-DEMO-001"],
                        "proposed": {
                            "title": "Failure",
                            "classification": classification,
                            "severity": "high",
                            "root_cause_hypothesis": "Compare the assertion to the frozen contract",
                        },
                        "affected_surface": {"kind": "test", "value": "tests/a.py"},
                        "fingerprint_inputs": {"surface": "tests/a.py", "symptom": "assertion failed"},
                        "possible_problem_ids": [],
                        "confidence": 0.9,
                        "recommended_action": "investigate",
                    }
                ],
            },
            "candidate_digest": f"sha256:{'a' * 64}",
            "issue_analysis_ref": ref,
        },
        "evidence_refs": [ref],
    }


def _applied(epoch: int = 0, repair_round: int = 1) -> dict[str, object]:
    source = _ref(f"qa/results/generated/epochs/{epoch}/tests/test_case.py")
    result = AppliedTestRepairV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        repair_round=repair_round,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        status="applied",
        changed_test_refs=(source,),
        mapping_ref=_ref(f"qa/results/generation/epochs/{epoch}/mapping.json"),
        receipt=_receipt(f"repair-{epoch}-{repair_round}"),
    )
    dumped = result.model_dump(mode="json")
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": epoch,
        "repair_result": dumped,
        "applied_change_id": dumped["change_id"],
        "applied_plan_digest": dumped["plan_digest"],
        "applied_plan_ref": dumped["plan_ref"],
        "applied_coverage_epoch": dumped["coverage_epoch"],
        "applied_repair_round": dumped["repair_round"],
        "changed_test_refs": dumped["changed_test_refs"],
        "applied_mapping_ref": dumped["mapping_ref"],
        "apply_receipt": dumped["receipt"],
        "rounds_used": repair_round,
        "healing_rounds_used": repair_round,
        "status": "applied",
    }


def _build_context(checkpointer: Any = None) -> EngineGraphBuildContext:
    return EngineGraphBuildContext(contracts={}, checkpointer=checkpointer, approved_source_roots=())


def _public_input(entrypoint: str, **overrides: object) -> dict[str, object]:
    case_delta = (_CASE_DELTA,) if entrypoint in {"intake", "full"} else ()
    families = ("api",) if entrypoint in {"full", "intake"} else ()
    values: dict[str, object] = {
        "case_delta_paths": case_delta,
        "candidate_test_families": families,
    }
    values.update(overrides)
    payload = valid_product_input(
        **values,
    )
    dumped = ProductInputV1.model_validate(payload).model_dump(mode="json")
    if entrypoint == "full":
        dumped["family_policy"] = {"required": ["api"], "allowed": ["api"]}
    return dumped


def _product_graphs(features: Mapping[str, object] | None = None) -> ProductGraphs:
    return build_product_graphs(context=_build_context(), features=features or _real_features())


def test_build_product_graphs_merges_six_thin_roots_plus_full() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert isinstance(graphs, ProductGraphs)
    assert set(graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    assert len(graphs.entrypoints) == 7
    assert graphs.contracts == entrypoint_contracts()
    assert set(graphs.entrypoints) - {"full"} == set(
        build_thin_entrypoint_graphs(context=_build_context(), features=_real_features()).entrypoints
    )


def test_build_product_graphs_rejects_missing_duplicate_and_extra_before_return() -> None:
    from assurance_product.graphs.factory import _closed_entrypoints

    thin = build_thin_entrypoint_graphs(context=_build_context(), features=_real_features())
    placeholder = next(iter(thin.entrypoints.values()))
    closed = {**dict(thin.entrypoints), "full": placeholder}
    with pytest.raises(ValueError, match="missing"):
        _closed_entrypoints({name: graph for name, graph in closed.items() if name != "intake"})
    with pytest.raises(ValueError, match="extra"):
        _closed_entrypoints({**closed, "rogue": placeholder})

    class _Duplicate(Mapping[str, Any]):
        def __init__(self, items: tuple[tuple[str, Any], ...]) -> None:
            self._items = items

        def __getitem__(self, key: str) -> Any:
            return next(value for name, value in self._items if name == key)

        def __iter__(self) -> Iterator[str]:
            return (name for name, _ in self._items)

        def __len__(self) -> int:
            return len(self._items)

    with pytest.raises(ValueError, match="duplicate"):
        _closed_entrypoints(_Duplicate(tuple(closed.items()) + (("intake", placeholder),)))


def test_full_composes_generation_execution_inspect_and_report() -> None:
    import asyncio

    from tests.product.test_full_flow import _case, _front, _invoke, _reported, _tail_until_inspect

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        _reported(script)
        done = await _invoke(script)
        assert done.outcome == "achieved"
        assert done.result["terminal"] == {"status": "completed", "reason": "achieved"}  # type: ignore[index]
        names = [name for name, _item in done.captured]
        assert "generation.publish-cycle" in names
        assert "execution.execute" in names
        assert "quality.inspect" in names
        assert "quality.report" in names

    asyncio.run(run())


def test_repairable_inspection_requires_applied_repair_before_rerun() -> None:
    from tests.product.test_issue_healing_flow import test_applied_test_repair_is_the_only_path_to_rerun

    test_applied_test_repair_is_the_only_path_to_rerun()


def test_proposal_only_does_not_enter_rerun() -> None:
    from tests.product.test_issue_healing_flow import test_fix_proposal_output_cannot_parse_as_applied_repair

    test_fix_proposal_output_cannot_parse_as_applied_repair()


def test_full_reuses_case_subgraph_for_coverage_reentry() -> None:
    from tests.product.test_coverage_loop import test_coverage_insufficient_reenters_the_shared_case_flow

    test_coverage_insufficient_reenters_the_shared_case_flow()


def test_full_init_failure_does_not_enter_case() -> None:
    import asyncio

    from tests.product.test_full_flow import _failure, _front, _invoke

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        script["generation.init-test-runtime"] = [_failure()]
        done = await _invoke(script)
        names = [name for name, _item in done.captured]
        assert done.outcome == "not_achieved"
        assert "intake.case-design" not in names

    asyncio.run(run())


def test_full_retains_case_and_generation_history_across_nested_graphs() -> None:
    import asyncio

    from graph_engine.flow.declare import SubflowNode
    from graph_engine.flow.sources import LedgerRefs

    from assurance_intake.ops.case_review import op as case_review
    from assurance_product.graphs.execute_tail import build_execute_tail_flow
    from tests.product.test_execute_tail_flow import _bundles, _features
    from tests.product.test_full_flow import _case, _front, _invoke, _reported, _tail_until_inspect
    from graph_engine.testing.graph_harness import GraphHarness

    flow = build_execute_tail_flow(_bundles(_features(GraphHarness())))
    retro = next(node for node in flow.nodes if getattr(node, "name", None) == "retro")
    assert isinstance(retro, SubflowNode)
    history = retro.inputs["history_refs"]
    assert isinstance(history, LedgerRefs)
    assert history.key == case_review.artifact("history").ledger_key
    assert history.many is True

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        _reported(script)
        done = await _invoke(script)
        names = [name for name, _item in done.captured]
        assert "intake.case-review" in names
        assert "generation.publish-cycle" in names

    asyncio.run(run())


def test_full_does_not_invoke_optional_post_report_work() -> None:
    import asyncio

    from tests.product.test_full_flow import _case, _front, _invoke, _reported, _tail_until_inspect

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        _reported(script)
        done = await _invoke(script)
        names = [name for name, _item in done.captured]
        assert done.outcome == "achieved"
        assert "improvement.retro" not in names
        assert "improvement.apply" not in names

    asyncio.run(run())


def test_full_case_rejection_does_not_enter_generation() -> None:
    import asyncio

    from tests.product.test_full_flow import _case, _front, _invoke

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script, "reject")
        done = await _invoke(script)
        names = [name for name, _item in done.captured]
        assert done.outcome == "not_achieved"
        assert "generation.publish-cycle" not in names
        assert "generation.resolve-inputs" not in names

    asyncio.run(run())


def test_failed_report_never_enters_retro_or_achieved() -> None:
    import asyncio

    from tests.product.test_full_flow import _case, _failure, _front, _invoke, _tail_until_inspect

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        script["quality.report"] = [_failure()]
        done = await _invoke(script)
        names = [name for name, _item in done.captured]
        assert done.outcome == "not_achieved"
        assert "improvement.retro" not in names
        assert done.result["terminal"] == {"status": "failed", "reason": "not_achieved"}  # type: ignore[index]

    asyncio.run(run())


def test_full_preserves_internal_execute_tail_without_standalone_entrypoints() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert {"case", "execute"}.isdisjoint(graphs.entrypoints)
    nodes = set(graphs.entrypoints["full"].nodes)
    assert {"surface", "prepare", "init", "case", "tail", "coverage-rework"} <= nodes
    assert nodes.isdisjoint(
        {
            "adapt-init",
            "adapt-prepare",
            "advance-coverage",
            "execute-tail",
            "coverage-repair",
            "coverage-repair-brief",
            "quality-recheck",
            "coverage-needed",
        }
    )


def test_dry_and_runtime_product_roots_share_nodes_and_attach_saver_only_at_runtime() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    features = _real_features()
    dry = build_product_graphs(context=_build_context(None), features=features)
    runtime = build_product_graphs(context=_build_context(InMemorySaver()), features=features)
    assert set(dry.entrypoints) == set(runtime.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    assert set(dry.contracts) == set(runtime.contracts) == set(PRODUCT_ENTRYPOINTS)
    assert len(dry.entrypoints) == 7
    contracts = entrypoint_contracts()
    for name in PRODUCT_ENTRYPOINTS:
        assert set(dry.entrypoints[name].nodes) == set(runtime.entrypoints[name].nodes)
        assert {(edge.source, edge.target) for edge in dry.entrypoints[name].get_graph().edges} == {
            (edge.source, edge.target) for edge in runtime.entrypoints[name].get_graph().edges
        }
        assert dry.entrypoints[name].checkpointer is None
        assert runtime.entrypoints[name].checkpointer is not None
        assert contracts[name].recursion_limit == ENTRYPOINT_RECURSION_LIMITS[name]


def test_product_owned_inventory_has_no_pending_rows() -> None:
    product_rows = [row for row in EXCLUSIVE_ROUTE_INVENTORY if row.owner == "product"]
    assert len(product_rows) == 5
    assert all("pending" not in row.target_test for row in product_rows)
