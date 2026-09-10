from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_quality.graphs.factory import QualityGraphs
from assurance_quality.graphs.nodes import select_quality
from assurance_quality.graphs.state import QualityState

from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_product.graphs.execute import adapt_quality_assess
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.routes import route_execute, route_run
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from tests.product.test_product_stategraph_flow import (
    _analysis_result,
    _diagnostic_report,
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)
from tests.verified_generation_fixture import accepted_verified_execution_input


def _analysis_required() -> dict[str, object]:
    return _inspection(disposition="analysis_required")


def test_assertion_analysis_product_bug_reaches_diagnostic_not_repair() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_analysis_required(),
                issue_analyze=_analysis_result("product_bug"),
                report=_diagnostic_report(),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "diagnostic"
    assert result["terminal"]["reason"] == "not_achieved"
    assert not result.get("repair_result")


def test_assertion_analysis_test_bug_reaches_repair_rerun_and_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=(_analysis_required(), _inspection()),
                issue_analyze=_analysis_result("test_bug"),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"
    assert result["repair_result"]["status"] == "applied"
    assert result["execution_result"]["repair_round"] == 1


@pytest.mark.parametrize(
    ("disposition", "kind"),
    [
        ("analysis_required", "unknown"),
        ("analysis_required", "pending"),
        ("analysis_required", "exhausted"),
        ("blocked", "unknown"),
        ("blocked", "pending"),
    ],
)
def test_analysis_requires_human_only_after_analysis_or_budget_exhaustion(
    kind: str, disposition: str
) -> None:
    analysis = _analysis_result("test_bug" if kind == "exhausted" else "unknown")
    if kind == "pending":
        cast(dict, analysis["issue_analysis"])["candidate_digest"] = None
        cast(dict, analysis["issue_analysis"])["agent_result"].update(
            status="pending", candidate_count=0, candidates=[]
        )
    payload = _public_input("execute")
    if kind == "exhausted":
        cast(dict, payload["budgets"])["healing_rounds"] = 0
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition=disposition),
                issue_analyze=analysis,
            )
        ),
        "execute",
        payload,
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert tail.issue_analysis_ref is not None
    assert not result.get("repair_result")


@pytest.mark.parametrize("invalid", ["empty", "uncovered", "foreign", "stale", "failed"])
def test_invalid_issue_analysis_never_authorizes_repair_or_report(invalid: str) -> None:
    assessment = _analysis_required()
    analysis = _analysis_result("test_bug")
    result_data = cast(dict, analysis["issue_analysis"])["agent_result"]
    if invalid == "empty":
        result_data.update(candidate_count=0, candidates=[])
    elif invalid == "uncovered":
        cast(dict, assessment["assessment_inputs"])["owned_evidence_ids"].append("OBS-DEMO-002")
        cast(list, assessment["owned_evidence_ids"]).append("OBS-DEMO-002")
    elif invalid == "foreign":
        result_data["candidates"][0]["observation_ids"] = ["OBS-OTHER"]
    elif invalid == "stale":
        result_data["batch_id"] = "previous-batch"
    else:
        result_data["status"] = "failed"
        cast(dict, analysis["issue_analysis"])["candidate_digest"] = None
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=assessment,
                issue_analyze=analysis,
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert not result.get("repair_result")
    assert not result.get("report_refs")


@pytest.mark.parametrize(
    ("second_kind", "expected_status"),
    [
        ("product_bug", "diagnostic"),
        ("environment_issue", "diagnostic"),
        ("unknown", "needs_human"),
    ],
)
def test_mixed_analysis_does_not_authorize_whole_batch_test_repair(
    second_kind: str,
    expected_status: str,
) -> None:
    assessment = _analysis_required()
    cast(dict, assessment["assessment_inputs"])["owned_evidence_ids"].append("OBS-DEMO-002")
    cast(list, assessment["owned_evidence_ids"]).append("OBS-DEMO-002")
    analysis = _analysis_result("test_bug")
    result_data = cast(dict, analysis["issue_analysis"])["agent_result"]
    other = cast(dict, _analysis_result(second_kind)["issue_analysis"])["agent_result"]["candidates"][0]
    other.update(candidate_id="CAND-2", observation_ids=["OBS-DEMO-002"])
    other["proposed"]["severity"] = "low"
    result_data["candidates"].append(other)
    result_data["candidate_count"] = 2
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=assessment,
                issue_analyze=analysis,
                report=_diagnostic_report(),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == expected_status
    assert not result.get("repair_result")


