from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_quality.contracts import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AuthMatrixEvidence,
    BaselineDriftEvidence,
    CLayerMetricsDocument,
    ChangeIssueSnapshot,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    CoverageGapsDocument,
    FactBaselineAuthoring,
    JourneyCoverageEvidence,
    MinimumCoverageResult,
    MutationEvidence,
    PerfSlackEvidence,
    QualityGateResultV2,
    QualityReport,
    QuarantineProjection,
    SufficiencyReportV2,
    TraceProjectionV2,
    TraceSufficiencyFacts,
)
from assurance_quality.contracts.issue_events import CHANGE_ISSUE_EVENT_ADAPTER
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.plugin import QualityPlugin

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create"})
VALID_CASES = frozenset({"TC_MENU_001"})
VALID_PLANS = frozenset({"api-plan"})
VALID_TESTS = frozenset({"tests/api/test_menu.py::test_create"})
VALID_SCHEMAS = frozenset({"assurance.quality.schema.trace.v2"})
VALID_ISSUES = frozenset({"ISS-1"})
VALID_EVIDENCE = frozenset({"sha256:" + ("a" * 64)})
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_ALLOWED_ASSURANCE = (
    "assurance_intake.contracts",
    "assurance_generation.contracts",
    "assurance_execution.contracts",
    "assurance_healing.contracts",
)


def catalog_context() -> dict[str, frozenset[str]]:
    return {
        "capability_leafs": VALID_LEAFS,
        "case_ids": VALID_CASES,
        "plan_ids": VALID_PLANS,
        "test_ids": VALID_TESTS,
        "schema_ids": VALID_SCHEMAS,
        "issue_ids": VALID_ISSUES,
        "evidence_refs": VALID_EVIDENCE,
    }


def trace_fixture(*, capability: str = "entities.item.create") -> dict[str, object]:
    return {
        "schema_version": "2",
        "change_id": "CH-DEMO-001",
        "phase": "execution",
        "authoritative_batch_id": "20260822T000000Z",
        "sources": [],
        "rows": [
            {
                "case_id": "TC_MENU_001",
                "module": "menus",
                "case_type": "API",
                "automation_required": True,
                "assertions": ("the operation succeeds",),
                "covering_tests": ({"file": "tests/api/test_menu.py", "test_name": "test_create"},),
                "coverage_state": "covered",
                "presence_in_current_batch": "executed",
                "capability": capability,
                "plan_id": "api-plan",
                "schema_id": "assurance.quality.schema.trace.v2",
                "issue_ids": (),
                "evidence_refs": (),
            }
        ],
        "unmapped_tests": [],
        "gaps": [],
        "integrity": "complete",
    }


def schema_bytes(schema_id: str) -> bytes:
    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_quality_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_quality"
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _LEGACY_ROOTS):
                found.add(module_name)
            if (
                module_name.startswith("assurance_intake.")
                or module_name.startswith("assurance_generation.")
                or module_name.startswith("assurance_execution.")
                or module_name.startswith("assurance_healing.")
            ):
                if not any(
                    module_name == allowed or module_name.startswith(f"{allowed}.")
                    for allowed in _ALLOWED_ASSURANCE
                ):
                    found.add(module_name)
            if module_name in {
                "assurance_intake",
                "assurance_generation",
                "assurance_execution",
                "assurance_healing",
            }:
                found.add(module_name)
            if module_name == "assurance_product" or module_name.startswith("assurance_product."):
                found.add(module_name)
    return found


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_quality_descriptor_declares_exact_dependency_order() -> None:
    assert tuple(item.plugin_id for item in QualityPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
    )


def test_trace_rejects_unmapped_or_unknown_capability() -> None:
    with pytest.raises(ValidationError, match="trace capability is not a frozen typed leaf"):
        TraceProjectionV2.model_validate(
            trace_fixture(capability="entities.fake"),
            context={"capability_leafs": VALID_LEAFS},
        )


def test_trace_rejects_prefix_capability_leaf() -> None:
    with pytest.raises(ValidationError, match="trace capability is not a frozen typed leaf"):
        TraceProjectionV2.model_validate(
            trace_fixture(capability="entities.item"),
            context={"capability_leafs": VALID_LEAFS},
        )


def test_trace_accepts_exact_typed_leaf() -> None:
    model = TraceProjectionV2.model_validate(trace_fixture(), context=catalog_context())
    assert model.rows[0].capability == "entities.item.create"


