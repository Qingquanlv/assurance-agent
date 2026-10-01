from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from tests.capabilities.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import execute_task

from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    ReportResultV1,
)
from assurance_quality.ops.fact_baseline import (
    finalize as fact_baseline_finalize,
    prepare as fact_baseline_prepare,
)
from assurance_quality.ops.inspect import finalize as inspect_finalize
from assurance_quality.ops.issue_analysis import (
    finalize as issue_analysis_finalize,
    prepare as issue_analysis_prepare,
)
from assurance_quality.ops.issue_triage import (
    finalize as issue_triage_finalize,
    prepare as issue_triage_prepare,
)
from assurance_quality.ops.report import finalize as report_finalize
from assurance_quality.resource_loader import resource_bytes
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    EVIDENCE_REF,
    HEX_A,
    HEX_B,
    LEAF,
    as_object,
)

_PACKAGE = Path(__file__).resolve().parent.parent / "assurance_quality"
_RESOURCES = _PACKAGE / "resources"
_SHA = HEX_A
_OWNED = "OBS-owned"
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)
BINDING: dict[str, JSONValue] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}
PLAN_REF: JSONValue = {
    "path": f"qa/results/plan/{HEX_B}/resolved-assurance-plan.json",
    "digest": HEX_B,
}


def issue_candidate(*, evidence_ids: list[str], possible_problem_ids: list[str] | None = None) -> JSONValue:
    return cast(
        JSONValue,
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "evidence_bundle_digest": EVIDENCE_REF,
            "status": "completed",
            "candidate_count": 1,
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": evidence_ids,
                    "proposed": {
                        "title": "boom",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "x",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": possible_problem_ids or [],
                    "confidence": 0.8,
                    "recommended_action": "triage",
                }
            ],
        },
    )