def test_pending_analysis_cannot_publish_a_reference_that_was_not_committed() -> None:
    analysis = _analysis_result("unknown")
    finalized = cast(dict, analysis["issue_analysis"])
    finalized["candidate_digest"] = None
    finalized["agent_result"].update(status="pending", candidate_count=0, candidates=[])
    analysis["evidence_refs"] = []
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_analysis_required(),
                issue_analyze=analysis,
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    result = invoke_product_root(_product_graphs(), "execute", _public_input("execute"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert tail.inspection is not None
    assert tail.report is not None


def test_coverage_insufficient_returns_without_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="coverage_insufficient"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "coverage_insufficient"
    assert result["terminal"] == {"status": "stopped", "reason": "coverage_insufficient"}
    assert tail.report is None
    assert tail.report_refs == ()


def test_needs_human_returns_without_test_repair_or_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="needs_human"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert result["terminal"] == {"status": "stopped", "reason": "needs_human"}
    assert tail.report is None
    assert "repair_result" not in result


def test_invalid_execution_result_blocks_before_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(execute={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "blocked"
    assert "inspection_outcome" not in result
    assert result.get("execution_result") in (None, {})


def _verified_state(root: Path, completion: str = "collected") -> dict[str, Any]:
    prepared = accepted_verified_execution_input(root, change_id="CH-DEMO-001")
    generation_model = prepared.generation_result
    assert generation_model is not None
    machine_ref = generation_model.case_execution_plan_ref
    assert machine_ref is not None
    execution_id = "12345678-1234-4123-8123-123456789abc"
    process_ref = generation_model.mapping_ref.model_copy(
        update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/process_terminal.json"}
    )
    cycle = VerifiedExecutionCycleResultV1(
        validation_profile="api_db.v1",
        change_id=prepared.change_id,
        case_id="TC_USER_CREATE_001",
        reviewed_case=generation_model.reviewed_case,
        coverage_epoch=prepared.coverage_epoch,
        repair_round=0,
        plan_digest=prepared.plan_digest,
        plan_ref=prepared.plan_ref,
        case_execution_plan_ref=machine_ref,
        case_execution_plan_digest=machine_ref.digest,
        spec_digest=generation_model.reviewed_case.preparation_refs[0].digest,
        execution_id=execution_id,
        attempt_key=AttemptKey(digest="4" * 64),
        batch_id="verified-batch",
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        completion_status=cast(Any, completion),
        mapping_ref=generation_model.mapping_ref,
        mapping_digest=generation_model.mapping_ref.digest,
        manifest_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/manifest.json"}
        ),
        evidence_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/outcome.json"}
        ),
        execution_index_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/execute-result.json"}
        ),
        execution_authority_ref=generation_model.mapping_ref.model_copy(
            update={
                "path": (f"qa/changes/{prepared.change_id}/execution/{execution_id}/execution_terminal.json")
            }
        ),
        raw_evidence_refs=(process_ref,),
        source_refs=generation_model.source_refs,
        receipt=ReceiptRef(receipt_id="verified-execution", receipt_digest="f" * 64),
    )
    # adapt_quality only needs coherent generation/cycle identities; use the
    # cycle's ReviewedCase and machine-plan bindings in the generation DTO.
    generation = {
        "change_id": cycle.change_id,
        "coverage_epoch": cycle.coverage_epoch,
        "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        "plan_digest": cycle.plan_digest,
        "plan_ref": cycle.plan_ref.model_dump(mode="json"),
        "mapping_ref": cycle.mapping_ref.model_dump(mode="json"),
        "source_refs": [item.model_dump(mode="json") for item in cycle.source_refs],
        "plan_refs": [cycle.case_execution_plan_ref.model_dump(mode="json")],
        "case_execution_plan_ref": cycle.case_execution_plan_ref.model_dump(mode="json"),
        "case_execution_plan_digest": cycle.case_execution_plan_digest,
    }
    state = cast(dict[str, Any], _public_input("full"))
    state.update(
        {
            "change_id": cycle.change_id,
            "coverage_epoch": cycle.coverage_epoch,
            "validation_profile": cycle.validation_profile,
            "generation_result": generation,
            "execution_result": cycle.model_dump(mode="json"),
            "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        }
    )
    return state


def test_collected_and_incomplete_verified_cycles_both_reach_quality(tmp_path: Path) -> None:
    for completion in ("collected", "incomplete"):
        state = _verified_state(tmp_path / completion, completion)
        assert route_execute(state) == "quality"
        assert route_run(state) == "quality"
        adapted = adapt_quality_assess(cast(Any, state))
        assert adapted["execution_status"] == completion
        feature_input = cast(dict[str, Any], adapted["feature_input"])
        execution_result = cast(dict[str, Any], feature_input["execution_result"])
        assert execution_result["completion_status"] == completion


@pytest.mark.parametrize("inspection", [None, {}, _inspection(disposition="blocked")["inspection_outcome"]])
def test_failed_inspect_attempt_stops_without_diagnostic_report(inspection) -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess={
                    "attempt_failure": {"kind": "invalid_output", "message": "Inspect failed"},
                    "inspection_outcome": inspection,
                }
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert result["attempt_failure"]["kind"] == "invalid_output"
    assert not result.get("report_refs")
    assert result["terminal"] == {"status": "failed", "reason": "blocked"}


@pytest.mark.parametrize("entrypoint", ["execute", "full"])
@pytest.mark.parametrize("disposition", ["blocked", "analysis_required"])
def test_blocking_inspection_publishes_diagnostic_report_without_achievement(
    entrypoint: str, disposition: str
) -> None:
    from types import SimpleNamespace

    from assurance_execution.graphs.nodes import publish_execution
    from assurance_product.status import _execution_gate_from_snapshot
    from tests.product.test_achieved_terminal import _execution_evidence
    from tests.product.test_product_stategraph_flow import _execution

    issue_inputs: list[dict[str, object]] = []
    report_inputs: list[dict[str, object]] = []
    execution = _execution(status="FAIL")
    cycle = cast(dict, execution["execution_result"])
    execution.update(
        publish_execution(
            {"rounds_budget": 2, "rounds_used": 0},
            _execution_evidence(
                batch_id=cycle["batch_id"],
                status="failed",
                plan_digest=cycle["plan_digest"],
                plan_ref=cycle["plan_ref"],
            ),
            None,
        )
    )

    def recording_graph(seen: list[dict[str, object]], update: Mapping[str, object]) -> Any:
        builder = StateGraph(QualityState)

        def record(state: Mapping[str, object]) -> dict[str, object]:
            if seen is issue_inputs:
                business = select_quality(state)
                assert business.trace_digest == "a" * 64
                assert business.coverage_digest == "a" * 64
                assert business.metrics_digest == "a" * 64
                assert business.case_digest == "a" * 64
                assert business.mapping_digest == "a" * 64
                assert business.execution_digest == "a" * 64
            seen.append(dict(state))
            return dict(update)

        builder.add_node("record", cast(Any, record))
        builder.add_edge(START, "record")
        builder.add_edge("record", END)
        return builder.compile()

    issue_ref = {
        "path": "qa/changes/CH-DEMO-001/inspect/issue-analysis.json",
        "digest": "a" * 64,
    }
    features = _flow_features(
        execute=execution,
        assess=_inspection(disposition=disposition),
        report=_diagnostic_report(),
    )
    quality = cast(QualityGraphs, features["assurance.quality"])
    features["assurance.quality"] = QualityGraphs(
        assess=quality.assess,
        issue_review=quality.issue_review,
        issue_analyze=recording_graph(
            issue_inputs,
            {
                **_analysis_result("product_bug"),
                "classification": "product_bug",
                "fix_eligible": False,
                "evidence_refs": [issue_ref],
                "status": "passed",
            },
        ),
        issue_reconcile=quality.issue_reconcile,
        report=recording_graph(report_inputs, _diagnostic_report()),
    )
    result = invoke_product_root(
        _product_graphs(features),
        entrypoint,
        _public_input(entrypoint),
    )

    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "diagnostic"
    assert tail.inspection is not None
    assert tail.inspection.disposition == disposition
    assert tail.report is None
    assert tail.report_refs
    assert tail.report_receipt is not None
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert len(issue_inputs) == 1
    assert issue_inputs[0]["owned_evidence_ids"] == ["OBS-DEMO-001"]
    assert issue_inputs[0]["evidence_bundle_digest"] == f"sha256:{'a' * 64}"
    assert len(report_inputs) == 1
    assert report_inputs[0]["issue_analysis_ref"] == issue_ref
    gate = _execution_gate_from_snapshot(SimpleNamespace(values=result))
    assert gate is not None
    assert gate.execution_digest == execution["execution_digest"]


def _profiled_assessment_composition(installed_sources, tmp_path, profile: str):
    import sys
    import yaml
    from assurance_product.binding_builder import build_deployment_wheel
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition
    from graph_engine.composition import WheelPluginSource
    from tests.product.composition_harness import _extract_wheel

    document = yaml.safe_load(Path("tests/product/fixtures/deployment/opencode.yaml").read_text())
    document["validation_profile"] = profile
    document["verification_host"] = {"managed_sut_authority_handle": "sut.authority"}
    manifest = tmp_path / "deployment.yaml"
    manifest.write_text(yaml.safe_dump(document))
    wheel = build_deployment_wheel(manifest, tmp_path / "wheel")
    extracted = _extract_wheel(wheel.wheel, tmp_path / "installed")
    try:
        yield resolve_assurance_composition(
            AssuranceCompositionRequest(
                product_entrypoint="assurance-opencode",
                deployment_source=WheelPluginSource(
                    distribution=wheel.distribution,
                    entrypoint_name="deployment",
                    declaration_path=wheel.declaration_path,
                ),
                configuration_tree=installed_sources.configuration_tree,
            )
        )
    finally:
        sys.path.remove(str(extracted))
        for name in tuple(sys.modules):
            if name == wheel.import_package or name.startswith(wheel.import_package + "."):
                sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def assessment_composition(installed_sources, tmp_path_factory):
    yield from _profiled_assessment_composition(
        installed_sources, tmp_path_factory.mktemp("assessment-composition"), "api_db.v1"
    )


@pytest.fixture(scope="module")
def trace_assessment_composition(installed_sources, tmp_path_factory):
    yield from _profiled_assessment_composition(
        installed_sources, tmp_path_factory.mktemp("trace-assessment-composition"), "api_db_trace.v1"
    )


def _bridge_assessment_request(project):
    from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1
    from assurance_generation.contracts.admission import diagnose_verified_bridge_defect
    from assurance_intake.contracts.plan import decode_plan
    from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1

    prepared = accepted_verified_execution_input(project)
    generation = prepared.generation_result
    assert generation is not None
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    (project / bridge.path).unlink()
    defect = diagnose_verified_bridge_defect(
        project,
        generation=generation,
        validation_profile="api_db.v1",
        selected_test_families=prepared.selected_test_families,
        capability_leafs=prepared.capability_leafs,
        attempt_key=AttemptKey(digest="4" * 64),
    )
    execution = VerifiedIncompleteExecutionV1(
        defect=defect,
        batch_id=defect.attempt_key.digest,
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        receipt=ReceiptRef(receipt_id="kernel", receipt_digest="9" * 64),
    )
    plan = decode_plan((project / prepared.plan_ref.path).read_bytes(), prepared.plan_ref)
    return MaterializeAssessmentInputV1(
        plan_digest=plan.plan_digest,
        plan_ref=prepared.plan_ref,
        reviewed_case=generation.reviewed_case,
        generation=generation,
        execution=execution,
        policy_resource_id="assurance.product.configuration.product-policy",
        policy_sha256=plan.policy_digest,
        execution_at=execution.executed_at,
    )


async def _installed_assessment(project, request, composition, authority, monkeypatch):
    import asyncio
    from graph_engine.attempts import AttemptExecutionContext, AuthorizedAttemptScope
    from graph_engine.attempts.activity import journal_backed_activity_factory
    from graph_engine.attempts.events import AttemptOpened, ResourcesAuthorized, ActivityPrepared
    from graph_engine.attempts.workspace import TaskWorkspaceStore
    from graph_engine.attempts.production_host import create_production_task_execution_host
    from graph_engine.attempts.host_receipts import TerminalReceiptStore
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )
    from graph_engine.canonical import canonical_digest
    from graph_engine.persistence.attempt_journal import MemoryAttemptJournal

    resolved = composition.semantic_attempt_contracts["assurance.quality.materialize-assessment-inputs"]
    handler_id = resolved.contract.handler_id
    key = AttemptKey(digest="a" * 64)
    store = TaskWorkspaceStore(project, project / ".attempts", project / ".receipts")
    workspace = store.begin(
        task_id=key.digest, attempt=1, output_paths=(f"qa/changes/{request.reviewed_case.change_id}/inspect",)
    )
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv",
            public_entrypoint="execute",
            semantic_node_id="quality.materialize-assessment-inputs",
            attempt_key=key,
            fencing_token=1,
            authorization_id="d" * 64,
        ),
        workspace=workspace,
    )
    journal = MemoryAttemptJournal()
    await journal.append(
        key,
        (
            AttemptOpened(
                contract_digest="e" * 64,
                input_digest=canonical_digest(request.model_dump(mode="json")),
                graph_revision="f" * 64,
                invocation_id="inv",
                public_entrypoint="execute",
                semantic_node_id="quality.materialize-assessment-inputs",
            ),
            ResourcesAuthorized(authorization_id="d" * 64),
            ActivityPrepared(activity_id=key.digest),
        ),
        expected_revision=0,
        fencing_token=1,
    )
    monkeypatch.setenv(
        "AA_ASSESSMENT_AUTHORITY",
        authority.resolve("sut.authority").decode() if authority else "unused-bridge-authority",
    )
    sources = (SecretSourceBinding("sut.authority", "environment", "AA_ASSESSMENT_AUTHORITY"),)
    auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )

    async def fence():
        pass

    activity_factory = journal_backed_activity_factory(
        journal=journal,
        attempt_key=key,
        owner_loop=asyncio.get_running_loop(),
        assert_live_fence=fence,
    )
    host = create_production_task_execution_host(
        authorization=auth,
        handlers={handler_id: composition.registries.capabilities.task_handlers[handler_id]},
        store=store,
        receipts=TerminalReceiptStore.create(project / ".host-receipts"),
        activity_factory=cast(Any, activity_factory),
        invocation_root=project,
    )
    executor = resolved.executor.with_host(
        host, graph_revision="f" * 64, product_lock_digest=composition.lock.digest
    )
    try:
        result = await executor.execute(request, scope)
        if hasattr(result, "output"):
            sealed = store.seal(workspace.identity)
            store.promote(workspace.identity, sealed)
        return result
    finally:
        store.close()