def test_trace_rejects_unknown_case_plan_test_schema_issue_and_evidence() -> None:
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(
            trace_fixture(),
            context={**catalog_context(), "case_ids": frozenset({"TC_OTHER"})},
        )
    payload = trace_fixture()
    payload["rows"][0]["plan_id"] = "missing-plan"  # type: ignore[index]
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(payload, context=catalog_context())
    payload = trace_fixture()
    payload["rows"][0]["covering_tests"] = (  # type: ignore[index]
        {"file": "tests/api/test_menu.py", "test_name": "test_missing"},
    )
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(payload, context=catalog_context())
    payload = trace_fixture()
    payload["rows"][0]["schema_id"] = "assurance.quality.schema.missing.v1"  # type: ignore[index]
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(payload, context=catalog_context())
    payload = trace_fixture()
    payload["rows"][0]["issue_ids"] = ("ISS-MISSING",)  # type: ignore[index]
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(payload, context=catalog_context())
    payload = trace_fixture()
    payload["rows"][0]["evidence_refs"] = ("sha256:" + ("b" * 64),)  # type: ignore[index]
    with pytest.raises(ValidationError, match="is not a frozen catalog member"):
        TraceProjectionV2.model_validate(payload, context=catalog_context())


def test_quality_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.quality.schema.fact-baseline.v1") == canonical_json_bytes(
        cast(JSONValue, FactBaselineAuthoring.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.trace.v2") == canonical_json_bytes(
        cast(JSONValue, TraceProjectionV2.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.coverage-gaps.v1") == canonical_json_bytes(
        cast(JSONValue, CoverageGapsDocument.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.minimum-coverage.v1") == canonical_json_bytes(
        cast(JSONValue, MinimumCoverageResult.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.issues.v1") == canonical_json_bytes(
        cast(JSONValue, ChangeIssueSnapshot.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.issue-events.v1") == canonical_json_bytes(
        cast(JSONValue, CHANGE_ISSUE_EVENT_ADAPTER.json_schema())
    )
    assert schema_bytes("assurance.quality.schema.metrics.v1") == canonical_json_bytes(
        cast(JSONValue, MetricsDocument.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.coverage-diff.v1") == canonical_json_bytes(
        cast(JSONValue, CoverageDiffEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.constraint-coverage.v1") == canonical_json_bytes(
        cast(JSONValue, ConstraintCoverageEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.auth-matrix.v1") == canonical_json_bytes(
        cast(JSONValue, AuthMatrixEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.journey-coverage.v1") == canonical_json_bytes(
        cast(JSONValue, JourneyCoverageEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.perf-slack.v1") == canonical_json_bytes(
        cast(JSONValue, PerfSlackEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.mutation.v1") == canonical_json_bytes(
        cast(JSONValue, MutationEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.assertion-strength.v1") == canonical_json_bytes(
        cast(JSONValue, AssertionStrengthEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.baseline-drift.v1") == canonical_json_bytes(
        cast(JSONValue, BaselineDriftEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.adversarial-yield.v1") == canonical_json_bytes(
        cast(JSONValue, AdversarialYieldEvidence.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.c-layer.v1") == canonical_json_bytes(
        cast(JSONValue, CLayerMetricsDocument.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.quarantine.v1") == canonical_json_bytes(
        cast(JSONValue, QuarantineProjection.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.sufficiency.v2") == canonical_json_bytes(
        cast(JSONValue, SufficiencyReportV2.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.trace-sufficiency.v1") == canonical_json_bytes(
        cast(JSONValue, TraceSufficiencyFacts.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.quality-gate.v2") == canonical_json_bytes(
        cast(JSONValue, QualityGateResultV2.model_json_schema())
    )
    assert schema_bytes("assurance.quality.schema.report.v1") == canonical_json_bytes(
        cast(JSONValue, QualityReport.model_json_schema())
    )


def test_quality_contracts_import_only_upstream_public_contracts() -> None:
    assert forbidden_quality_imports() == set()


def test_quality_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_quality.contracts.workflow import AGENT_JOB_CONTRACTS

    expected = {
        "fact-baseline": ("aa-fact-baseline", "assurance-v1-doc-author"),
        "inspect": ("aa-inspect", "assurance-v1-reviewer"),
        "issue-analysis": ("aa-issue-analyzer", "assurance-v1-reporter"),
        "issue-triage": ("aa-issue-triage-advisor", "assurance-v1-reporter"),
        "report": ("aa-report-generator", "assurance-v1-reporter"),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 5
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    for base, (skill_id, agent_profile) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.quality.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        dumped = contract.model_dump_json().lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped
