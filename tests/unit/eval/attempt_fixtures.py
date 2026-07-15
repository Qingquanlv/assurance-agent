from __future__ import annotations

import json
from pathlib import Path

import yaml


def make_attempt(root: Path, *, stdout: str = "", stderr: str = "",
                 execution: dict | None = None) -> Path:
    attempt = root / "attempt-0"
    (attempt / "raw-output").mkdir(parents=True)
    (attempt / "stdout.log").write_text(stdout, encoding="utf-8")
    (attempt / "stderr.log").write_text(stderr, encoding="utf-8")
    (attempt / "execution.json").write_text(
        json.dumps(execution or {"executor": "workflow-run"}), encoding="utf-8"
    )
    return attempt


def write_case(attempt: Path, module: str, valid: bool = True) -> None:
    cases = attempt / "raw-output" / "cases" / module
    cases.mkdir(parents=True, exist_ok=True)
    body = "id: TC-1\n" if valid else "id: [unclosed"
    (cases / "case.yaml").write_text(body, encoding="utf-8")


def write_review(attempt: Path, decision: str) -> None:
    review = attempt / "raw-output" / "review"
    review.mkdir(parents=True, exist_ok=True)
    (review / "case-review.json").write_text(
        json.dumps({"decision": decision}), encoding="utf-8"
    )


def write_state(attempt: Path, case_status: str = "done") -> None:
    (attempt / "raw-output" / "workflow-state.yaml").write_text(
        yaml.safe_dump({"phases": {"case_design": {"status": case_status}}}),
        encoding="utf-8",
    )


def write_layer_result(attempt: Path, layer: str, passed: int, total: int,
                       status: str = "PASS") -> None:
    ex = attempt / "raw-output" / "execution"
    ex.mkdir(parents=True, exist_ok=True)
    (ex / f"{layer}-result.json").write_text(
        json.dumps({"status": status, "passed": passed, "total": total}),
        encoding="utf-8",
    )


def write_manifest(attempt: Path, final_status: str, selected: list[str]) -> None:
    ex = attempt / "raw-output" / "execution"
    ex.mkdir(parents=True, exist_ok=True)
    (ex / "execution-manifest.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "change_id": "eval-sample-001",
            "batch_id": "20260715-000000",
            "final_status": final_status,
            "selected_targets": {t: (t in selected) for t in
                                 ["api", "e2e", "fuzz", "performance"]},
            "result_files": {},
        }),
        encoding="utf-8",
    )
