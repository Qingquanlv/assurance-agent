from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.retro.apply import apply_proposal_to_stage
from assurance_agent.retro.nightly.driver import resume_nightly
from assurance_agent.retro.nightly.exit_codes import NIGHTLY_OK, NIGHTLY_PENDING_REVIEW
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.promotions import read_promotion_events


def _eval_support_from_disk(engine: Path, suite: str) -> dict:
    suite_contract: dict = {}
    suite_path = engine / "eval" / "suites" / f"{suite}.yaml"
    if suite_path.is_file():
        data = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            suite_contract = data
    baseline_metrics = None
    baseline_path = engine / "eval" / "baselines" / "main.json"
    if baseline_path.is_file():
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
        entry = raw.get(suite) if isinstance(raw, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("metrics"), dict):
            baseline_metrics = entry["metrics"]
    return {"suite_contract": suite_contract, "baseline_metrics": baseline_metrics}


def _retro_with_proposal(sut: Path, retro_id: str, *, status: str = "promoted") -> Path:
    retro = sut / "qa" / "retro" / retro_id
    retro.mkdir(parents=True)
    write_json(
        retro / "proposals.json",
        {
            "proposals": [
                {
                    "id": "P-1",
                    "apply_kind": "memory_append",
                    "eval_suite": "workflow-run",
                    "status": status,
                    "target": ".aa/memory/P-1.md",
                    "problem": "x",
                    "proposed_change": "remember to check fixtures",
                    "evidence_ids": ["CH-1#F-1"],
                }
            ]
        },
    )
    return retro


def test_apply_proposal_to_stage(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-1")
    stage = tmp_path / "stage"
    proposal = apply_proposal_to_stage(sut_root=sut, retro_id="retro-1", proposal_id="P-1", stage_dir=stage)
    assert proposal.id == "P-1"
    assert (stage / ".aa" / "memory" / "P-1.md").read_text().startswith("remember")


def test_resume_gate_pass_promotes(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-pass")
    engine = tmp_path / "engine"
    _seed_baseline(engine)
    _seed_suite(engine)

    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        run_id = "eval-gate-1"
        run = sut_dir / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True)
        (run / "metrics.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "suite": suite,
                    "sample_count": 1,
                    "metrics": {"evidence_integrity": 1.0},
                }
            ),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "verdict": "pass",
            "metrics": {"evidence_integrity": 1.0},
            "hard_gate_failures": [],
            **_eval_support_from_disk(Path(engine_root), suite),
        }

    # resume uses Path.cwd() as engine_root — monkey via chdir in test by writing baseline under cwd
    # Instead patch by placing baseline where cwd is: use monkeypatch in pytest
    import os

    old = os.getcwd()
    os.chdir(engine)
    try:
        code = resume_nightly(
            NightlyOptions(sut=str(sut), retro_id="retro-pass"),
            eval_runner=eval_runner,
        )
    finally:
        os.chdir(old)
    assert code == NIGHTLY_OK
    events = read_promotion_events(sut / "qa/retro/retro-pass")
    assert any(e["type"] == "eval_completed" and e["result"] == "pass" for e in events)
    applied = [e for e in events if e["type"] == "application" and e["result"] == "applied"]
    assert len(applied) == 1
    assert applied[0]["target"] == ".aa/memory/P-1.md"
    assert applied[0]["content_sha256"]
    memory = (sut / ".aa" / "memory" / "P-1.md").read_text(encoding="utf-8")
    assert "<!-- retro:retro-pass#P-1 evidence:CH-1#F-1 -->" in memory
    assert "- remember to check fixtures" in memory


def test_resume_gate_fail_needs_rework(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-fail")
    engine = tmp_path / "engine"
    _seed_baseline(engine)
    _seed_suite(engine)

    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        run_id = "eval-gate-2"
        run = sut_dir / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True)
        (run / "metrics.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "suite": suite,
                    "sample_count": 1,
                    "metrics": {"evidence_integrity": 0.5},
                }
            ),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "verdict": "pass",
            "metrics": {"evidence_integrity": 0.5},
            "hard_gate_failures": [],
            **_eval_support_from_disk(Path(engine_root), suite),
        }

    import os

    old = os.getcwd()
    os.chdir(engine)
    try:
        code = resume_nightly(
            NightlyOptions(sut=str(sut), retro_id="retro-fail"),
            eval_runner=eval_runner,
        )
    finally:
        os.chdir(old)
    assert code == NIGHTLY_PENDING_REVIEW
    events = read_promotion_events(sut / "qa/retro/retro-fail")
    assert any(e["type"] == "eval_completed" and e["result"] == "regression" for e in events)
    assert any(e["type"] == "application" and e["result"] == "rolled_back" for e in events)


