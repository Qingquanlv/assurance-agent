from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_improvement.contracts import (
    DeclarationProposal,
    ImprovementCandidateDocumentV3,
    ImprovementEffectIntentV1,
    ImprovementEffectReceiptV1,
    ImprovementDeliveryDocument,
    ImprovementReviewSubject,
    RetroContextV3,
    SignalDocumentV3,
    TestPromotionManifest,
)
from assurance_improvement.contracts.retro import RetroSourceManifestV3
from assurance_improvement.plugin import ImprovementPlugin
from tests.capabilities.import_boundary_exceptions import is_declared_cross_wheel_import

_WHEEL_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_ALLOWED_ASSURANCE = (
    "assurance_intake.contracts",
    "assurance_generation.contracts",
    "assurance_execution.contracts",
    "assurance_healing.contracts",
    "assurance_quality.contracts",
)

REFS = {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]}

ISSUE_SIGNAL = {
    "signal_id": "issue-pattern:l1_knowledge_missing_symbol",
    "summary": "Existing user/role adapters are absent from L1 capability registry",
    "occurrence_count": 3,
    "recommended_change": "Register the existing adapter and factory symbols",
    "source_refs": REFS,
    "confidence": "high",
    "signal_type": "issue_pattern",
    "pattern_kind": "knowledge_gap",
    "affected_surface": {"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
    "symptom": "Existing user/role adapters are absent from L1 capability registry",
}


def authenticated_manifest() -> dict[str, object]:
    return {
        "issue_slice_sha256": "a",
        "workflow_slice_sha256": "b",
        "eval_slice_sha256": "c",
        "issue_sources": [
            {
                "kind": "project_problem_ledger",
                "change_id": None,
                "head_event_id": "evt-1",
                "sha256": "abc",
                "evidence_ids": ["PROB-1", "OCC-1"],
            }
        ],
        "workflow_sources": [],
        "eval_sources": [],
    }


def candidate_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": "C-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": REFS,
        "target": "assurance_agent/_resources/schemas/",
        "rationale": "L1 registry gaps repeatedly block codegen",
        "proposed_change": "Register existing adapter symbols",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "manual review"},
        "risk": "low",
        "confidence": "high",
        "signal_ids": ["issue-pattern:l1_knowledge_missing_symbol"],
    }
    payload.update(overrides)
    if payload.get("signal_ids") is None:
        payload.pop("signal_ids")
    return payload


def candidate_document_payload(*, candidates: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": "RET-1",
        "context_sha256": "ctx",
        "candidates": candidates if candidates is not None else [candidate_payload()],
    }


def candidate_with_unknown_source() -> dict[str, object]:
    return candidate_document_payload(
        candidates=[candidate_payload(source_refs={"problem_ids": ["PROB-UNKNOWN"]})],
    )


def retro_manifest_context() -> dict[str, object]:
    return {"retro_manifest": RetroSourceManifestV3.model_validate(authenticated_manifest())}


def schema_bytes(schema_id: str) -> bytes:
    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_improvement_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_improvement"
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
                or module_name.startswith("assurance_quality.")
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
                "assurance_quality",
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


def test_improvement_descriptor_declares_all_upstream_in_order() -> None:
    assert tuple(item.plugin_id for item in ImprovementPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
    )


def test_retro_candidate_requires_authenticated_source_membership() -> None:
    with pytest.raises(ValidationError, match="candidate source is outside the retro manifest"):
        ImprovementCandidateDocumentV3.model_validate(candidate_with_unknown_source())


def test_retro_candidate_rejects_unknown_source_even_with_manifest() -> None:
    with pytest.raises(ValidationError, match="candidate source is outside the retro manifest"):
        ImprovementCandidateDocumentV3.model_validate(
            candidate_with_unknown_source(),
            context=retro_manifest_context(),
        )


def test_retro_candidate_accepts_authenticated_source_membership() -> None:
    document = ImprovementCandidateDocumentV3.model_validate(
        candidate_document_payload(),
        context=retro_manifest_context(),
    )
    assert document.candidates[0].source_refs.problem_ids == ("PROB-1",)


def test_candidate_requires_at_least_one_source_ref() -> None:
    from assurance_improvement.contracts import ImprovementCandidate

    with pytest.raises(ValidationError, match="candidate requires at least one source ref"):
        ImprovementCandidate.model_validate(candidate_payload(source_refs={}, signal_ids=None))


