"""Authenticate the opaque Change-local prerequisite for the cutover plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from tests.phase6.conformance import (
    ChangeLocalAdmissionV1,
    EXPECTED_RESIDUAL_MAPPINGS,
    REQUIRED_WAIVERS,
    ResidualDispositionV1,
)


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = ROOT / ".superpowers/sdd/2026-08-26-change-local-assurance-workspace"
UPSTREAM_PLAN = ROOT / "docs/superpowers/plans/2026-08-26-change-local-assurance-workspace.md"
UPSTREAM_SPEC = ROOT / "docs/superpowers/specs/2026-08-25-change-local-assurance-workspace-design.md"
UPSTREAM_ACCEPTANCE = UPSTREAM_ROOT / "acceptance.md"
UPSTREAM_REPORT = UPSTREAM_ROOT / "task-17-report.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _verify_ancestry(source_commit: str) -> None:
    object_result = subprocess.run(
        ["git", "cat-file", "-e", f"{source_commit}^{{commit}}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _require(object_result.returncode == 0, "source_commit is not an available commit")
    ancestry_result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _require(ancestry_result.returncode == 0, "source_commit is not an ancestor of HEAD")


def _verify_admission(admission_path: Path) -> None:
    admission = ChangeLocalAdmissionV1.model_validate(_read_json(admission_path))
    _require(_sha256(UPSTREAM_PLAN) == admission.plan_sha256, "Change-local plan digest mismatch")
    _require(_sha256(UPSTREAM_SPEC) == admission.spec_sha256, "Change-local spec digest mismatch")
    _require(
        _sha256(UPSTREAM_ACCEPTANCE) == admission.acceptance_sha256, "Change-local acceptance digest mismatch"
    )
    _verify_ancestry(admission.source_commit)

    acceptance_text = UPSTREAM_ACCEPTANCE.read_text(encoding="utf-8")
    _require("**Verdict: not accepted.**" in acceptance_text, "upstream verdict is not authentic")
    _require(admission.upstream_verdict == "not_accepted", "admission rewrites upstream verdict")
    _require(
        admission.admission_status == "accepted_with_waivers",
        "not accepted upstream evidence cannot be admitted as complete",
    )

    actual_waivers = {
        waiver.waiver_id: (waiver.disposition, waiver.replacement_tasks, waiver.approved_by)
        for waiver in admission.waivers
    }
    expected_waivers = {
        waiver_id: (disposition, replacement_tasks, "user")
        for waiver_id, (disposition, replacement_tasks) in REQUIRED_WAIVERS.items()
    }
    _require(actual_waivers == expected_waivers, "unwaived or non-user-approved upstream blocker")
    _require(all(waiver.evidence for waiver in admission.waivers), "waiver lacks user approval evidence")

    report_text = UPSTREAM_REPORT.read_text(encoding="utf-8")
    unresolved_priority = re.search(r"\b(?:P1|P2)\b.*\b(?:open|unresolved)\b", report_text, re.IGNORECASE)
    _require(unresolved_priority is None, "unresolved upstream P1/P2 finding")

    residual_path = admission_path.with_name("residual-disposition.json")
    raw_residuals = _read_json(residual_path)
    if not isinstance(raw_residuals, list):
        raise ValueError("residual disposition must be a JSON array")
    records = tuple(ResidualDispositionV1.model_validate(item) for item in raw_residuals)
    actual_residuals = {
        (record.source_plan, record.source_task): (record.disposition, record.replacement_task)
        for record in records
    }
    _require(len(records) == len(actual_residuals), "duplicate residual disposition record")
    _require(actual_residuals == EXPECTED_RESIDUAL_MAPPINGS, "residual disposition is incomplete or changed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admission", type=Path, required=True)
    args = parser.parse_args()
    try:
        _verify_admission(args.admission.resolve())
    except ValueError as error:
        print(f"remaining-phase admission rejected: {error}", file=sys.stderr)
        return 1
    print("remaining-phase admission accepted_with_waivers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
