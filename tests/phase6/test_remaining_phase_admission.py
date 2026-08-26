from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from tests.phase6.conformance import (
    ChangeLocalAdmissionV1,
    EXPECTED_RESIDUAL_MAPPINGS,
    REQUIRED_WAIVERS,
    ResidualDispositionV1,
)


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / ".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout"


def _load_json(name: str) -> object:
    path = EVIDENCE_ROOT / name
    assert path.is_file(), f"missing admission artifact: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_admission_preserves_not_accepted_upstream_verdict() -> None:
    admission = ChangeLocalAdmissionV1.model_validate(_load_json("admission.json"))

    assert admission.source_commit == "8d8d1ba0a755398ae1edccd6c55cff986c9e6729"
    assert admission.upstream_verdict == "not_accepted"
    assert admission.admission_status == "accepted_with_waivers"


def test_every_upstream_blocker_has_one_exact_user_approved_waiver() -> None:
    admission = ChangeLocalAdmissionV1.model_validate(_load_json("admission.json"))

    actual = {
        waiver.waiver_id: (waiver.disposition, waiver.replacement_tasks, waiver.approved_by)
        for waiver in admission.waivers
    }
    expected = {
        waiver_id: (disposition, replacement_tasks, "user")
        for waiver_id, (disposition, replacement_tasks) in REQUIRED_WAIVERS.items()
    }
    assert actual == expected


def test_residual_dispositions_are_unique_and_exactly_mapped() -> None:
    raw = _load_json("residual-disposition.json")
    assert isinstance(raw, list)
    records = tuple(ResidualDispositionV1.model_validate(item) for item in raw)

    actual = {
        (record.source_plan, record.source_task): (record.disposition, record.replacement_task)
        for record in records
    }
    assert actual == EXPECTED_RESIDUAL_MAPPINGS


def test_checker_accepts_only_the_authenticated_waiver_admission() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_remaining_phase_admission.py"),
            "--admission",
            str(EVIDENCE_ROOT / "admission.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