@pytest.mark.parametrize("scenario", ["bridge", "http_unknown", "environment", "wrong_business"])
def test_installed_assessment_reaches_inspect_with_verified_incomplete(
    tmp_path,
    assessment_composition,
    monkeypatch,
    scenario,
):
    import asyncio
    from assurance_quality.contracts.verification import VerificationVerdictV1
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    if scenario == "bridge":
        request = _bridge_assessment_request(project)
        authority = None
    else:
        request, authority, _ = _materialization_request(
            project,
            authenticated=True,
            http_unknown=scenario == "http_unknown",
            wrong_email=scenario == "wrong_business",
            process_reason="telemetry_unavailable" if scenario in {"environment", "wrong_business"} else None,
        )
    result = asyncio.run(
        _installed_assessment(project, request, assessment_composition, authority, monkeypatch)
    )
    assert hasattr(result, "output"), result
    assessment = result.output
    verdict = VerificationVerdictV1.model_validate_json(
        (project / assessment.verification_ref.path).read_bytes()
    )
    expected = (
        "repairable_execution_failure"
        if scenario == "bridge"
        else "needs_human"
        if scenario == "wrong_business"
        else "blocked"
    )
    inspection = _publish_verified_inspection(project, request, assessment, verdict)
    assert cast(dict, inspection["inspection_outcome"])["disposition"] == expected
    assert verdict.verdict == ("FAILED" if scenario == "wrong_business" else "INCOMPLETE")
    if scenario == "bridge":
        from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1

        assert isinstance(request.execution, VerifiedIncompleteExecutionV1)
        assert request.execution.defect.defect_kind == "missing_bridge"
        assert assessment.incomplete_execution == request.execution
        assert assessment.execution_ref is None
        assert verdict.execution_id is None
        assert verdict.executed == 0
        assert not list((project / "qa").rglob("manifest.json"))
        import json

        evidence_manifest = json.loads((project / assessment.issue_evidence_manifest_ref.path).read_bytes())
        assert request.execution.defect.bridge_ref.path not in {
            entry["path"] for entry in evidence_manifest["entries"]
        }


