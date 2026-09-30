"""Test-only capability catalog, leaf validators, and canonical handoff helpers."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from tests.capabilities.conformance import execute_task

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.selection import SelectHandler
from assurance_generation.contracts.codegen import CodegenMapping
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.operations.planning import validate_plan_input
from assurance_healing.contracts.agent import FixProposalInputV1
from assurance_healing.contracts.status import HealingStatusV1
from assurance_healing.operations.proposal import FixProposalFinalizeHandler, FixProposalPrepareHandler
from assurance_improvement.operations.archive import (
    ArchivePublishReceipt,
    ProjectArchiveInput,
    project_archive,
)
from assurance_intake.contracts import CaseYamlAuthoring
from assurance_quality.contracts.coverage import CoverageGapsDocument
from assurance_quality.contracts.report import QualityReport
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.coverage import coverage_gap_to_repair_brief
from assurance_quality.operations.inspect import InspectHandler, InspectInputV1, document_digest
from assurance_quality.operations.trace import TraceOperationInput, project_trace

CapabilityValidator = Callable[..., None]
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(__file__).resolve().parent / "fixtures" / "capability-catalog.v1.json"
_WHEEL_ROOTS = REPO_ROOT / "packages"
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_L1_CAPABILITY_ROOTS = (
    "auth.",
    "accounts.",
    "entities.",
    "auth_matrix.",
    "capabilities.cleanup.",
    "capabilities.domain_factories.",
    "capabilities.adapters.",
)
_CHANGE_ID = "CH-DEMO-001"
_BATCH_ID = "20260822T000000Z"
_CASE_ID = "TC_MENU_001"
_HEX = "a" * 64
_PLAN_REF = {
    "path": f"qa/results/plan/{_HEX}/resolved-assurance-plan.json",
    "digest": _HEX,
}
_PROPOSAL_BINDING = cast(
    JSONValue,
    {
        "agent_profile": "aa-doc-author",
        "execution": {
            "provider_model": "test-model",
            "worker_profile": "worker",
            "permission_profile_digest": _HEX,
            "limits": {"max_seconds": 5},
        },
        "request_policy_digest": _HEX,
        "request_config_digest": _HEX,
    },
)
_ENVELOPE_KEYS = frozenset({"schema_id", "digest", "family", "payload"})
_EXACT_LEAFS = (
    "auth.session.create",
    "capabilities.adapters.create",
    "entities.item.create",
)


def load_capability_catalog(path: Path | None = None) -> dict[str, JSONValue]:
    target = path or CATALOG_PATH
    raw = target.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("capability catalog must be a JSON object")
    expected = canonical_json_bytes(cast(JSONValue, document))
    if raw != expected:
        raise ValueError("capability catalog must be canonical JSON")
    return document


def validate_catalog_document(document: Mapping[str, object]) -> frozenset[str]:
    if document.get("schema_version") != "1":
        raise ValueError("unknown catalog schema version")
    digest_input = document.get("digest_input")
    if not isinstance(digest_input, dict):
        raise ValueError("catalog digest_input must be an object")
    leafs = digest_input.get("leafs")
    if not isinstance(leafs, list) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("catalog digest_input.leafs must be a list of strings")
    typed = [str(item) for item in leafs]
    if document.get("digest") != canonical_digest(cast(JSONValue, digest_input)):
        raise ValueError("catalog digest does not match digest_input")
    if len(typed) != len(set(typed)):
        raise ValueError("catalog leafs must not contain duplicates")
    if typed != sorted(typed):
        raise ValueError("catalog leafs must be canonically sorted")
    for leaf in typed:
        if leaf not in _EXACT_LEAFS:
            raise ValueError(f"unknown catalog leaf: {leaf}")
        if not _is_typed_leaf(leaf):
            raise ValueError(f"catalog leaf is a prefix or non-leaf node: {leaf}")
    for left in typed:
        for right in typed:
            if left != right and (right.startswith(f"{left}.") or left.startswith(f"{right}.")):
                raise ValueError("catalog must not contain prefix or non-leaf nodes")
    if set(typed) != set(_EXACT_LEAFS):
        raise ValueError("catalog leafs must be the exact typed leaf set")
    return frozenset(typed)


def _is_typed_leaf(key: str) -> bool:
    if not key or key != key.strip() or key.endswith(".") or "." not in key:
        return False
    for root in _L1_CAPABILITY_ROOTS:
        if not key.startswith(root):
            continue
        remainder = key[len(root) :]
        return bool(remainder) and ("." in remainder or remainder == "create")
    return False


CAPABILITY_CATALOG = validate_catalog_document(load_capability_catalog())


def catalog_leafs(catalog: object) -> frozenset[str]:
    if isinstance(catalog, frozenset) and all(isinstance(item, str) for item in catalog):
        return catalog
    if isinstance(catalog, (set, tuple, list)) and all(isinstance(item, str) for item in catalog):
        return frozenset(catalog)
    if isinstance(catalog, Mapping):
        if "digest_input" in catalog and isinstance(catalog["digest_input"], Mapping):
            return validate_catalog_document(catalog)
        leafs = catalog.get("leafs")
        if isinstance(leafs, list) and all(isinstance(item, str) for item in leafs):
            return frozenset(str(item) for item in leafs)
    raise TypeError("catalog must be a frozenset of typed leaves or the catalog document")


def _package_source_root(package: str) -> Path:
    name = package.replace("_", "-")
    candidates = (
        _WHEEL_ROOTS / "capabilities" / name / package,
        _WHEEL_ROOTS / "adapters" / name / package,
        _WHEEL_ROOTS / "features" / name / package,
        _WHEEL_ROOTS / "clients" / name / package,
        _WHEEL_ROOTS / "products" / name / package,
        _WHEEL_ROOTS / "framework" / name / package,
        _WHEEL_ROOTS / name / package,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"package source is missing: {candidates[0]}")


def forbidden_imports(package: str, prefix: str | None = None) -> set[str]:
    root = _package_source_root(package)
    forbidden = {*_LEGACY_ROOTS}
    if prefix is not None:
        forbidden.add(prefix)
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in forbidden):
                found.add(module_name)
    return found


def quality_gap_fixture() -> CoverageGapsDocument:
    return CoverageGapsDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": _CHANGE_ID,
            "batch_id": _BATCH_ID,
            "projection_digest": f"sha256:{_HEX}",
            "gaps": [
                {
                    "kind": "uncovered_required_case",
                    "locator": {"case_id": _CASE_ID},
                    "layer": "execution",
                    "batch_id": _BATCH_ID,
                    "evidence_refs": [f"sha256:{_HEX}"],
                }
            ],
        }
    )


def all_capability_leaf_validators() -> tuple[CapabilityValidator, ...]:
    return (
        _intake_case_leaf,
        _generation_plan_review_leaf,
        _generation_plan_result_leaf,
        _generation_planning_input_leaf,
        _execution_mapping_leaf,
        _execution_evidence_leaf,
        _quality_trace_leaf,
        _quality_project_trace_leaf,
        _healing_claimed_leaf,
    )


def _intake_case_leaf(value: str, *, catalog: object) -> None:
    CaseYamlAuthoring.model_validate(
        _authoring_payload(value),
        context={"capability_leafs": catalog_leafs(catalog)},
    )


def _generation_plan_review_leaf(value: str, *, catalog: object) -> None:
    PlanReviewAuthoring.model_validate(
        _plan_review_payload(value),
        context={"capability_leafs": catalog_leafs(catalog)},
    )


def _generation_plan_result_leaf(value: str, *, catalog: object) -> None:
    PlanResultV1.model_validate(
        _plan_result_payload(value),
        context={"capability_leafs": catalog_leafs(catalog)},
    )


def _generation_planning_input_leaf(value: str, *, catalog: object) -> None:
    validate_plan_input(
        _planning_input(value, catalog_leafs(catalog)),
        family="api",
        workspace=Path("."),
    )


def _execution_mapping_leaf(value: str, *, catalog: object) -> None:
    ClosedMappingV1.model_validate(
        _closed_mapping_payload(value),
        context={"capability_leafs": catalog_leafs(catalog), "case_ids": frozenset({_CASE_ID})},
    )


def _execution_evidence_leaf(value: str, *, catalog: object) -> None:
    ExecutionEvidenceV1.model_validate(
        _evidence_payload(value),
        context={"capability_leafs": catalog_leafs(catalog), "case_ids": frozenset({_CASE_ID})},
    )


def _quality_trace_leaf(value: str, *, catalog: object) -> None:
    TraceProjectionV2.model_validate(_trace_payload(value), context=_trace_context(catalog_leafs(catalog)))


def _quality_project_trace_leaf(value: str, *, catalog: object) -> None:
    leafs = catalog_leafs(catalog)
    project_trace(
        TraceOperationInput.model_validate(
            {
                "change_id": _CHANGE_ID,
                "batch_id": _BATCH_ID,
                "phase": "execution",
                "closed_mapping": ["qa/tests/generated.py"],
                "observed": ["qa/tests/generated.py"],
                "capability_leafs": tuple(sorted(leafs)),
                "case_ids": [_CASE_ID],
                "cases": [
                    {
                        "case_id": _CASE_ID,
                        "module": "menus",
                        "case_type": "API",
                        "automation_required": True,
                        "capability": value,
                    }
                ],
            }
        )
    )


def _healing_claimed_leaf(value: str, *, catalog: object) -> None:
    leafs = catalog_leafs(catalog)
    outcome = _run(FixProposalFinalizeHandler(), _healing_finalize_payload(value, leafs))
    if outcome.status != "failed" or outcome.failure is None:
        raise ValueError("healing finalize accepted an unknown capability leaf")
    raise ValueError(outcome.failure.message)


def encode_handoff(schema_id: str, family: str, payload: Mapping[str, object]) -> bytes:
    body = cast(JSONValue, dict(payload))
    envelope: dict[str, JSONValue] = {
        "digest": canonical_digest(body),
        "family": family,
        "payload": body,
        "schema_id": schema_id,
    }
    return canonical_json_bytes(envelope)


def consume_handoff(
    raw: bytes,
    *,
    schema_id: str,
    family: str,
    catalog: object,
    allowed_fields: frozenset[str],
    consumer: Callable[[dict[str, Any], frozenset[str]], object],
) -> object:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("handoff is not valid JSON") from error
    if not isinstance(envelope, dict):
        raise ValueError("handoff must be a JSON object")
    extra = set(envelope) - _ENVELOPE_KEYS
    missing = _ENVELOPE_KEYS - set(envelope)
    if extra:
        raise ValueError("extra field")
    if missing:
        raise ValueError("missing field")
    if envelope["schema_id"] != schema_id:
        raise ValueError("changed schema ID")
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ValueError("handoff payload must be an object")
    if envelope["digest"] != canonical_digest(cast(JSONValue, payload)):
        raise ValueError("changed digest")
    if envelope["family"] != family:
        raise ValueError("wrong family")
    extra_payload = set(payload) - allowed_fields
    missing_payload = allowed_fields - set(payload)
    if extra_payload:
        raise ValueError("extra field")
    if missing_payload:
        raise ValueError("missing field")
    leafs = catalog_leafs(catalog)
    unknown = _capability_values(payload) - leafs - {None, ""}
    if unknown:
        raise ValueError(f"unknown capability leaf: {sorted(unknown)[0]}")
    return consumer(cast(dict[str, Any], payload), leafs)


def handoff_seams() -> tuple[dict[str, Any], ...]:
    leaf = "entities.item.create"
    cases = _authoring_payload(leaf)
    plan = _plan_result_payload(leaf)
    mapping = _codegen_mapping_payload()
    evidence = _evidence_payload(leaf)
    healing = _healing_status_payload()
    gaps = quality_gap_fixture().model_dump(mode="json")
    report = _quality_report_payload()
    return (
        {
            "name": "intake_reviewed_case",
            "schema_id": "assurance.intake.schema.case-authoring.v1",
            "family": "api",
            "payload": cases,
            "allowed": frozenset(cases),
            "consumer": _consume_planning_input,
        },
        {
            "name": "generation_plan_mapping",
            "schema_id": "assurance.generation.schema.codegen-mapping.v1",
            "family": "api",
            "payload": {"plan": plan, "mapping": mapping, "reviewed_cases": cases},
            "allowed": frozenset({"plan", "mapping", "reviewed_cases"}),
            "consumer": _consume_execution_selection,
        },
        {
            "name": "execution_evidence",
            "schema_id": "assurance.execution.schema.execution-evidence.v1",
            "family": "api",
            "payload": evidence,
            "allowed": frozenset(evidence),
            "consumer": _consume_evidence,
        },
        {
            "name": "healing_status",
            "schema_id": "assurance.healing.schema.healing-status.v1",
            "family": "api",
            "payload": healing,
            "allowed": frozenset(healing),
            "consumer": _consume_healing_status,
        },
        {
            "name": "quality_coverage_gap",
            "schema_id": "assurance.quality.schema.coverage-gaps.v1",
            "family": "api",
            "payload": gaps,
            "allowed": frozenset(gaps),
            "consumer": _consume_coverage_gap,
        },
        {
            "name": "quality_report",
            "schema_id": "assurance.quality.schema.report.v1",
            "family": "api",
            "payload": report,
            "allowed": frozenset(report),
            "consumer": _consume_quality_report,
        },
    )


def _consume_planning_input(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    return validate_plan_input(
        _planning_input_from_cases(payload, leafs),
        family="api",
        workspace=Path("."),
    )


def _consume_execution_selection(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    PlanResultV1.model_validate(payload["plan"], context={"capability_leafs": leafs})
    CodegenMapping.model_validate(payload["mapping"])
    outcome = _run(
        SelectHandler(),
        {
            "change_id": _CHANGE_ID,
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "mappings": [payload["mapping"]],
            "reviewed_cases": payload["reviewed_cases"],
            "capability_leafs": list(sorted(leafs)),
            "case_ids": [_CASE_ID],
        },
    )
    if outcome.status != "succeeded":
        message = outcome.failure.message if outcome.failure is not None else "selection failed"
        raise ValueError(message)
    return outcome.output


def _consume_evidence(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    evidence = ExecutionEvidenceV1.model_validate(
        payload,
        context={"capability_leafs": leafs, "case_ids": frozenset({_CASE_ID})},
    )
    project_trace(
        TraceOperationInput.model_validate(
            {
                "change_id": _CHANGE_ID,
                "batch_id": _BATCH_ID,
                "closed_mapping": list(evidence.mapping.selected),
                "observed": [item.test for item in evidence.results],
                "capability_leafs": tuple(sorted(leafs)),
                "case_ids": [_CASE_ID],
                "cases": [
                    {
                        "case_id": _CASE_ID,
                        "module": "menus",
                        "case_type": "API",
                        "automation_required": True,
                        "capability": evidence.mapping.mappings[0].capability,
                    }
                ],
                "execution_evidence": evidence.model_dump(mode="json"),
            }
        )
    )
    proposal = FixProposalInputV1.model_validate(
        _fix_proposal_input(leafs, canonical_digest(cast(JSONValue, payload)))
    )
    outcome = _run(
        FixProposalPrepareHandler(),
        proposal.model_dump(mode="json"),
        binding_data=_PROPOSAL_BINDING,
    )
    if outcome.status != "succeeded":
        message = outcome.failure.message if outcome.failure is not None else "fix proposal prepare failed"
        raise ValueError(message)
    return proposal


def _consume_healing_status(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    del leafs
    status = HealingStatusV1.model_validate(payload)
    evidence = _evidence_payload("entities.item.create")
    inspect = {
        "change_id": _CHANGE_ID,
        "batch_id": _BATCH_ID,
        "execution": evidence,
        "healing": payload,
        "trace": {"kind": "trace"},
        "coverage": {"kind": "coverage"},
        "metrics": {"kind": "metrics"},
        "execution_digest": document_digest(ExecutionEvidenceV1.model_validate(evidence)),
        "healing_digest": document_digest(status),
        "trace_digest": canonical_digest({"kind": "trace"}),
        "coverage_digest": canonical_digest({"kind": "coverage"}),
        "metrics_digest": canonical_digest({"kind": "metrics"}),
        "result_paths": {"api": "execution/api-result.json"},
    }
    InspectInputV1.model_validate(inspect)
    outcome = _run(InspectHandler(), inspect)
    if outcome.status != "succeeded":
        message = outcome.failure.message if outcome.failure is not None else "inspect failed"
        raise ValueError(message)
    return status


def _consume_coverage_gap(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    del leafs
    return coverage_gap_to_repair_brief(CoverageGapsDocument.model_validate(payload))


def _consume_quality_report(payload: dict[str, Any], leafs: frozenset[str]) -> object:
    del leafs
    report = QualityReport.model_validate(payload)
    return project_archive(
        ProjectArchiveInput(
            change_id=report.change_id,
            invocation_id="inv-archive-1",
            archive_digest=_HEX,
            report=report,
            publish_receipt=ArchivePublishReceipt(
                schema_version="1",
                change_id=report.change_id,
                manifest_digest=_HEX,
                source_digest=_HEX,
                target_baseline=_HEX,
                final_digest=_HEX,
            ),
        )
    )


def _authoring_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": "TC_MENU_001",
                "title": "create menu happy path",
                "status": "active",
                "priority": "P1",
                "severity": "major",
                "type": "API",
                "module": "menus",
                "requirement_id": "REQ-1",
                "feature_name": "menu-management",
                "test_condition_id": "COND-1",
                "design_technique": "use_case",
                "objective": "verify the menu behavior",
                "summary": "exercise and assert the menu behavior",
                "preconditions": [],
                "test_data": [],
                "steps": ["perform the operation"],
                "assertions": ["the operation succeeds"],
                "postconditions": [],
                "edge_cases": [],
                "related_cases": [],
                "risk": {
                    "level": "high",
                    "likelihood": 3,
                    "impact": 4,
                    "rationale": "important administration path",
                },
                "automation": {"required": True, "framework": "pytest", "status": "planned"},
                "regression": {
                    "candidate": True,
                    "tier": "smoke",
                    "rationale": "protect the administration path",
                    "selection_reason": ["critical_user_journey"],
                    "maintenance_rule": "keep_until_feature_deprecated",
                },
                "trace": {leaf: {"covered": True}},
            }
        ],
        "modified": [],
        "removed": [],
    }


def _plan_review_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "review_type": "api-codegen",
        "change_id": _CHANGE_ID,
        "route": "codegen",
        "findings": [],
        "finding_ids": [],
        "next_action": "proceed to execute",
        "risk_level": "medium",
        "required_capabilities": [leaf],
    }


def _plan_result_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "family": "api",
        "change_id": _CHANGE_ID,
        "case_ids": [_CASE_ID],
        "required_capabilities": [leaf],
        "coverage": [
            {
                "case_id": _CASE_ID,
                "operation": "create",
                "risk": "high",
                "required_capabilities": [leaf],
            }
        ],
        "output_files": ["qa/results/plans/api-plan.md"],
    }


def _planning_input(leaf: str, catalog: frozenset[str]) -> dict[str, object]:
    return _planning_input_from_cases(_authoring_payload(leaf), catalog)


def _planning_input_from_cases(cases: Mapping[str, object], catalog: frozenset[str]) -> dict[str, object]:
    return {
        "change_id": _CHANGE_ID,
        "plan_digest": _HEX,
        "plan_ref": _PLAN_REF,
        "capability_leafs": tuple(sorted(catalog)),
        "artifact_paths": ["qa/results/plans/api-plan.md"],
        "reviewed_cases": dict(cases),
        "family_constraints": {
            "write_roots": ["qa/results/plans/"],
            "operations": ["create"],
            "risks": ["high"],
        },
    }


def _codegen_mapping_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "layer": "api",
        "entries": [{"case_id": _CASE_ID, "symbol": "test_ok", "target_file": "qa/tests/generated.py"}],
    }


def _closed_mapping_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "selected": ["qa/tests/generated.py"],
        "mappings": [
            {"test": "qa/tests/generated.py", "case_id": _CASE_ID, "capability": leaf, "layer": "api"}
        ],
    }


def _evidence_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": _CHANGE_ID,
        "plan_digest": _HEX,
        "plan_ref": _PLAN_REF,
        "batch_id": _BATCH_ID,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "family_outcomes": [{"family": "api", "state": "executed"}],
        "mapping": _closed_mapping_payload(leaf),
        "mapping_digest": _HEX,
        "baseline_tree_id": _HEX,
        "runner_profile_digest": _HEX,
        "receipt_digest": _HEX,
        "receipt": {
            "commands": [
                {
                    "family": "api",
                    "command": ["pytest"],
                    "exit_code": 0,
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                }
            ]
        },
        "results": [
            {
                "test": "qa/tests/generated.py",
                "status": "passed",
                "duration_ms": 1,
                "message": "",
                "case_id": _CASE_ID,
            }
        ],
    }


def _trace_payload(leaf: str) -> dict[str, object]:
    return {
        "schema_version": "2",
        "change_id": _CHANGE_ID,
        "phase": "execution",
        "authoritative_batch_id": _BATCH_ID,
        "sources": [],
        "rows": [
            {
                "case_id": "TC_MENU_001",
                "module": "menus",
                "case_type": "API",
                "automation_required": True,
                "assertions": ["the operation succeeds"],
                "covering_tests": [{"file": "tests/api/test_menu.py", "test_name": "test_create"}],
                "coverage_state": "covered",
                "presence_in_current_batch": "executed",
                "capability": leaf,
                "plan_id": "api-plan",
                "schema_id": "assurance.quality.schema.trace.v2",
            }
        ],
        "unmapped_tests": [],
        "gaps": [],
        "integrity": "complete",
    }


def _trace_context(leafs: frozenset[str]) -> dict[str, frozenset[str]]:
    return {
        "capability_leafs": leafs,
        "case_ids": frozenset({_CASE_ID}),
        "plan_ids": frozenset({"api-plan"}),
        "test_ids": frozenset({"tests/api/test_menu.py::test_create"}),
        "schema_ids": frozenset({"assurance.quality.schema.trace.v2"}),
    }


def _healing_status_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": _CHANGE_ID,
        "status": "not_needed",
        "attempts_used": 0,
    }


def _quality_report_payload() -> dict[str, object]:
    counts = {"total": 1, "passed": 1, "failed": 0}
    return {
        "schema_version": "1.1",
        "change_id": _CHANGE_ID,
        "batch_id": _BATCH_ID,
        "plan": {"plan_digest": _HEX, "plan_ref": _PLAN_REF},
        "final_status": "PASS",
        "quality_score": 1.0,
        "score_breakdown": {"functional": 1.0, "coverage": 1.0, "fuzz": "N/A", "performance": "N/A"},
        "scope": {"cases": 1, "requirements": []},
        "functional": {"status": "PASS", "api": counts, "e2e": counts},
        "coverage": {
            "status": "PASS",
            "available": True,
            "line_coverage": 1.0,
            "branch_coverage": 1.0,
            "threshold": {"line": 0.8, "branch": 0.8},
        },
        "defects": {"product": [], "test": [], "environment": []},
        "risk_level": "LOW",
        "risk_rationale": "ok",
        "recommendation": "ship",
        "issues": {
            "analysis_status": "completed",
            "project_sync_status": "synced",
            "total_occurrences": 1,
            "counts_by_status": {},
            "counts_by_classification": {},
            "counts_by_severity": {},
            "new_count": 0,
            "repeated_count": 0,
            "regressed_count": 0,
            "resolved_count": 0,
            "accepted_risk_count": 0,
            "not_an_issue_count": 0,
            "issue_risk": "clear",
            "issue_risk_rationale": "no active issues",
        },
        "metrics": {"schema_version": "1", "change_id": _CHANGE_ID},
    }


def _fix_proposal_input(leafs: frozenset[str], evidence_digest: str) -> dict[str, Any]:
    return {
        "change_id": _CHANGE_ID,
        "plan_digest": _HEX,
        "plan_ref": _PLAN_REF,
        "owner_id": "assurance.healing",
        "capability_leafs": list(sorted(leafs)),
        "allowed_paths": ["tests/api/test_users.py"],
        "allowed_roots": ["tests/"],
        "baseline_digest": "b" * 64,
        "candidate_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "mapping_paths": ["tests/api/test_users.py"],
        "require_approval": True,
        "execution_evidence_digest": evidence_digest,
    }


def _healing_finalize_payload(
    claimed: str,
    leafs: frozenset[str],
    *,
    evidence_digest: str | None = None,
) -> dict[str, Any]:
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.wire.schema import canonical_digest as runtime_digest
    from tests.capabilities.agent_harness import FakeAgentAdapter

    structured = {
        "schema_version": "1",
        "change_id": _CHANGE_ID,
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "P1",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "needs_review": False,
                "files_to_modify": ["tests/api/test_users.py"],
            }
        ],
    }
    prepare = _fix_proposal_input(leafs, evidence_digest or ("e" * 64))
    result = AgentRunResult(
        result_payload=cast(JSONValue, structured),
        result_digest=runtime_digest(cast(JSONValue, structured)),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    return {
        "agent_result": result.model_dump(mode="json"),
        **prepare,
        "claimed_capabilities": [claimed],
        "prepare": prepare,
    }


def _capability_values(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key in {"capability", "claimed_capabilities", "required_capabilities"} or key == "trace":
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, Mapping):
                    found.update(str(item) for item in value)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    found.update(str(item) for item in value if isinstance(item, str))
            found.update(_capability_values(value))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            found.update(_capability_values(item))
    return found


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def _run(handler: object, payload: object, *, binding_data: JSONValue = None) -> Any:
    return asyncio.run(execute_task(handler, cast(JSONValue, payload), binding_data=binding_data))  # type: ignore[arg-type]


__all__ = [
    "CAPABILITY_CATALOG",
    "CATALOG_PATH",
    "all_capability_leaf_validators",
    "catalog_leafs",
    "consume_handoff",
    "encode_handoff",
    "forbidden_imports",
    "handoff_seams",
    "load_capability_catalog",
    "quality_gap_fixture",
    "validate_catalog_document",
]
