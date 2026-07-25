"""TDD tests for MemoryPatchDelivery evaluate / apply / rollback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.improvements import (
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.memory_delivery import (
    ImprovementDeliveryError,
    MemoryPatchDelivery,
    sha256_bytes,
)


IMP_ID = "IMP-MEM000000000000001"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed_approved(project: Path, *, state_events: list[dict] | None = None) -> ImprovementProjection:
    store = ProjectImprovementStore(project)
    events = [
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "IMPEVT-PROP",
            "idempotency_key": "IDEM-PROP",
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": "a" * 64,
            "fingerprint_version": "1",
            "kind": "prompt_improvement",
            "delivery": "memory_patch",
            "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
            "target": ".aa/memory/aa-run.md",
            "rationale": "Remember fixture rule",
            "proposed_change": "always seed department name",
            "verification": {
                "suites": ["workflow-run"],
                "success_criteria": "eval passes",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": "retro-1",
            "candidate_id": "C-1",
            "context_sha256": "c" * 64,
            "candidate_batch_digest": "d" * 64,
        },
        {
            "schema_version": "1.0",
            "seq": 2,
            "event_id": "IMPEVT-APP",
            "idempotency_key": "IDEM-APP",
            "ts": "2026-07-26T00:01:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 1,
            "type": "improvement_review_approved",
            "who": "reviewer",
            "reason": "ok",
            "review_id": "REV-1",
        },
    ]
    if state_events:
        events.extend(state_events)
    store.append_and_rebuild(
        [IMPROVEMENT_EVENT_ADAPTER.validate_python(item) for item in events]
    )
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    return ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])


def _passing_runner(calls: list):
    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        calls.append({"suite": suite, "extra": extra_memory_dir})
        run_id = f"eval-{len(calls)}"
        run = Path(sut_dir) / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True)
        (run / "metrics.json").write_text(
            json.dumps({"run_id": run_id, "suite": suite, "metrics": {"evidence_integrity": 1.0}}),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "verdict": "pass",
            "metrics": {"evidence_integrity": 1.0},
            "hard_gate_failures": [],
            "suite_contract": {
                "thresholds": [{"metric": "evidence_integrity", "gate": "hard", "op": "gte", "value": 0.95}]
            },
            "baseline_metrics": {"evidence_integrity": 1.0},
        }

    return eval_runner


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".aa" / "memory").mkdir(parents=True)
    (tmp_path / ".aa" / "memory" / "aa-run.md").write_text("# memory\n", encoding="utf-8")
    return tmp_path


def test_memory_apply_requires_approved_eval_and_pinned_digest(project: Path) -> None:
    improvement = _seed_approved(project)
    delivery = MemoryPatchDelivery(project, eval_runner=_passing_runner([]))
    with pytest.raises(ImprovementDeliveryError, match="successful eval"):
        delivery.apply(improvement, expected_target_sha256=sha256_bytes(b"# memory\n"))


def test_missing_baseline_yields_awaiting_baseline(project: Path) -> None:
    improvement = _seed_approved(project)

    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        return {
            "run_id": "eval-no-base",
            "verdict": "pass",
            "metrics": {"evidence_integrity": 1.0},
            "hard_gate_failures": [],
            "suite_contract": {
                "thresholds": [{"metric": "evidence_integrity", "gate": "hard", "op": "gte", "value": 0.95}]
            },
            "baseline_metrics": None,
        }

    delivery = MemoryPatchDelivery(project, eval_runner=eval_runner)
    receipt = delivery.evaluate(improvement)
    assert receipt.outcome == "awaiting_baseline"
    live = (project / ".aa/memory/aa-run.md").read_text(encoding="utf-8")
    assert live == "# memory\n"
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.AWAITING_BASELINE.value


def test_eval_regression_rolls_back_without_mutating_live_memory(project: Path) -> None:
    improvement = _seed_approved(project)
    original = (project / ".aa/memory/aa-run.md").read_bytes()

    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        return {
            "run_id": "eval-regress",
            "verdict": "pass",
            "metrics": {"evidence_integrity": 0.1},
            "hard_gate_failures": [],
            "suite_contract": {
                "thresholds": [{"metric": "evidence_integrity", "gate": "hard", "op": "gte", "value": 0.95}]
            },
            "baseline_metrics": {"evidence_integrity": 1.0},
        }

    delivery = MemoryPatchDelivery(project, eval_runner=eval_runner)
    receipt = delivery.evaluate(improvement)
    assert receipt.outcome == "regressed"
    assert (project / ".aa/memory/aa-run.md").read_bytes() == original
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.ROLLED_BACK.value


def test_evaluate_then_apply_writes_improvement_marker(project: Path) -> None:
    improvement = _seed_approved(project)
    calls: list = []
    delivery = MemoryPatchDelivery(project, eval_runner=_passing_runner(calls))
    eval_receipt = delivery.evaluate(improvement)
    assert eval_receipt.outcome == "passed"
    assert calls and calls[0]["extra"] is not None

    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    assert current.state is ImprovementState.EVALUATING

    before = sha256_bytes((project / ".aa/memory/aa-run.md").read_bytes())
    apply_receipt = delivery.apply(current, expected_target_sha256=before)
    content = (project / ".aa/memory/aa-run.md").read_text(encoding="utf-8")
    assert f"<!-- improvement:{IMP_ID} evidence:OCC-1,PROB-1 -->" in content
    assert "- always seed department name" in content
    assert "<!-- /improvement -->" in content
    assert apply_receipt.after_sha256 == _sha((project / ".aa/memory/aa-run.md").read_bytes())

    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.APPLIED.value


def test_rollback_validates_applied_digest(project: Path) -> None:
    improvement = _seed_approved(project)
    delivery = MemoryPatchDelivery(project, eval_runner=_passing_runner([]))
    delivery.evaluate(improvement)
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    before = sha256_bytes((project / ".aa/memory/aa-run.md").read_bytes())
    applied = delivery.apply(current, expected_target_sha256=before)

    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    with pytest.raises(ImprovementDeliveryError, match="applied digest"):
        delivery.rollback(current, reason="bad", expected_applied_digest="0" * 64)

    delivery.rollback(
        current, reason="regression found", expected_applied_digest=applied.after_sha256
    )
    content = (project / ".aa/memory/aa-run.md").read_text(encoding="utf-8")
    assert "- deprecated: always seed department name" in content
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.ROLLED_BACK.value