def test_knowledge_delta_required_only_for_knowledge_delta_delivery() -> None:
    from assurance_improvement.contracts import ImprovementCandidate

    delta = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"required_fields": ["name"]}},
    }
    with pytest.raises(ValidationError, match="knowledge_delta payload is required only"):
        ImprovementCandidate.model_validate(
            candidate_payload(kind="domain_knowledge", delivery="knowledge_delta", signal_ids=None)
        )
    with pytest.raises(ValidationError, match="knowledge_delta payload is required only"):
        ImprovementCandidate.model_validate(candidate_payload(knowledge_delta=delta, signal_ids=None))
    candidate = ImprovementCandidate.model_validate(
        candidate_payload(
            kind="domain_knowledge",
            delivery="knowledge_delta",
            knowledge_delta=delta,
            signal_ids=None,
        )
    )
    assert candidate.knowledge_delta is not None


def test_candidate_v3_requires_signal_ids() -> None:
    from assurance_improvement.contracts import ImprovementCandidateV3

    payload = candidate_payload()
    payload.pop("signal_ids")
    with pytest.raises(ValidationError):
        ImprovementCandidateV3.model_validate(payload)


def test_draft_candidate_document_rejects_context_sha256() -> None:
    from assurance_improvement.contracts import ImprovementCandidateDocumentDraftV3

    with pytest.raises(ValidationError):
        ImprovementCandidateDocumentDraftV3.model_validate(
            {"schema_version": "3", "retro_id": "RET-1", "candidates": [], "context_sha256": "x"}
        )


def test_signal_draft_rejects_slice_sha256() -> None:
    from assurance_improvement.contracts import SignalDraftDocument

    with pytest.raises(ValidationError):
        SignalDraftDocument.model_validate(
            {
                "schema_version": "3",
                "retro_id": "RET-1",
                "domain": "issue",
                "analysis_status": "ok",
                "failure_reason": None,
                "analyzer": "aa-retro-issue-analysis",
                "signals": [ISSUE_SIGNAL],
                "slice_sha256": "abc",
            }
        )


def test_failed_signal_document_requires_reason_and_no_signals() -> None:
    from assurance_improvement.contracts import SignalDraftDocument

    draft = {
        "schema_version": "3",
        "retro_id": "RET-1",
        "domain": "issue",
        "analysis_status": "failed",
        "failure_reason": None,
        "analyzer": "aa-retro-issue-analysis",
        "signals": [],
    }
    with pytest.raises(ValidationError, match="failure_reason is required"):
        SignalDraftDocument.model_validate(draft)
    with pytest.raises(ValidationError, match="must not carry signals"):
        SignalDraftDocument.model_validate({**draft, "failure_reason": "timeout", "signals": [ISSUE_SIGNAL]})


def test_domain_status_reason_consistency() -> None:
    from assurance_improvement.contracts import DomainAnalysisStatus

    with pytest.raises(ValidationError, match="failure_reason is required"):
        DomainAnalysisStatus.model_validate({"status": "failed", "failure_reason": None})
    with pytest.raises(ValidationError, match="failure_reason must be null"):
        DomainAnalysisStatus.model_validate({"status": "ok", "failure_reason": "boom"})


def test_memory_patch_target_must_be_child_of_aa_memory() -> None:
    from assurance_improvement.contracts import ImprovementCandidate

    with pytest.raises(ValidationError, match="memory_patch target must be a child path under .aa/memory/"):
        ImprovementCandidate.model_validate(
            candidate_payload(
                kind="prompt_improvement",
                delivery="memory_patch",
                target=".aa/memory",
                signal_ids=None,
            )
        )


def test_prompt_cannot_use_change_draft_delivery() -> None:
    from assurance_improvement.contracts import ImprovementCandidate

    with pytest.raises(ValidationError, match="cannot use"):
        ImprovementCandidate.model_validate(
            candidate_payload(kind="prompt_improvement", delivery="change_draft", signal_ids=None)
        )