def _seed_baseline(engine: Path, metric: float = 1.0) -> None:
    (engine / "eval" / "baselines").mkdir(parents=True, exist_ok=True)
    (engine / "eval" / "baselines" / "main.json").write_text(
        json.dumps(
            {
                "workflow-run": {
                    "run_id": "base",
                    "approved_at": "2026-07-16T00:00:00Z",
                    "approved_by": "test",
                    "metrics": {"evidence_integrity": metric},
                }
            }
        ),
        encoding="utf-8",
    )


def _seed_suite(engine: Path) -> None:
    """Hard-gated suite contract so the phase-F gate may auto-apply."""
    suites = engine / "eval" / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    (suites / "workflow-run.yaml").write_text(
        "name: workflow-run\n"
        "scorer: workflow-run\n"
        "thresholds:\n"
        "  - metric: evidence_integrity\n"
        "    gate: hard\n"
        "    op: gte\n"
        "    value: 0.95\n",
        encoding="utf-8",
    )


def _passing_runner(calls: list):  # noqa: ANN001, ANN202
    def eval_runner(*, suite, sut_dir, engine_root, extra_memory_dir=None):  # noqa: ANN001
        calls.append(suite)
        run_id = f"eval-gate-es-{len(calls)}"
        run = sut_dir / "eval" / "out" / "runs" / run_id
        run.mkdir(parents=True)
        (run / "metrics.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "suite": suite,
                    "sample_count": 1,
                    "metrics": {"evidence_integrity": 1.0},
                }
            ),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "verdict": "pass",
            "metrics": {"evidence_integrity": 1.0},
            "hard_gate_failures": [],
            **_eval_support_from_disk(Path(engine_root), suite),
        }

    return eval_runner


def _resume_from(engine: Path, sut: Path, retro_id: str, runner) -> int:  # noqa: ANN001, ANN202
    import os

    old = os.getcwd()
    os.chdir(engine)
    try:
        return resume_nightly(NightlyOptions(sut=str(sut), retro_id=retro_id), eval_runner=runner)
    finally:
        os.chdir(old)


def test_resume_reads_event_stream_promotions(tmp_path: Path) -> None:
    """A schema_version "2" promotions.json drives resume even when the
    proposal itself still carries status=proposed."""
    sut = tmp_path / "sut"
    retro = _retro_with_proposal(sut, "retro-es", status="proposed")
    write_json(
        retro / "promotions.json",
        {
            "schema_version": "2",
            "events": [
                {
                    "proposal_id": "P-1",
                    "type": "review_decision",
                    "decision": "promoted",
                    "actor": "LQ",
                    "at": "2026-07-16T00:00:00Z",
                }
            ],
        },
    )
    engine = tmp_path / "engine"
    _seed_baseline(engine)
    _seed_suite(engine)

    calls: list = []
    code = _resume_from(engine, sut, "retro-es", _passing_runner(calls))
    assert code == NIGHTLY_OK
    assert calls == ["workflow-run"]
    events = read_promotion_events(retro)
    assert any(e["type"] == "application" and e["result"] == "applied" for e in events)
    assert (sut / ".aa" / "memory" / "P-1.md").exists()


def test_resume_reads_legacy_promotions_list(tmp_path: Path) -> None:
    """Legacy list-format promotions.json keeps working (read compatibility)."""
    sut = tmp_path / "sut"
    retro = _retro_with_proposal(sut, "retro-legacy", status="proposed")
    write_json(
        retro / "promotions.json",
        [
            {
                "proposal_id": "P-1",
                "decision": "promoted",
                "decided_by": "LQ",
                "decided_at": "2026-07-16T00:00:00Z",
            }
        ],
    )
    engine = tmp_path / "engine"
    _seed_baseline(engine)
    _seed_suite(engine)

    calls: list = []
    code = _resume_from(engine, sut, "retro-legacy", _passing_runner(calls))
    assert code == NIGHTLY_OK
    assert calls == ["workflow-run"]


def test_resume_skips_terminal_applied_proposals(tmp_path: Path) -> None:
    """A second resume after a gate pass must not re-run eval or re-append."""
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-twice")
    engine = tmp_path / "engine"
    _seed_baseline(engine)
    _seed_suite(engine)

    calls: list = []
    runner = _passing_runner(calls)
    assert _resume_from(engine, sut, "retro-twice", runner) == NIGHTLY_OK
    assert calls == ["workflow-run"]

    assert _resume_from(engine, sut, "retro-twice", runner) == NIGHTLY_PENDING_REVIEW
    assert calls == ["workflow-run"]  # no second eval
