from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.phase4.conformance import execute_task

from assurance_agent.artifacts.canonical import canonical_json_bytes as legacy_canonical_json_bytes
from assurance_agent.artifacts.canonical import sha256_bytes as legacy_sha256_bytes
from assurance_agent.artifacts.models.improvements import ImprovementCandidate as LegacyCandidate
from assurance_agent.artifacts.models.improvements import ImprovementState as LegacyState
from assurance_agent.retro.assemble import assemble_context as legacy_assemble_context
from assurance_agent.workflow.improvements.identity import (
    improvement_event_id as legacy_event_id,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_fingerprint as legacy_fingerprint,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_id_for_fingerprint as legacy_id_for_fingerprint,
)
from assurance_agent.workflow.improvements.review import REVIEW_ACTIONS as LEGACY_REVIEW_ACTIONS
from assurance_agent.workflow.improvements.transitions import (
    assert_improvement_transition as legacy_assert_transition,
)
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.contracts.improvements import ImprovementCandidate, ImprovementState
from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
from assurance_improvement.effects.store import InMemoryImprovementStore
from assurance_improvement.operations.archive import ProjectArchiveInput, project_archive
from assurance_improvement.operations.delivery import RollbackMemoryImprovementHandler
from assurance_improvement.operations.keys import (
    archive_effect_key,
    delivery_effect_key,
    improvement_event_id,
    improvement_fingerprint,
    improvement_id_for_fingerprint,
    promotion_effect_key,
)
from assurance_improvement.operations.retro import AssembleRetroInput, assemble_context
from assurance_improvement.operations.review import REVIEW_ACTIONS, apply_review
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    ARCHIVE_KEY,
    CHANGE_ID,
    DELIVERY_KEY,
    HEX_A,
    IMPROVEMENT_ID,
    PROMOTION_KEY,
    RETRO_ID,
    archive_intent,
    as_object,
    candidate_payload,
    delivery_intent,
    json_value,
    production_import_roots,
    promotion_intent,
    quality_report_payload,
)


def test_production_improvement_does_not_import_legacy_or_runtimes() -> None:
    names = production_import_roots()
    assert "assurance_agent" not in names
    assert "assurance_kernel" not in names
    assert "agent_runtime_opencode" not in names
    assert "agent_runtime_cursor" not in names


def test_identity_formulas_match_legacy() -> None:
    payload = {key: value for key, value in candidate_payload().items() if key != "signal_ids"}
    current = ImprovementCandidate.model_validate(payload)
    legacy = LegacyCandidate.model_validate(payload)
    fingerprint = improvement_fingerprint(current)
    assert fingerprint == legacy_fingerprint(legacy)
    assert improvement_id_for_fingerprint(fingerprint) == legacy_id_for_fingerprint(fingerprint)
    assert improvement_event_id(RETRO_ID, "improvement_proposed", 1) == legacy_event_id(
        RETRO_ID, "improvement_proposed", 1
    )


def test_review_actions_and_transitions_match_legacy() -> None:
    assert REVIEW_ACTIONS == LEGACY_REVIEW_ACTIONS
    from assurance_improvement.operations.review import ApplyReviewInput

    projection = {
        "improvement_id": IMPROVEMENT_ID,
        "fingerprint": "f" * 64,
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": "proposed",
        "version": 1,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
    }
    from assurance_improvement.contracts.improvements import ImprovementProjection

    current = ImprovementProjection.model_validate(projection)
    updated = apply_review(
        ApplyReviewInput(
            projection=current,
            action="approve",
            review_id="REV-1",
            expected_improvement_version=1,
        )
    )
    assert updated.state is ImprovementState.APPROVED
    legacy_assert_transition(LegacyState.PROPOSED, LegacyState.APPROVED)


