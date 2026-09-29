from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

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
    TraceProjectionDocument,
    TraceProjectionV2,
    TraceSufficiencyFacts,
)
from assurance_quality.contracts.issue_events import CHANGE_ISSUE_EVENT_ADAPTER
from assurance_quality.contracts.issues import IssueReconcileStatusDocument, IssueReconcileStatusV2
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.obligations import ObligationAssessmentV1
from assurance_quality.plugin import QualityPlugin
from tests.capabilities.import_boundary_exceptions import is_declared_cross_wheel_import

_CURRENT_QUALITY_SCHEMA_MAPPING: dict[str, tuple[str, str]] = {
    "assurance.quality.schema.adversarial-yield.v1": (
        "1",
        "0325a4b0ef8fe88bb53e753679350a9779423390cb3ce7c37d66f8747dd86cc0",
    ),
    "assurance.quality.schema.assertion-strength.v1": (
        "1",
        "c3401289756d52af7cdd504cbdf69be511ecc2365ca19bbe5ed1452191126840",
    ),
    "assurance.quality.schema.auth-matrix.v1": (
        "1",
        "d710cab2ea180d5362d06b3c1a79ebe6c51515c509f537880a964754813f3acf",
    ),
    "assurance.quality.schema.baseline-drift.v1": (
        "1",
        "92b3891c54c845dbcb14c0f93f73012f3dbfcf13d72722552b4ec6594ba8a734",
    ),
    "assurance.quality.schema.c-layer.v1": (
        "1",
        "da38172ed9391601982d3170df4473da58fff4cc3d48263c806e4c8f015b3a42",
    ),
    "assurance.quality.schema.constraint-coverage.v1": (
        "1",
        "04bb2a00ec337aa3d26f2067dc8b2772f1cf7a912814763ac32423fc917444f2",
    ),
    "assurance.quality.schema.coverage-diff.v1": (
        "1",
        "e2f71220dc220e3d38a790dcd9a55388938ea846029bbcc0a042d9cff05ff630",
    ),
    "assurance.quality.schema.coverage-gaps.v1": (
        "1",
        "268de79665c17e117d4aa69d96ab0d14610d6f6952edf5518d30aaffb1231262",
    ),
    "assurance.quality.schema.fact-baseline.v1": (
        "1",
        "5744f50235e32defa045ded5745722b48a3b3f6ba1c2736ec374785d0b2d214a",
    ),
    "assurance.quality.schema.issue-events.v1": (
        "1",
        "45f09cb6f357301494eb75995c3bef7b20cf64d9bf6e464b0724dececc74e3c7",
    ),
    "assurance.quality.schema.issues.v1": (
        "1",
        "49728b682d1ec26d4efdeea795f6ee0d044c4454f64b467d1693c8eb07e5bda1",
    ),
    "assurance.quality.schema.journey-coverage.v1": (
        "1",
        "1216ef3605574b10444f412d417d0d723ddb8de578c03f4bef46211fb90895cb",
    ),
    "assurance.quality.schema.metrics.v1": (
        "1",
        "a20c5bcfb54d3d4daf548939c5079f174c05caba80fd6821700de4edb09f9381",
    ),
    "assurance.quality.schema.minimum-coverage.v1": (
        "1",
        "aef09f2ab32f91700903a73424e4c9274391dbb48297d4ad59c39ecba7854fe8",
    ),
    "assurance.quality.schema.obligation-assessment.v1": (
        "1",
        "8bae0fcf3ee607cff065865a594a27f718babd7671bbad2b3bad0473bb1ff500",
    ),
    "assurance.quality.schema.mutation.v1": (
        "1",
        "77cd051117226b66d600b949447386a1382ec7a6495510d5449e61d123c1fb79",
    ),
    "assurance.quality.schema.perf-slack.v1": (
        "1",
        "4cf76def638529a39479a74f0990d8f87c78885335f2701f3f0282c79c302246",
    ),
    "assurance.quality.schema.quality-gate.v2": (
        "1",
        "1a5a208dfb92b9a506f5daf8ccb4e84d135f715fa720661cdd662f6534e90898",
    ),
    "assurance.quality.schema.quarantine.v1": (
        "1",
        "ef9fae65a7b56611624cd884a2a09c38a7edd2d804daf2e924e2443f5bfc19eb",
    ),
    "assurance.quality.schema.report.v1": (
        "1",
        "edddd18d71cf091b561c504f6077a3d23fff4a6fc464741d22f556afa2f95a11",
    ),
    "assurance.quality.schema.sufficiency.v2": (
        "1",
        "db8c82d19c0a603f61ed91a02bd47a080af065df7e34ea2c1b06cfb3a0603186",
    ),
    "assurance.quality.schema.trace-sufficiency.v1": (
        "1",
        "48df3933626849c974b5108ff53f40d16b7a455bf42c894fd2cfdb398f8385f3",
    ),
    "assurance.quality.schema.trace.v2": (
        "1",
        "f1c5b0b1a3fcce64abaade2c2f90acadd03adda6fd7de231639e2d3365590e45",
    ),
    "assurance.quality.workflow.assess.input.v1": (
        "1",
        "1082fea0fd50a41ccf4ecfa10d51b07bf13d8817af26e90aef0e4df927c95028",
    ),
    "assurance.quality.workflow.assess.output.v1": (
        "1",
        "b454175dbb5cd1f283affabfd4f238692cf3c98d76af8cc856ee4cecd3edbfa3",
    ),
    "assurance.quality.workflow.issue-analyze.input.v1": (
        "1",
        "7ca56fec95bf59f2996692db50878334fd4f3f22049e675b29bf1530b05b4b00",
    ),
    "assurance.quality.workflow.issue-analyze.output.v1": (
        "1",
        "c294931c8f49f34abb6ae7c5f2cd2db0b2167accaf3eaf6cd0f1646048a59d53",
    ),
    "assurance.quality.workflow.issue-reconcile.input.v1": (
        "1",
        "7ca56fec95bf59f2996692db50878334fd4f3f22049e675b29bf1530b05b4b00",
    ),
    "assurance.quality.workflow.issue-reconcile.output.v1": (
        "1",
        "c294931c8f49f34abb6ae7c5f2cd2db0b2167accaf3eaf6cd0f1646048a59d53",
    ),
    "assurance.quality.workflow.issue-review.input.v1": (
        "1",
        "7ca56fec95bf59f2996692db50878334fd4f3f22049e675b29bf1530b05b4b00",
    ),
    "assurance.quality.workflow.issue-review.output.v1": (
        "1",
        "c294931c8f49f34abb6ae7c5f2cd2db0b2167accaf3eaf6cd0f1646048a59d53",
    ),
    "assurance.quality.workflow.report.input.v1": (
        "1",
        "7d0088873d1a5935243f3cfb071a58b9df8bb868927f3fd4027e3678cb46df95",
    ),
    "assurance.quality.workflow.report.output.v1": (
        "1",
        "878092885436e105a48e0478983f4d6799a350e29a8b1452aea1fa72acca2706",
    ),
}

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
            if is_declared_cross_wheel_import(root, path, module_name):
                continue
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