def test_installed_assessment_admits_sealed_user_trace(tmp_path, trace_assessment_composition, monkeypatch):
    import asyncio
    from assurance_quality.contracts.verification import VerificationVerdictV1
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    request, authority, _ = _materialization_request(project, authenticated=True, trace=True)
    result = asyncio.run(
        _installed_assessment(project, request, trace_assessment_composition, authority, monkeypatch)
    )
    assert hasattr(result, "output"), result
    assessment = result.output
    verdict = VerificationVerdictV1.model_validate_json(
        (project / assessment.verification_ref.path).read_bytes()
    )
    assert verdict.verdict == "PASSED"
    assert verdict.by_id("trace.http").evidence_status == "observed"
    inspection = _publish_verified_inspection(project, request, assessment, verdict)
    assert cast(dict, inspection["inspection_outcome"])["disposition"] == "satisfied"


def test_installed_assessment_rejects_missing_or_rewritten_trace(
    tmp_path, trace_assessment_composition, monkeypatch
):
    import asyncio
    from graph_engine.attempts import PermanentTaskFailure
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    request, authority, _ = _materialization_request(project, authenticated=True, trace=True)
    otlp = next(
        ref for ref in request.execution.raw_evidence_refs if ref.path.endswith("telemetry.otlp.jsonl")
    )
    path = project / otlp.path
    path.chmod(0o600)
    path.write_bytes(b"{}\n")
    result = asyncio.run(
        _installed_assessment(project, request, trace_assessment_composition, authority, monkeypatch)
    )
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_input"