def test_improvement_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.improvement.schema.retro-context.v3") == canonical_json_bytes(
        cast(JSONValue, RetroContextV3.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.retro-signals.v3") == canonical_json_bytes(
        cast(JSONValue, SignalDocumentV3.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.improvement-candidates.v3") == canonical_json_bytes(
        cast(JSONValue, ImprovementCandidateDocumentV3.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.improvement-review.v1") == canonical_json_bytes(
        cast(JSONValue, ImprovementReviewSubject.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.improvement-delivery.v1") == canonical_json_bytes(
        cast(JSONValue, ImprovementDeliveryDocument.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.promotion.v1") == canonical_json_bytes(
        cast(JSONValue, TestPromotionManifest.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.declaration-proposal.v1") == canonical_json_bytes(
        cast(JSONValue, DeclarationProposal.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.improvement-effect-intent.v1") == canonical_json_bytes(
        cast(JSONValue, ImprovementEffectIntentV1.model_json_schema())
    )
    assert schema_bytes("assurance.improvement.schema.improvement-effect-receipt.v1") == canonical_json_bytes(
        cast(JSONValue, ImprovementEffectReceiptV1.model_json_schema())
    )


def test_improvement_contracts_import_only_upstream_public_contracts() -> None:
    assert forbidden_improvement_imports() == set()


def test_improvement_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "archive": (
            "aa-archive",
            "assurance-v1-archiver",
            ("qa/results/archive/archive-receipt.json",),
        ),
        "improvement-review": (
            "aa-improvement-reviewer",
            "assurance-v1-reviewer",
            ("qa/results/review/improvement-review.json",),
        ),
        "retro-eval-analysis": (
            "aa-retro-eval-analysis",
            "assurance-v1-doc-author",
            ("qa/results/retro/retro-eval-analysis.json",),
        ),
        "retro-issue-analysis": (
            "aa-retro-issue-analysis",
            "assurance-v1-doc-author",
            ("qa/results/retro/retro-issue-analysis.json",),
        ),
        "retro-workflow-analysis": (
            "aa-retro-workflow-analysis",
            "assurance-v1-doc-author",
            ("qa/results/retro/retro-workflow-analysis.json",),
        ),
        "retro": (
            "aa-retro",
            "assurance-v1-doc-author",
            ("qa/results/retro/retro.json",),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 6
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.improvement.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        assert contract.resources.writes == writes
        assert OUTPUT_ROUTE_TEMPLATES[base] == writes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped


def test_output_routes_are_flat_qa_paths() -> None:
    from assurance_improvement.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    rendered = "\n".join(path for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)
    assert "qa/" + "changes" not in rendered
    assert "{change_id}" not in rendered
    assert "qa/" + "archive" not in rendered
    assert all(path.startswith("qa/") for paths in OUTPUT_ROUTE_TEMPLATES.values() for path in paths)


def test_delivery_receipts_are_data_only_shapes() -> None:
    from assurance_improvement.contracts import (
        ChangeExportReceipt,
        KnowledgeExportReceipt,
        MemoryApplyReceipt,
        MemoryEvalReceipt,
        MemoryRollbackReceipt,
    )

    assert (
        ChangeExportReceipt.model_validate(
            {"sha256": "abc", "created": True, "artifact_path": "qa/improvements/drafts/IMP-1.yaml"}
        ).created
        is True
    )
    assert (
        KnowledgeExportReceipt.model_validate(
            {
                "sha256": "abc",
                "created": False,
                "artifact_path": "qa/improvements/knowledge-delta/IMP-1.proposal.yaml",
            }
        ).created
        is False
    )
    eval_receipt = MemoryEvalReceipt.model_validate(
        {
            "eval_run_id": "eval-1",
            "outcome": "passed",
            "report_sha256": "r",
            "staged_sha256": "s",
            "baseline_sha256": None,
        }
    )
    assert eval_receipt.outcome == "passed"
    apply_receipt = MemoryApplyReceipt.model_validate(
        {
            "target": ".aa/memory/aa-api-plan.md",
            "before_sha256": "b",
            "after_sha256": "a",
            "receipt_sha256": "r",
        }
    )
    assert apply_receipt.target.startswith(".aa/memory/")
    rollback = MemoryRollbackReceipt.model_validate(
        {"target": ".aa/memory/aa-api-plan.md", "restored_sha256": "x", "reason": "regressed"}
    )
    assert rollback.reason == "regressed"