def test_assembled_context_and_signals_match_legacy(tmp_path: Path) -> None:
    window = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": [CHANGE_ID]}
    source = {
        "kind": "project_problem_ledger",
        "change_id": None,
        "head_event_id": "evt-1",
        "sha256": "abc",
        "evidence_ids": ["PROB-1", "OCC-1"],
    }
    slices = {
        domain: {
            "schema_version": "3",
            "retro_id": RETRO_ID,
            "domain": domain,
            "window": window,
            "sources": [source] if domain == "issue" else [],
            "integrity": {"status": "complete", "reasons": []},
            "deterministic_signals": [],
            "entries": [],
        }
        for domain in ("issue", "workflow", "eval")
    }
    retro_dir = tmp_path / "qa" / "retro" / RETRO_ID
    (retro_dir / "evidence").mkdir(parents=True)
    (retro_dir / "signals").mkdir(parents=True)
    (retro_dir / "window.json").write_bytes(legacy_canonical_json_bytes(window))
    digests: dict[str, str] = {}
    for domain, payload in slices.items():
        slice_bytes = legacy_canonical_json_bytes(payload)
        (retro_dir / "evidence" / f"{domain}-slice.json").write_bytes(slice_bytes)
        digest = legacy_sha256_bytes(slice_bytes)
        digests[domain] = digest
        signal = {
            "schema_version": "3",
            "retro_id": RETRO_ID,
            "domain": domain,
            "analysis_status": "ok",
            "failure_reason": None,
            "analyzer": f"aa-retro-{domain}-analysis",
            "signals": [],
            "slice_sha256": digest,
        }
        (retro_dir / "signals" / f"{domain}.json").write_bytes(legacy_canonical_json_bytes(signal))
    generated_at = datetime(2026, 8, 22, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy = legacy_assemble_context(retro_dir, dry_run=False, now=datetime(2026, 8, 22, tzinfo=UTC))
    current = assemble_context(
        AssembleRetroInput.model_validate(
            {
                "generated_at": generated_at,
                "window": window,
                "issue_slice": slices["issue"],
                "workflow_slice": slices["workflow"],
                "eval_slice": slices["eval"],
                "issue_signals": {
                    "schema_version": "3",
                    "retro_id": RETRO_ID,
                    "domain": "issue",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-issue-analysis",
                    "signals": [],
                    "slice_sha256": digests["issue"],
                },
                "workflow_signals": {
                    "schema_version": "3",
                    "retro_id": RETRO_ID,
                    "domain": "workflow",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-workflow-analysis",
                    "signals": [],
                    "slice_sha256": digests["workflow"],
                },
                "eval_signals": {
                    "schema_version": "3",
                    "retro_id": RETRO_ID,
                    "domain": "eval",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-eval-analysis",
                    "signals": [],
                    "slice_sha256": digests["eval"],
                },
                "issue_slice_sha256": digests["issue"],
                "workflow_slice_sha256": digests["workflow"],
                "eval_slice_sha256": digests["eval"],
            }
        )
    )
    assert current.signals.model_dump(mode="json") == legacy.signals.model_dump(mode="json")
    assert current.domain_status.model_dump(mode="json") == legacy.domain_status.model_dump(mode="json")
    assert current.signal_count == legacy.signal_count == 0
    assert current.source_manifest.resolvable_ids() == legacy.source_manifest.resolvable_ids()


def test_candidate_identity_matches_legacy_fingerprint() -> None:
    payload = candidate_payload()
    current = ImprovementCandidate.model_validate({k: v for k, v in payload.items() if k != "signal_ids"})
    legacy = LegacyCandidate.model_validate({k: v for k, v in payload.items() if k != "signal_ids"})
    assert current.kind.value == legacy.kind.value
    assert current.source_refs.problem_ids == legacy.source_refs.problem_ids
    assert improvement_fingerprint(current) == legacy_fingerprint(legacy)


def test_effect_keys_and_receipts_match_frozen_formulas() -> None:
    delivery = ImprovementEffectIntentV1.model_validate(delivery_intent().payload)
    promotion = ImprovementEffectIntentV1.model_validate(promotion_intent().payload)
    archive = ImprovementEffectIntentV1.model_validate(archive_intent().payload)
    assert delivery_effect_key(delivery) == DELIVERY_KEY
    assert promotion_effect_key(promotion) == PROMOTION_KEY
    assert archive_effect_key(archive) == ARCHIVE_KEY


async def test_rollback_output_is_explicit_delivery_memory_rollback() -> None:
    outcome = await execute_task(
        RollbackMemoryImprovementHandler(),
        json_value(
            {
                "projection": {
                    "improvement_id": IMPROVEMENT_ID,
                    "fingerprint": "f" * 64,
                    "kind": "prompt_improvement",
                    "delivery": "memory_patch",
                    "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
                    "target": ".aa/memory/aa-api-plan.md",
                    "rationale": "gap",
                    "proposed_change": "register adapters",
                    "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
                    "risk": "low",
                    "confidence": "high",
                    "state": "approved",
                    "version": 1,
                    "proposed_by_retro_ids": ["RET-1"],
                    "last_event_id": "IMPEVT-1",
                },
                "reason": "regressed",
                "restored_sha256": "x",
                "target_digest": HEX_A,
            }
        ),
    )
    payload = as_object(outcome.output)
    assert payload["reason"] == "regressed"
    assert payload["target"] == ".aa/memory/aa-api-plan.md"
    assert as_object(outcome.effects[0].payload)["kind"] == "memory_rollback"
    applied = await ImprovementDeliveryEffect(store=InMemoryImprovementStore()).apply(
        outcome.effects[0],
        f"{IMPROVEMENT_ID}:1:memory_rollback:{HEX_A}",
    )
    assert applied.status == "applied"
    assert as_object(applied.receipt)["kind"] == "memory_rollback"


def test_archive_summary_warns_on_non_clear_issue_risk() -> None:
    projection = project_archive(
        ProjectArchiveInput.model_validate(
            {
                "change_id": CHANGE_ID,
                "invocation_id": "inv-archive-1",
                "archive_digest": HEX_A,
                "report": quality_report_payload(issue_risk="high"),
                "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
            }
        )
    )
    assert projection["archive_status"] == "archived_with_warnings"
    assert projection["issue_risk"] == "high"
    assert "issue_risk: high" in str(projection["summary"])
    assert f"# Archive {CHANGE_ID}" in str(projection["summary"])