def test_installed_assessment_missing_write_span_is_incomplete(
    tmp_path, trace_assessment_composition, monkeypatch
):
    import asyncio
    from assurance_quality.contracts.verification import VerificationVerdictV1
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    request, authority, _ = _materialization_request(
        project, authenticated=True, trace=True, drop_write_span=True
    )
    result = asyncio.run(
        _installed_assessment(project, request, trace_assessment_composition, authority, monkeypatch)
    )
    assert hasattr(result, "output"), result
    verdict = VerificationVerdictV1.model_validate_json(
        (project / result.output.verification_ref.path).read_bytes()
    )
    assert verdict.verdict == "INCOMPLETE"
    assert verdict.by_id("trace.user_write").evidence_status == "missing"


@pytest.mark.parametrize("kind", ["cycle", "defect"])
def test_installed_legacy_assessment_rejects_verified_inputs(tmp_path, opencode_composition, kind):
    import asyncio
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    request = (
        _bridge_assessment_request(project)
        if kind == "defect"
        else _materialization_request(project, authenticated=True)[0]
    )
    executor = opencode_composition.semantic_attempt_contracts[
        "assurance.quality.materialize-assessment-inputs"
    ].executor
    with pytest.raises(ValueError, match="legacy assessment cannot accept"):
        asyncio.run(executor.execute(request, None))