def fake_agent_result(structured_result: JSONValue, **locks: JSONValue) -> JSONValue:
    result = AgentRunResult(
        result_payload=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    payload: dict[str, JSONValue] = {
        "agent_result": result.model_dump(mode="json"),
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "capability_leafs": [LEAF],
        "owned_evidence_ids": [_OWNED],
        "evidence_bundle_digest": EVIDENCE_REF,
        "artifact_paths": [],
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "plan_ref": PLAN_REF,
        "mapping_digest": HEX_A,
        "issue_digest": HEX_B,
    }
    payload.update(locks)
    return payload


def skill_input() -> JSONValue:
    return {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "capability_leafs": [LEAF],
        "artifact_paths": ["inspect/observations.json"],
        "owned_evidence_ids": [_OWNED],
        "evidence_bundle_digest": EVIDENCE_REF,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "plan_ref": PLAN_REF,
        "mapping_digest": HEX_A,
        "issue_digest": HEX_B,
    }


def authenticated_issue_input(root: Path) -> JSONValue:
    base = "qa/results"
    source_path = f"{base}/execution/api-result.json"
    observations_path = f"{base}/inspect/epochs/0/batches/{BATCH_ID}/observations.json"
    manifest_path = f"{base}/inspect/epochs/0/batches/{BATCH_ID}/issue-evidence-manifest.json"
    source = b"failed execution evidence\n"
    source_digest = hashlib.sha256(source).hexdigest()
    entries = [{"path": source_path, "digest": f"sha256:{source_digest}"}]
    observations = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "observations": [
            {
                "observation_id": _OWNED,
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "kind": "test_failure",
                "target": "api",
                "case_id": "TC-1",
                "source": {"artifact": source_path, "json_pointer": "/results/0"},
                "evidence_refs": [source_path],
                "signature": "request failed",
                "observed_at": "2026-08-22T00:00:00Z",
            }
        ],
    }
    entries.append(
        {
            "path": observations_path,
            "digest": f"sha256:{hashlib.sha256(canonical_json_bytes(cast(JSONValue, observations))).hexdigest()}",
        }
    )
    entries.sort(key=lambda entry: entry["path"])
    bundle_digest = f"sha256:{canonical_digest(cast(JSONValue, entries))}"
    manifest = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "digest": bundle_digest,
        "entries": entries,
    }
    for relative, data in (
        (source_path, source),
        (observations_path, canonical_json_bytes(cast(JSONValue, observations))),
        (manifest_path, canonical_json_bytes(cast(JSONValue, manifest))),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {
        **cast(dict[str, JSONValue], skill_input()),
        "artifact_paths": [manifest_path, observations_path, source_path],
        "evidence_bundle_digest": bundle_digest,
    }


def _resource_files() -> Iterator[Path]:
    roots = (_RESOURCES, _PACKAGE / "ops")
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".py":
                yield path


@pytest.mark.asyncio
async def test_issue_finalize_rejects_candidate_without_owned_evidence(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        fake_agent_result(issue_candidate(evidence_ids=["missing"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("surface_kind", ["module", "endpoint"])
async def test_issue_finalize_rechecks_owned_evidence_before_stamping_candidate_digest(
    tmp_path: Path,
    tampered: bool,
    surface_kind: str,
) -> None:
    locked = as_object(authenticated_issue_input(tmp_path))
    structured = as_object(issue_candidate(evidence_ids=[_OWNED]))
    if surface_kind == "endpoint":
        candidate = as_object(cast(list[JSONValue], structured["candidates"])[0])
        candidate["affected_surface"] = {"kind": "endpoint", "value": "POST /api/v1/dept/create"}
    structured["evidence_bundle_digest"] = locked["evidence_bundle_digest"]
    relative = "qa/results/inspect/issue-analysis.json"
    staged = tmp_path / "qa" / ".staging" / "attempt-1" / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(canonical_json_bytes(structured))
    if tampered:
        (tmp_path / "qa/results/execution/api-result.json").write_bytes(b"changed after prepare")
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        fake_agent_result(structured, **locked),
        tmp_path,
    )
    if tampered:
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert "digest changed" in outcome.failure.message
        return
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["agent_result"] == IssueAnalysisResultV1.model_validate(structured).model_dump(mode="json")
    assert payload["candidate_digest"].startswith("sha256:")
    assert payload["issue_analysis_ref"] == {
        "path": relative,
        "digest": hashlib.sha256(canonical_json_bytes(structured)).hexdigest(),
    }


@pytest.mark.asyncio
async def test_completed_analysis_must_account_for_every_owned_observation(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        fake_agent_result(issue_candidate(evidence_ids=[_OWNED]), owned_evidence_ids=[_OWNED, "OBS-second"]),
        tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "every owned observation" in outcome.failure.message


@pytest.mark.asyncio
async def test_issue_finalize_rejects_wrapped_input(tmp_path: Path) -> None:
    payload = as_object(fake_agent_result(issue_candidate(evidence_ids=[_OWNED])))
    agent_result = payload.pop("agent_result")
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        {"validated_input": payload, "agent_result": agent_result},
        tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_business(tmp_path: Path) -> None:
    payload = authenticated_issue_input(tmp_path)
    first = await execute_task(
        cast(TaskHandler, issue_analysis_prepare),
        payload,
        tmp_path,
        binding_data=BINDING,
    )
    second = await execute_task(
        cast(TaskHandler, issue_analysis_prepare),
        payload,
        tmp_path,
        binding_data=BINDING,
    )
    assert first.status == "succeeded", first.failure
    request = AgentRunRequest.model_validate(first.output)
    assert request.canonical_bytes() == AgentRunRequest.model_validate(second.output).canonical_bytes()
    skill, business = request.instructions
    assert skill.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned issue-analyzer skill" in (skill.text_content or "")
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_issue_analysis_prepare_rejects_tampered_manifest_evidence(tmp_path: Path) -> None:
    payload = authenticated_issue_input(tmp_path)
    (tmp_path / "qa/results/execution/api-result.json").write_bytes(b"tampered\n")

    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_prepare),
        payload,
        tmp_path,
        binding_data=BINDING,
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_issue_analysis_prepare_rejects_unbound_observations(tmp_path: Path) -> None:
    payload = as_object(authenticated_issue_input(tmp_path))
    manifest_path = next(
        path for path in payload["artifact_paths"] if path.endswith("issue-evidence-manifest.json")
    )
    manifest = json.loads((tmp_path / manifest_path).read_bytes())
    manifest["entries"] = [
        entry for entry in manifest["entries"] if not entry["path"].endswith("observations.json")
    ]
    manifest["digest"] = f"sha256:{canonical_digest(manifest['entries'])}"
    (tmp_path / manifest_path).write_bytes(canonical_json_bytes(manifest))
    payload["evidence_bundle_digest"] = manifest["digest"]
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_prepare), payload, tmp_path, binding_data=BINDING
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "observations" in outcome.failure.message


@pytest.mark.asyncio
async def test_issue_analysis_prepare_rejects_foreign_observation_in_bound_document(
    tmp_path: Path,
) -> None:
    payload = as_object(authenticated_issue_input(tmp_path))
    observations_path = next(path for path in payload["artifact_paths"] if path.endswith("observations.json"))
    manifest_path = next(
        path for path in payload["artifact_paths"] if path.endswith("issue-evidence-manifest.json")
    )
    observations = json.loads((tmp_path / observations_path).read_bytes())
    observations["observations"][0]["batch_id"] = "BATCH-FOREIGN"
    observation_bytes = canonical_json_bytes(observations)
    (tmp_path / observations_path).write_bytes(observation_bytes)
    manifest = json.loads((tmp_path / manifest_path).read_bytes())
    for entry in manifest["entries"]:
        if entry["path"] == observations_path:
            entry["digest"] = f"sha256:{hashlib.sha256(observation_bytes).hexdigest()}"
    manifest["digest"] = f"sha256:{canonical_digest(manifest['entries'])}"
    (tmp_path / manifest_path).write_bytes(canonical_json_bytes(manifest))
    payload["evidence_bundle_digest"] = manifest["digest"]

    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_prepare), payload, tmp_path, binding_data=BINDING
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "another execution batch" in outcome.failure.message


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **cast(dict[str, object], BINDING["execution"]),
            "provider_model": "primary,fallback",
        },
    }
    outcome = await execute_task(
        cast(TaskHandler, fact_baseline_prepare),
        skill_input(),
        tmp_path,
        binding_data=cast(JSONValue, binding),
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "marker"),
    ((cast(TaskHandler, issue_triage_prepare), "Capability-owned issue-triage skill"),),
)
async def test_each_prepare_locks_skill_and_execution(
    handler: TaskHandler, marker: str, tmp_path: Path
) -> None:
    outcome = await execute_task(handler, skill_input(), tmp_path, binding_data=BINDING)
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert marker in (request.instructions[0].text_content or "")
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_fact_baseline_finalize_requires_authenticated_reviewed_case(tmp_path: Path) -> None:
    structured = {
        "source": "seed_file",
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "warnings": [],
        "facts": {"route_prefix": "/api/v1"},
        "source_evidence_ids": ["EVID-missing"],
    }
    outcome = await execute_task(
        cast(TaskHandler, fact_baseline_finalize),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_inspect_finalize_requires_authenticated_assessment_input(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": "c" * 64,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
    }
    outcome = await execute_task(
        cast(TaskHandler, inspect_finalize),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_issue_triage_finalize_rejects_unauthenticated_evidence_digest(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.0",
        "problem_id": "PROB-1",
        "expected_problem_version": 1,
        "review_id": "REV-1",
        "change_id": CHANGE_ID,
        "evidence_digests": {"inspect/observations.json": "sha256:" + ("f" * 64)},
        "recommended_action": "confirm_assessment",
        "reason": "x",
        "summary": "y",
        "reasoning": "z",
    }
    outcome = await execute_task(
        cast(TaskHandler, issue_triage_finalize),
        fake_agent_result(
            cast(JSONValue, structured),
            locked_evidence_digests={"inspect/observations.json": EVIDENCE_REF},
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_issue_triage_finalize_rejects_empty_or_subset_evidence_digests(tmp_path: Path) -> None:
    locked = {
        "inspect/observations.json": EVIDENCE_REF,
        "issues/snapshot.json": EVIDENCE_REF,
    }
    empty = {
        "schema_version": "1.0",
        "problem_id": "PROB-1",
        "expected_problem_version": 1,
        "review_id": "REV-1",
        "change_id": CHANGE_ID,
        "evidence_digests": {},
        "recommended_action": "confirm_assessment",
        "reason": "x",
        "summary": "y",
        "reasoning": "z",
    }
    subset = {
        **empty,
        "evidence_digests": {"inspect/observations.json": EVIDENCE_REF},
    }
    for structured in (empty, subset):
        outcome = await execute_task(
            cast(TaskHandler, issue_triage_finalize),
            fake_agent_result(cast(JSONValue, structured), locked_evidence_digests=locked),
            tmp_path,
        )
        assert outcome.failure is not None
        assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_issue_analysis_finalize_rejects_forged_problem_id(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        fake_agent_result(issue_candidate(evidence_ids=[_OWNED], possible_problem_ids=["PROB-FORGED"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["endpoint_without_method", "empty_normalized_symptom"])
async def test_issue_analysis_finalize_rejects_invalid_fingerprint_without_crashing(
    tmp_path: Path, invalid: str
) -> None:
    structured = as_object(issue_candidate(evidence_ids=[_OWNED]))
    candidate = as_object(cast(list[JSONValue], structured["candidates"])[0])
    if invalid == "endpoint_without_method":
        candidate["affected_surface"] = {"kind": "endpoint", "value": "/api/v1/dept/update"}
    else:
        candidate["fingerprint_inputs"] = {"surface": "dept", "symptom": "- / ."}
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize), fake_agent_result(structured), tmp_path
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_issue_analysis_finalize_requires_evidence_bundle_lock(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, issue_analysis_finalize),
        fake_agent_result(issue_candidate(evidence_ids=[_OWNED]), evidence_bundle_digest=None),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_report_finalize_rejects_unreferenced_projection(tmp_path: Path) -> None:
    structured = {
        "schema_version": "1.1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "mapping_digest": HEX_A,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "issue_digest": HEX_B,
        "metrics_digest": "d" * 64,
        "risk_rationale": "refined",
        "recommendation": "Do not release: failing tests or hard blockers must be resolved first.",
    }
    outcome = await execute_task(
        cast(TaskHandler, report_finalize),
        fake_agent_result(cast(JSONValue, structured)),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


def test_quality_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        _PACKAGE / "ops/fact_baseline/SKILL.md",
        _PACKAGE / "ops/inspect/SKILL.md",
        _PACKAGE / "ops/issue_analysis/SKILL.md",
        _PACKAGE / "ops/issue_triage/SKILL.md",
        _PACKAGE / "ops/report/SKILL.md",
        _RESOURCES / "skills/aa-dashboard/SKILL.md",
        _PACKAGE / "ops/fact_baseline/result.schema.json",
        _PACKAGE / "ops/inspect/result.schema.json",
        _PACKAGE / "ops/issue_analysis/result.schema.json",
        _PACKAGE / "ops/issue_triage/result.schema.json",
        _PACKAGE / "ops/report/result.schema.json",
    )
    missing = [item for item in required if not item.is_file()]
    assert missing == [], f"missing quality resources: {missing}"
    hits: list[str] = []
    for path in _resource_files():
        if path.suffix == ".json":
            continue
        if _TOKEN.search(path.read_text(encoding="utf-8")):
            hits.append(path.relative_to(_PACKAGE).as_posix())
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in _resource_files() if path.suffix != ".json"
    )
    for token in _FORBIDDEN:
        assert token not in lowered
    assert "Do not claim a lifecycle transition" in (
        _PACKAGE / "ops/issue_triage/SKILL.md"
    ).read_text(encoding="utf-8")


def test_fact_baseline_records_only_source_proven_initial_admin_credentials() -> None:
    skill = " ".join((_PACKAGE / "ops/fact_baseline/SKILL.md").read_text(encoding="utf-8").split())

    assert "startup initialization or seed source" in skill
    assert "`admin_username` and `admin_password`" in skill
    assert "deterministically resolves both values" in skill
    assert "omit both credential facts" in skill
    assert "Never read `.env` or `*.env` files" in skill


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("ops/fact_baseline/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, FactBaselineResultV1.model_json_schema())
    )
    assert resource_bytes("ops/inspect/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, InspectionResultV1.model_json_schema())
    )
    assert resource_bytes("ops/issue_analysis/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, IssueAnalysisResultV1.model_json_schema())
    )
    assert resource_bytes("ops/issue_triage/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, IssueTriageResultV1.model_json_schema())
    )
    assert resource_bytes("ops/report/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, ReportResultV1.model_json_schema())
    )


def test_inspect_skill_distinguishes_execution_failure_from_inspection_failure() -> None:
    skill = " ".join((_PACKAGE / "ops/inspect/SKILL.md").read_text(encoding="utf-8").split())

    assert "Execution test failures do not make the Inspect operation itself failed" in skill
    assert "When the authenticated execution evidence contains failures" in skill
    assert '`status="analyzed"` and `classification_performed=true`' in skill
    assert "When the authenticated execution evidence contains no failures" in skill
    assert '`status="no_failures"`' in skill
    assert 'Use `status="failed"` only when the locked evidence cannot be authenticated or analyzed' in skill


def test_quality_plugin_has_no_product_hooks_import() -> None:
    from quality_fixtures import production_import_roots  # pyright: ignore[reportMissingImports]

    names = production_import_roots()
    assert "ProductHooks" not in names
    assert "assurance_agent" not in names
    assert "assurance_kernel" not in names
    assert "agent_runtime_opencode" not in names
    assert "agent_runtime_cursor" not in names


@pytest.mark.asyncio
async def test_failed_report_validation_leaves_canonical_outputs_unchanged(tmp_path: Path) -> None:
    from tests.product.test_change_local_output_routing import dual_roots

    project, write_root = dual_roots(tmp_path)
    canonical = project / "qa/results/report/report.md"
    canonical.parent.mkdir(parents=True)
    original = b"# Canonical report\n"
    canonical.write_bytes(original)
    outcome = await execute_task(
        cast(TaskHandler, report_finalize),
        fake_agent_result({"schema_version": "1.0"}),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert canonical.read_bytes() == original
