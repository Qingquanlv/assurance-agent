"""Round identity lives on the op input, so a later call cannot reuse an earlier attempt."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from graph_engine.attempts.models.keys import BusinessActivation, derive_attempt_key
from graph_engine.flow.activation import activation_value
from graph_engine.testing import GraphHarness, committed

from assurance_quality.contracts.agent import QualitySkillInputV1
from assurance_quality.contracts.assessment import FactBaselineSkillInputV1, ReportSkillInputV1
from assurance_quality.contracts.issues import ReconcileIssuesInputV1
from assurance_quality.graphs.factory import build_quality_graphs as _build_quality_graphs
from test_quality_graph_factory import (  # pyright: ignore[reportMissingImports]
    _fact_baseline_output,
    _finalized_issue_analysis_output,
    _routed_issue_analysis,
    _receipt,
    _reconcile_output,
    assess_graph_input,
    quality_contracts,
    quality_graph_input,
)
from test_report_outcome import _bound_report, _skill_from_state, _state  # pyright: ignore[reportMissingImports]

from graph_engine.testing.feature_bundle import compile_bundle


def build_quality_graphs(*args, **kwargs):
    return compile_bundle(_build_quality_graphs(*args, **kwargs))


def _key(model: object, *, step: str, contract_id: str):
    from pydantic import BaseModel

    assert isinstance(model, BaseModel)
    return derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="rev-1",
        public_entrypoint="assurance.quality",
        semantic_node_id=f"quality.{step}",
        business_activation=BusinessActivation.for_trigger(activation_value((), step, ())),
        contract_id=contract_id,
        validated_input=model,
    )


async def _digest(graph_name: str, semantic_id: str, payload: dict[str, object], output: object) -> str:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    graph = getattr(build_quality_graphs(context), graph_name)
    result = await harness.run(graph, input=payload, script={semantic_id: [committed(output, _receipt())]})
    assert result.semantic_calls
    return result.semantic_calls[0].input_digest


def _fact_payload(epoch: int) -> dict[str, object]:
    payload = assess_graph_input()
    payload["coverage_epoch"] = epoch
    reviewed = payload["reviewed_case"]
    assert isinstance(reviewed, dict)
    reviewed["coverage_epoch"] = epoch
    selection = reviewed["selection_ref"]
    assert isinstance(selection, dict)
    reviewed["selection_ref"] = {
        **selection,
        "path": f"qa/results/cases/epochs/{epoch}/selection.json",
    }
    return payload


@pytest.mark.asyncio
async def test_fact_baseline_epochs_do_not_share_an_attempt() -> None:
    first = await _digest("fact_baseline", "quality.fact-baseline", _fact_payload(1), _fact_baseline_output())
    second = await _digest(
        "fact_baseline", "quality.fact-baseline", _fact_payload(2), _fact_baseline_output()
    )
    assert first != second

    def skill(epoch: int) -> FactBaselineSkillInputV1:
        raw = _fact_payload(epoch)
        raw["artifact_paths"] = raw["allowed_artifact_paths"]
        return FactBaselineSkillInputV1.model_validate(
            {name: raw[name] for name in FactBaselineSkillInputV1.model_fields if name in raw}
        )

    left = skill(1)
    right = skill(2)
    assert _key(left, step="fact-baseline", contract_id="assurance.quality.agent.fact-baseline.v1") != _key(
        right, step="fact-baseline", contract_id="assurance.quality.agent.fact-baseline.v1"
    )


@pytest.mark.asyncio
async def test_issue_analysis_identity_does_not_share_an_attempt() -> None:
    output = _routed_issue_analysis()
    base = quality_graph_input()
    other = quality_graph_input()
    other["batch_id"] = "20260908T010000Z"
    changed = quality_graph_input()
    changed["evidence_bundle_digest"] = "sha256:" + "b" * 64
    first = await _digest("issue_analyze", "quality.issue-analyze", base, output)
    assert first != await _digest("issue_analyze", "quality.issue-analyze", other, output)
    assert first != await _digest("issue_analyze", "quality.issue-analyze", changed, output)
    model = QualitySkillInputV1.model_validate(
        {name: base[name] for name in QualitySkillInputV1.model_fields if name in base}
    )
    assert _key(model, step="issue-analyze", contract_id="assurance.quality.agent.issue-analysis.v1") != _key(
        model.model_copy(update={"batch_id": "20260908T010000Z"}),
        step="issue-analyze",
        contract_id="assurance.quality.agent.issue-analysis.v1",
    )
    assert _key(model, step="issue-analyze", contract_id="assurance.quality.agent.issue-analysis.v1") != _key(
        model.model_copy(update={"change_id": "CH-DEMO-002"}),
        step="issue-analyze",
        contract_id="assurance.quality.agent.issue-analysis.v1",
    )


@pytest.mark.asyncio
async def test_issue_reconcile_identity_does_not_share_an_attempt() -> None:
    output = _reconcile_output()
    output["classification"] = "test"
    output["fix_eligible"] = True
    output["evidence_refs"] = [output["issue_snapshot_ref"]]
    base = quality_graph_input()
    base["issue_analysis"] = _finalized_issue_analysis_output()
    other = dict(base)
    other["batch_id"] = "20260908T010000Z"
    first = await _digest("issue_reconcile", "quality.issue-reconcile", base, output)
    second = await _digest("issue_reconcile", "quality.issue-reconcile", other, output)
    assert first != second
    model = ReconcileIssuesInputV1.model_validate(
        {
            "change_id": base["change_id"],
            "batch_id": base["batch_id"],
            "evidence_bundle_digest": base["evidence_bundle_digest"],
        }
    )
    assert _key(model, step="issue-reconcile", contract_id="assurance.quality.reconcile-issues") != _key(
        model.model_copy(update={"evidence_bundle_digest": "sha256:" + "c" * 64}),
        step="issue-reconcile",
        contract_id="assurance.quality.reconcile-issues",
    )


def _with_coverage_epoch(state: dict[str, object], epoch: int) -> dict[str, object]:
    import copy

    cloned = copy.deepcopy(state)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if "coverage_epoch" in value:
                value["coverage_epoch"] = epoch
            selection = value.get("selection_ref")
            if isinstance(selection, dict):
                selection["path"] = f"qa/results/cases/epochs/{epoch}/selection.json"
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(cloned)
    cloned["coverage_epoch"] = epoch
    return cloned


@pytest.mark.asyncio
async def test_report_epoch_and_purpose_do_not_share_an_attempt(tmp_path: Path) -> None:
    normal_state = _state()
    normal = _skill_from_state(normal_state)
    diagnostic_state = _state()
    inspection = cast(dict[str, object], diagnostic_state["inspection_outcome"])
    inspection["disposition"] = "blocked"
    inspection["coverage_state"] = None
    diagnostic_state["report_purpose"] = "diagnostic"
    diagnostic_state["issue_analysis_ref"] = {
        "path": "qa/results/inspect/issue-analysis.json",
        "digest": "a" * 64,
    }
    diagnostic = _skill_from_state(diagnostic_state)
    later = _skill_from_state(_state(batch_id="batch-2"))
    published = {
        "publication": "reported",
        "report_refs": [{"path": "qa/results/report/report.md", "digest": "a" * 64}],
        "coverage_state": "satisfied",
        "report_outcome": None,
    }
    first = await _digest(
        "report",
        "quality.report",
        _bound_report(tmp_path / "normal", cast(ReportSkillInputV1, _skill_from_state(normal_state))),
        published,
    )
    diagnostic_output = {**published, "publication": "diagnostic", "coverage_state": None}
    assert first != await _digest(
        "report",
        "quality.report",
        _bound_report(tmp_path / "diagnostic", cast(ReportSkillInputV1, _skill_from_state(diagnostic_state))),
        diagnostic_output,
    )
    assert isinstance(normal, ReportSkillInputV1)
    assert _key(normal, step="report", contract_id="assurance.quality.agent.report.v1") != _key(
        later, step="report", contract_id="assurance.quality.agent.report.v1"
    )
    assert _key(normal, step="report", contract_id="assurance.quality.agent.report.v1") != _key(
        diagnostic, step="report", contract_id="assurance.quality.agent.report.v1"
    )
    next_epoch_state = _with_coverage_epoch(_state(), 1)
    next_epoch = _skill_from_state(next_epoch_state)
    epoch_output = published
    assert first != await _digest(
        "report",
        "quality.report",
        _bound_report(tmp_path / "epoch", cast(ReportSkillInputV1, _skill_from_state(next_epoch_state))),
        epoch_output,
    )
    assert _key(normal, step="report", contract_id="assurance.quality.agent.report.v1") != _key(
        next_epoch, step="report", contract_id="assurance.quality.agent.report.v1"
    )


def _assess_payload(*, epoch: int, repair_round: int) -> dict[str, object]:
    payload = _with_coverage_epoch(assess_graph_input(), epoch)
    payload["repair_round"] = repair_round
    execution = payload["execution_result"]
    assert isinstance(execution, dict)
    execution["repair_round"] = repair_round
    execution["coverage_epoch"] = epoch
    return payload


async def _assess_inspect_digest(*, epoch: int, repair_round: int) -> str:
    from assurance_quality.contracts.assessment import ASSESSMENT_INPUTS_PATH

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    published = {
        "disposition": "satisfied",
        "coverage_state": "satisfied",
        "inspection_outcome": {},
        "assessment": {},
        "evidence_refs": [],
        "observations_ref": {},
        "issue_evidence_manifest_ref": {},
        "owned_evidence_ids": [],
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "finalized": {},
    }
    result = await harness.run(
        build_quality_graphs(context).assess,
        input=_assess_payload(epoch=epoch, repair_round=repair_round),
        script={
            "quality.materialize-assessment-inputs": [
                committed(
                    {"change_id": "CH-DEMO-001"},
                    _receipt(),
                    artifacts=[{"path": ASSESSMENT_INPUTS_PATH, "digest": "c" * 64}],
                )
            ],
            "quality.inspect": [committed(published, _receipt())],
        },
    )
    inspect = next(call for call in result.semantic_calls if call.semantic_node_id == "quality.inspect")
    return inspect.input_digest


@pytest.mark.asyncio
async def test_assess_epoch_and_repair_round_do_not_share_an_attempt() -> None:
    first = await _assess_inspect_digest(epoch=1, repair_round=0)
    assert first != await _assess_inspect_digest(epoch=1, repair_round=1)
    assert first != await _assess_inspect_digest(epoch=2, repair_round=0)