@pytest.mark.parametrize("kind", ["cycle", "defect"])
def test_installed_assessment_rejects_wrong_profile(tmp_path, assessment_composition, kind):
    import asyncio
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    request = (
        _bridge_assessment_request(project)
        if kind == "defect"
        else _materialization_request(project, authenticated=True)[0]
    )
    execution = request.execution
    if kind == "cycle":
        execution = execution.model_copy(update={"validation_profile": "api_db_trace.v1"})
    else:
        from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1

        assert isinstance(execution, VerifiedIncompleteExecutionV1)
        execution = execution.model_copy(
            update={"defect": execution.defect.model_copy(update={"validation_profile": "api_db_trace.v1"})}
        )
    request = request.model_copy(update={"execution": execution})
    executor = assessment_composition.semantic_attempt_contracts[
        "assurance.quality.materialize-assessment-inputs"
    ].executor
    with pytest.raises(ValueError, match="validation profile"):
        asyncio.run(executor.execute(request, None))


@pytest.mark.parametrize("tamper", ["stale_attempt", "forged_journal", "forged_defect"])
def test_installed_assessment_rejects_unauthenticated_execution(
    tmp_path, assessment_composition, monkeypatch, tamper
):
    import asyncio
    import json
    from graph_engine.attempts import PermanentTaskFailure
    from tests.verified_assessment_fixture import _materialization_request

    project = tmp_path / "project"
    project.mkdir()
    if tamper == "forged_defect":
        request = _bridge_assessment_request(project)
        from assurance_execution.contracts.workflow import VerifiedIncompleteExecutionV1

        execution = request.execution
        assert isinstance(execution, VerifiedIncompleteExecutionV1)
        execution = execution.model_copy(
            update={"defect": execution.defect.model_copy(update={"bridge_symbol": "test_forged"})}
        )
        request = request.model_copy(update={"execution": execution})
        authority = None
    else:
        request, authority, _ = _materialization_request(project, authenticated=True)
        if tamper == "stale_attempt":
            request = request.model_copy(
                update={
                    "execution": request.execution.model_copy(
                        update={"attempt_key": AttemptKey(digest="0" * 64)}
                    )
                }
            )
        else:
            from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1

            assert isinstance(request.execution, VerifiedExecutionCycleResultV1)
            ref = next(
                ref
                for ref in request.execution.raw_evidence_refs
                if ref.path.endswith("action_terminal.json")
            )
            path = project / ref.path
            data = json.loads(path.read_text())
            data["payload"]["http"]["status"] = 201
            path.chmod(0o600)
            path.write_text(json.dumps(data))
    result = asyncio.run(
        _installed_assessment(project, request, assessment_composition, authority, monkeypatch)
    )
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_input"
    assert not (project / f"qa/changes/{request.reviewed_case.change_id}/inspect/verification.json").exists()


