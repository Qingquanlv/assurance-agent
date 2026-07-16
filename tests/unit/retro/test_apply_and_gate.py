from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.apply import apply_proposal_to_stage
from assurance_agent.retro.nightly.driver import resume_nightly
from assurance_agent.retro.nightly.exit_codes import NIGHTLY_OK, NIGHTLY_PENDING_REVIEW
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import write_json


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
    proposal = apply_proposal_to_stage(
        sut_root=sut, retro_id="retro-1", proposal_id="P-1", stage_dir=stage
    )
    assert proposal.id == "P-1"
    assert (stage / ".aa" / "memory" / "P-1.md").read_text().startswith("remember")


def test_resume_gate_pass_promotes(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-pass")
    # Seed a fake baseline + run metrics directory for compare path
    engine = tmp_path / "engine"
    (engine / "eval" / "baselines").mkdir(parents=True)
    (engine / "eval" / "baselines" / "main.json").write_text(
        json.dumps(
            {
                "workflow-run": {
                    "run_id": "base",
                    "approved_at": "2026-07-16T00:00:00Z",
                    "approved_by": "test",
                    "metrics": {"evidence_integrity": 1.0},
                }
            }
        ),
        encoding="utf-8",
    )

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
        return {"run_id": run_id, "verdict": "pass", "metrics": {"evidence_integrity": 1.0}}

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
    promotions = json.loads((sut / "qa/retro/retro-pass/promotions.json").read_text())
    assert promotions[-1]["decision"] == "promoted"
    assert (sut / ".aa" / "memory" / "P-1.md").exists()


def test_resume_gate_fail_needs_rework(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    _retro_with_proposal(sut, "retro-fail")
    engine = tmp_path / "engine"
    (engine / "eval" / "baselines").mkdir(parents=True)
    (engine / "eval" / "baselines" / "main.json").write_text(
        json.dumps(
            {
                "workflow-run": {
                    "run_id": "base",
                    "approved_at": "2026-07-16T00:00:00Z",
                    "approved_by": "test",
                    "metrics": {"evidence_integrity": 1.0},
                }
            }
        ),
        encoding="utf-8",
    )

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
        return {"run_id": run_id, "verdict": "pass", "metrics": {"evidence_integrity": 0.5}}

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
    promotions = json.loads((sut / "qa/retro/retro-fail/promotions.json").read_text())
    assert promotions[-1]["decision"] == "needs_rework"
