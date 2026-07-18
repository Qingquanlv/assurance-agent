"""Phase-F gate wiring in ``resume_nightly`` (spec §6 baseline regression policy).

Covers the contract the old uniform ``delta < -0.05`` check violated:
suites without hard gates never auto-apply, a missing baseline is
inconclusive (never applied, resumable after approval), hard-threshold
satisfaction flips roll back, a pass lands exactly once via the marker,
and runner infrastructure errors become ``eval_error`` without blocking
other suite groups.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import yaml

from assurance_agent.retro.nightly.driver import resume_nightly
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE,
    NIGHTLY_OK,
    NIGHTLY_PENDING_REVIEW,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.phase_f import compare_suite_regression
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.promotions import proposal_states, read_promotion_events


def _eval_support_from_disk(engine: Path, suite: str) -> dict:
    suite_contract: dict = {}
    suite_path = engine / "eval" / "suites" / f"{suite}.yaml"
    if suite_path.is_file():
        data = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            suite_contract = data
    baseline_metrics = None
    baseline_provenance: dict = {}
    baseline_path = engine / "eval" / "baselines" / "main.json"
    if baseline_path.is_file():
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
        entry = raw.get(suite) if isinstance(raw, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("metrics"), dict):
            baseline_metrics = entry["metrics"]
            baseline_provenance = {
                "baseline_suite_version": entry.get("suite_version"),
                "baseline_repeat": entry.get("repeat"),
                "baseline_regression_policy_sha256": entry.get("regression_policy_sha256"),
            }
    return {
        "suite_contract": suite_contract,
        "baseline_metrics": baseline_metrics,
        **baseline_provenance,
    }


def _proposal(pid: str, suite: str) -> dict:
    return {
        "id": pid,
        "apply_kind": "memory_append",
        "eval_suite": suite,
        "status": "proposed",
        "target": f".aa/memory/{pid}.md",
        "problem": "x",
        "proposed_change": f"remember {pid}",
        "evidence_ids": ["CH-1#F-1"],
    }


def _seed_promoted(sut: Path, retro_id: str, proposals: list[dict]) -> Path:
    """Seed proposals.json plus a promoted review_decision per proposal."""
    retro = sut / "qa" / "retro" / retro_id
    retro.mkdir(parents=True, exist_ok=True)
    write_json(retro / "proposals.json", {"proposals": proposals})
    write_json(
        retro / "promotions.json",
        {
            "schema_version": "2",
            "events": [
                {
                    "proposal_id": p["id"],
                    "type": "review_decision",
                    "decision": "promoted",
                    "actor": "LQ",
                    "at": "2026-07-17T00:00:00Z",
                }
                for p in proposals
            ],
        },
    )
    return retro


def _seed_suite(engine: Path, suite: str, *, hard_gate: bool = True) -> None:
    suites = engine / "eval" / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    thresholds = (
        "thresholds:\n  - metric: evidence_integrity\n    gate: hard\n    op: gte\n    value: 0.95\n"
        if hard_gate
        else "thresholds: []\n"
    )
    regression = (
        "regression:\n"
        "  repeat: 1\n"
        "  metrics:\n"
        "    evidence_integrity:\n"
        "      direction: higher_is_better\n"
        "      max_regression: 0.0\n"
        if hard_gate
        else ""
    )
    (suites / f"{suite}.yaml").write_text(
        f'name: {suite}\nversion: "1"\nscorer: s\n{regression}{thresholds}',
        encoding="utf-8",
    )


def _policy_hash(engine: Path, suite: str) -> str | None:
    raw = yaml.safe_load((engine / "eval" / "suites" / f"{suite}.yaml").read_text())
    policy = raw.get("regression") if isinstance(raw, dict) else None
    if not isinstance(policy, dict):
        return None
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _seed_baselines(engine: Path, entries: dict[str, float]) -> None:
    baselines = engine / "eval" / "baselines"
    baselines.mkdir(parents=True, exist_ok=True)
    (baselines / "main.json").write_text(
        json.dumps(
            {
                suite: {
                    "run_id": "base",
                    "suite_version": "1",
                    "repeat": 1,
                    "regression_policy_sha256": _policy_hash(engine, suite),
                    "approved_at": "2026-07-16T00:00:00Z",
                    "approved_by": "test",
                    "metrics": {"evidence_integrity": value},
                }
                for suite, value in entries.items()
            }
        ),
        encoding="utf-8",
    )


def _runner(
    calls: list,
    *,
    metrics: dict[str, float] | None = None,
    verdicts: dict[str, str] | None = None,
    hard_gate_failures: dict[str, list[str]] | None = None,
    raises_for: set[str] | None = None,
):  # noqa: ANN202
    """Fake eval_runner; per-suite metric/verdict overrides, default pass@1.0."""

    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        calls.append(suite)
        if raises_for and suite in raises_for:
            raise RuntimeError(f"{suite} infrastructure down")
        metric = (metrics or {}).get(suite, 1.0)
        run_id = f"eval-{suite}-{len(calls)}"
        run = sut_dir / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True, exist_ok=True)
        (run / "metrics.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "suite": suite,
                    "sample_count": 1,
                    "metrics": {"evidence_integrity": metric},
                }
            ),
            encoding="utf-8",
        )
        result = {
            "run_id": run_id,
            "verdict": (verdicts or {}).get(suite, "pass"),
            "metrics": {"evidence_integrity": metric},
            "hard_gate_failures": (hard_gate_failures or {}).get(suite) or [],
            "suite_version": "1",
            "repeat": 1,
            "regression_policy_sha256": _policy_hash(Path(engine_root), suite),
            **_eval_support_from_disk(Path(engine_root), suite),
        }
        return result

    return eval_runner


def _resume(engine: Path, sut: Path, retro_id: str, runner) -> int:  # noqa: ANN001, ANN202
    return resume_nightly(
        NightlyOptions(sut=str(sut), retro_id=retro_id, engine_root=str(engine)),
        eval_runner=runner,
    )


def _states(retro: Path) -> dict[str, str]:
    return proposal_states(read_promotion_events(retro))


def test_suite_without_hard_gate_never_auto_applies(tmp_path: Path) -> None:
    """eval-smoke scenario: no hard gate -> no auto-apply, even on a clean pass."""
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-nogate", [_proposal("P-1", "eval-smoke")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "eval-smoke", hard_gate=False)
    _seed_baselines(engine, {"eval-smoke": 1.0})

    calls: list = []
    code = _resume(engine, sut, "retro-nogate", _runner(calls))

    assert code == NIGHTLY_PENDING_REVIEW
    assert calls == ["eval-smoke"]
    events = read_promotion_events(retro)
    completed = [e for e in events if e["type"] == "eval_completed"]
    assert len(completed) == 1
    assert completed[0]["result"] == "pass"
    assert "no hard gates" in completed[0]["note"]
    assert not [e for e in events if e["type"] == "application"]
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()
    assert _states(retro)["P-1"] == "promoted_pending_eval"


def test_missing_baseline_inconclusive_then_resumes_after_approval(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-nobase", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")

    calls: list = []
    code = _resume(engine, sut, "retro-nobase", _runner(calls))

    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(retro)
    completed = [e for e in events if e["type"] == "eval_completed"]
    assert len(completed) == 1
    assert completed[0]["result"] == "inconclusive"
    assert "aa eval baseline update --suite workflow-run" in completed[0]["note"]
    assert completed[0]["run_ids"] == ["eval-workflow-run-1"]
    assert not [e for e in events if e["type"] == "application"]
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()
    assert _states(retro)["P-1"] == "awaiting_baseline"

    # After the baseline is approved, resume re-runs the eval and applies.
    _seed_baselines(engine, {"workflow-run": 1.0})
    code = _resume(engine, sut, "retro-nobase", _runner(calls))
    assert code == NIGHTLY_OK
    assert calls == ["workflow-run", "workflow-run"]
    assert (sut / ".aa" / "memory" / "P-1.md").exists()
    assert _states(retro)["P-1"] == "applied"


def test_hard_gate_satisfaction_flip_rolls_back(tmp_path: Path) -> None:
    """Baseline passes the hard threshold, candidate fails it -> regression."""
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-flip", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")
    _seed_baselines(engine, {"workflow-run": 1.0})

    calls: list = []
    runner = _runner(calls, metrics={"workflow-run": 0.5})
    code = _resume(engine, sut, "retro-flip", runner)

    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(retro)
    assert any(e["type"] == "eval_completed" and e["result"] == "regression" for e in events)
    rolled_back = [e for e in events if e["type"] == "application" and e["result"] == "rolled_back"]
    assert len(rolled_back) == 1
    assert "evidence_integrity" in rolled_back[0]["note"]
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()
    assert _states(retro)["P-1"] == "rolled_back"

    # rolled_back is treated as needs_rework: resume does not re-run the eval.
    assert _resume(engine, sut, "retro-flip", runner) == NIGHTLY_PENDING_REVIEW
    assert calls == ["workflow-run"]


def test_directional_regression_rolls_back_before_hard_threshold_flip(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-delta", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")
    _seed_baselines(engine, {"workflow-run": 1.0})

    calls: list = []
    code = _resume(
        engine,
        sut,
        "retro-delta",
        _runner(calls, metrics={"workflow-run": 0.96}),
    )

    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(retro)
    completed = [e for e in events if e["type"] == "eval_completed"]
    assert completed[-1]["result"] == "regression"
    assert "evidence_integrity: 1.0 → 0.96" in completed[-1]["note"]
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()


def test_lower_is_better_and_provenance_fail_closed() -> None:
    policy = {
        "version": "3",
        "thresholds": [{"metric": "violations", "gate": "hard", "op": "lte", "value": 10}],
        "regression": {
            "repeat": 2,
            "metrics": {
                "violations": {"direction": "lower_is_better", "max_regression": 0.0},
            },
        },
    }
    policy_hash = hashlib.sha256(
        json.dumps(policy["regression"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    provenance = {
        "suite_version": "3",
        "repeat": 2,
        "regression_policy_sha256": policy_hash,
    }

    comparison = compare_suite_regression(
        {"violations": 1.0},
        {"violations": 2.0},
        policy,
        baseline_provenance=provenance,
        candidate_provenance=provenance,
    )
    assert comparison.regressed is True
    assert comparison.inconclusive is False

    mismatch = compare_suite_regression(
        {"violations": 1.0},
        {"violations": 1.0},
        policy,
        baseline_provenance={**provenance, "repeat": 1},
        candidate_provenance=provenance,
    )
    assert mismatch.inconclusive is True
    assert "repeat" in mismatch.details[0]

    missing = compare_suite_regression(
        {"violations": 1.0},
        {},
        policy,
        baseline_provenance=provenance,
        candidate_provenance=provenance,
    )
    assert missing.inconclusive is True
    assert "missing baseline or candidate evidence" in missing.details[0]


def test_candidate_gate_fail_is_regression(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-gatefail", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")
    _seed_baselines(engine, {"workflow-run": 1.0})

    calls: list = []
    runner = _runner(calls, verdicts={"workflow-run": "fail"})
    code = _resume(engine, sut, "retro-gatefail", runner)

    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(retro)
    completed = [e for e in events if e["type"] == "eval_completed"]
    assert completed[0]["result"] == "regression"
    assert completed[0]["gate"] == "fail"
    assert any(e["type"] == "application" and e["result"] == "rolled_back" for e in events)
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()


def test_hard_gate_failures_force_regression_despite_pass_verdict(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    retro = _seed_promoted(sut, "retro-hardfail", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")
    _seed_baselines(engine, {"workflow-run": 1.0})

    calls: list = []
    runner = _runner(calls, hard_gate_failures={"workflow-run": ["evidence_integrity"]})
    code = _resume(engine, sut, "retro-hardfail", runner)

    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(retro)
    completed = [e for e in events if e["type"] == "eval_completed"]
    assert completed[0]["result"] == "regression"
    assert "hard_gate_failures=evidence_integrity" in completed[0]["note"]
    assert not (sut / ".aa" / "memory" / "P-1.md").exists()


def test_pass_lands_exactly_once(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _seed_promoted(sut, "retro-once", [_proposal("P-1", "workflow-run")])
    engine = tmp_path / "engine"
    _seed_suite(engine, "workflow-run")
    _seed_baselines(engine, {"workflow-run": 1.0})

    calls: list = []
    runner = _runner(calls)
    assert _resume(engine, sut, "retro-once", runner) == NIGHTLY_OK

    memory_path = sut / ".aa" / "memory" / "P-1.md"
    content = memory_path.read_text(encoding="utf-8")
    assert content.count("<!-- retro:retro-once#P-1 evidence:CH-1#F-1 -->") == 1
    assert "- remember P-1" in content

    # Second resume: terminal state, no re-eval, no duplicate append.
    assert _resume(engine, sut, "retro-once", runner) == NIGHTLY_PENDING_REVIEW
    assert calls == ["workflow-run"]
    assert memory_path.read_text(encoding="utf-8") == content


def test_runner_error_marks_eval_error_and_does_not_block_other_suites(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    retro = _seed_promoted(
        sut,
        "retro-err",
        [_proposal("P-A", "suite-a"), _proposal("P-B", "suite-b")],
    )
    engine = tmp_path / "engine"
    _seed_suite(engine, "suite-a")
    _seed_suite(engine, "suite-b")
    _seed_baselines(engine, {"suite-a": 1.0, "suite-b": 1.0})

    calls: list = []
    runner = _runner(calls, raises_for={"suite-a"})
    code = _resume(engine, sut, "retro-err", runner)

    assert code == NIGHTLY_FAILURE
    assert set(calls) == {"suite-a", "suite-b"}  # suite-b still ran
    events = read_promotion_events(retro)
    errors = [e for e in events if e["type"] == "eval_completed" and e["result"] == "error"]
    assert [e["proposal_id"] for e in errors] == ["P-A"]
    assert "infrastructure down" in errors[0]["note"]
    assert not (sut / ".aa" / "memory" / "P-A.md").exists()
    assert _states(retro)["P-A"] == "eval_error"
    assert _states(retro)["P-B"] == "applied"
    assert (sut / ".aa" / "memory" / "P-B.md").exists()

    # eval_error is retryable: a healthy runner re-evals and applies.
    code = _resume(engine, sut, "retro-err", _runner(calls))
    assert code == NIGHTLY_OK
    assert calls.count("suite-a") == 2
    assert (sut / ".aa" / "memory" / "P-A.md").exists()
    assert _states(retro)["P-A"] == "applied"