def _installed_schema_mapping() -> dict[str, tuple[str, str]]:
    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    return {
        schema.schema_id: (
            "1",
            canonical_digest(cast(JSONValue, json.loads(schema.content))),
        )
        for schema in contribution.schemas
    }


def test_quality_product_lock_schema_mapping_is_current_only() -> None:
    assert _installed_schema_mapping() == _CURRENT_QUALITY_SCHEMA_MAPPING


def test_exclusive_document_versions_are_locked_on_models() -> None:
    # SchemaContribution has no per-schema version field; exclusive document
    # versions live on the Pydantic models themselves.
    assert TraceProjectionV2.model_fields["schema_version"].annotation == Literal["2"]
    assert TraceProjectionDocument.model_fields["root"].annotation is TraceProjectionV2
    assert IssueReconcileStatusV2.model_fields["schema_version"].annotation == Literal["2.0"]
    assert IssueReconcileStatusDocument.model_fields["root"].annotation is IssueReconcileStatusV2
    assert QualityReport.model_fields["schema_version"].annotation == Literal["1.1"]


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


def test_fact_baseline_authoring_forbids_historical_extra_fields() -> None:
    with pytest.raises(ValidationError):
        FactBaselineAuthoring.model_validate(
            {
                "source": "seed_file",
                "schema_version": "1",
                "warnings": [],
                "legacy_inventory": True,
            }
        )


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
    assert schema_bytes("assurance.quality.schema.obligation-assessment.v1") == canonical_json_bytes(
        cast(JSONValue, ObligationAssessmentV1.model_json_schema())
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

    from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "fact-baseline": (
            "aa-fact-baseline",
            "assurance-v1-doc-author",
            ("qa/results/facts/fact-baseline.json",),
        ),
        "inspect": (
            "aa-inspect",
            "assurance-v1-reviewer",
            ("qa/results/inspect/inspection.json",),
        ),
        "issue-analysis": (
            "aa-issue-analyzer",
            "assurance-v1-reporter",
            ("qa/results/inspect/issue-analysis.json",),
        ),
        "issue-triage": (
            "aa-issue-triage-advisor",
            "assurance-v1-reporter",
            ("qa/results/inspect/issue-triage.json",),
        ),
        "report": (
            "aa-report-generator",
            "assurance-v1-reporter",
            ("qa/results/report/report.md",),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 5
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.quality.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        assert contract.resources.writes == writes
        assert OUTPUT_ROUTE_TEMPLATES[base] == writes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped


def test_output_routes_are_flat_qa_paths() -> None:
    from assurance_quality.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    rendered = "\n".join(path for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
    assert "qa/" + "changes" not in rendered
    assert "{change_id}" not in rendered
    assert "qa/" + "archive" not in rendered
    assert all(path.startswith("qa/") for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
