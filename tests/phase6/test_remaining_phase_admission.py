from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

import pytest

from scripts import check_remaining_phase_admission as checker
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


def _copied_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    upstream_root = tmp_path / "upstream"
    upstream_root.mkdir()
    plan = tmp_path / "plan.md"
    spec = tmp_path / "spec.md"
    acceptance = upstream_root / "acceptance.md"
    report = upstream_root / "task-17-report.md"
    plan.write_bytes(checker.UPSTREAM_PLAN.read_bytes())
    spec.write_bytes(checker.UPSTREAM_SPEC.read_bytes())
    acceptance.write_bytes(checker.UPSTREAM_ACCEPTANCE.read_bytes())
    report.write_bytes(checker.UPSTREAM_REPORT.read_bytes())

    admission = tmp_path / "admission.json"
    residuals = tmp_path / "residual-disposition.json"
    admission.write_bytes((EVIDENCE_ROOT / "admission.json").read_bytes())
    residuals.write_bytes((EVIDENCE_ROOT / "residual-disposition.json").read_bytes())
    monkeypatch.setattr(checker, "UPSTREAM_PLAN", plan)
    monkeypatch.setattr(checker, "UPSTREAM_SPEC", spec)
    monkeypatch.setattr(checker, "UPSTREAM_ACCEPTANCE", acceptance)
    monkeypatch.setattr(checker, "UPSTREAM_REPORT", report)
    return admission, residuals, report


def _rewrite_admission(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def _rewrite_residuals(path: Path, mutate: Callable[[list[dict[str, object]]], None]) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda admission: admission.__setitem__("admission_status", "complete"),
            "cannot be admitted as complete",
        ),
        (lambda admission: admission.__setitem__("plan_sha256", "0" * 64), "plan digest mismatch"),
        (lambda admission: admission.__setitem__("spec_sha256", "0" * 64), "spec digest mismatch"),
        (
            lambda admission: admission.__setitem__("acceptance_sha256", "0" * 64),
            "acceptance digest mismatch",
        ),
        (lambda admission: admission.__setitem__("waivers", admission["waivers"][1:]), "unwaived"),
        (
            lambda admission: admission["waivers"].append(admission["waivers"][0].copy()),
            "duplicate waiver",
        ),
    ],
)
def test_checker_rejects_mislabeled_or_unwaived_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    admission, _, _ = _copied_evidence(tmp_path, monkeypatch)
    _rewrite_admission(admission, mutation)

    with pytest.raises(ValueError, match=message):
        checker._verify_admission(admission)


@pytest.mark.parametrize("finding", ["Open P1\nstill pending", "P2\nstatus: unresolved"])
def test_checker_rejects_unresolved_priority_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finding: str
) -> None:
    admission, _, report = _copied_evidence(tmp_path, monkeypatch)
    report.write_text(finding, encoding="utf-8")

    with pytest.raises(ValueError, match="unresolved upstream P1/P2"):
        checker._verify_admission(admission)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda residuals: residuals.clear(),
        lambda residuals: residuals.append(residuals[0].copy()),
    ],
)
def test_checker_rejects_malformed_or_duplicate_residuals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[list[dict[str, object]]], None],
) -> None:
    admission, residuals, _ = _copied_evidence(tmp_path, monkeypatch)
    _rewrite_residuals(residuals, mutation)

    with pytest.raises(ValueError, match="residual disposition"):
        checker._verify_admission(admission)


def test_checker_rejects_malformed_residual_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admission, residuals, _ = _copied_evidence(tmp_path, monkeypatch)
    residuals.write_text("[{}]", encoding="utf-8")

    with pytest.raises(ValueError):
        checker._verify_admission(admission)