def _publish_verified_inspection(project, request, assessment, verdict):
    from assurance_quality.contracts.agent import InspectionResultV1
    from assurance_quality.contracts.assessment import FinalizedInspectionV1
    from assurance_quality.contracts.metrics import MetricsDocument
    from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
    from assurance_quality.graphs.nodes import publish_inspect
    from assurance_quality.operations.verification import verification_failure_facts
    from tests.verified_assessment_fixture import _write

    fact_ref = _write(project, f"qa/changes/{assessment.change_id}/inspect/fact-baseline.json", b"{}\n")
    finalized = FinalizedInspectionV1(
        agent_result=InspectionResultV1(
            schema_version="1.0",
            change_id=assessment.change_id,
            batch_id=assessment.batch_id,
            inspect_mode="primary",
            classification_performed=True,
            status="analyzed",
            execution_digest=assessment.execution_digest,
            healing_digest=None,
            trace_digest=assessment.trace_ref.digest,
            coverage_digest=assessment.gaps_ref.digest,
            metrics_digest=assessment.metrics_ref.digest,
        ),
        assessment=assessment,
        reviewed_case=request.reviewed_case,
        mapping_ref=request.generation.mapping_ref,
        metrics=MetricsDocument.model_validate_json((project / assessment.metrics_ref.path).read_bytes()),
        sufficiency=TraceSufficiencyFacts.model_validate_json(
            (project / assessment.sufficiency_ref.path).read_bytes()
        ),
        failure_facts=verification_failure_facts(verdict),
        fact_baseline_ref=fact_ref,
        reason_codes=verdict.reason_codes,
        verification=verdict,
    )
    state = {
        "change_id": assessment.change_id,
        "coverage_epoch": assessment.coverage_epoch,
        "batch_id": assessment.batch_id,
        "policy_sha256": request.policy_sha256,
        "assessment_inputs": assessment.model_dump(mode="json"),
        "reviewed_case": request.reviewed_case.model_dump(mode="json"),
        "generation_result": request.generation.model_dump(mode="json"),
        "fact_baseline_ref": fact_ref.model_dump(mode="json"),
    }
    return publish_inspect(state, finalized, ReceiptRef(receipt_id="inspect", receipt_digest="7" * 64))
