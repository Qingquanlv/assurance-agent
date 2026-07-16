from __future__ import annotations

import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

import yaml


def make_archived_change(
    project_root: Path,
    change_id: str,
    *,
    failures: list[dict] | None = None,
    review_decision: str = "pass",
    gate_pushbacks: int = 0,
    apply_status: str = "applied",
    reclassifications: list[dict] | None = None,
) -> Path:
    root = project_root / "qa" / "archive" / change_id
    root.mkdir(parents=True)
    write_aa_config(project_root)
    events: list[dict[str, object]] = [{"type": "workflow_started", "change_id": change_id}]
    for _ in range(gate_pushbacks):
        events.append({"type": "gate_pushback", "gate": "case-review"})
    for index, reclass in enumerate(reclassifications or [], start=1):
        events.append(
            {
                "source": "report",
                "type": "failure_reclassified",
                "seq": index,
                "failure": reclass.get("failure", f"F-{index}"),
                "from": reclass["from"],
                "to": reclass["to"],
                "evidence": reclass.get("evidence", "manual review"),
            }
        )
    events.append(
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": f"ep-{change_id}",
            "attempt_id": f"ha-{change_id}-1",
            "attempt_number": 1,
            "operation_id": f"op-{change_id}-1",
            "source_batch_id": "b1",
        }
    )
    (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (root / "workflow-state.yaml").write_text(
        yaml.safe_dump({"change_id": change_id, "phases": {}}), encoding="utf-8"
    )
    inspect = root / "inspect"
    inspect.mkdir()
    category_map = {
        "assertion": "assertion_failure",
        "locator": "locator_failure",
        "environment": "environment_failure",
    }
    entries = []
    for index, raw in enumerate(failures or [], start=1):
        category = category_map.get(raw.get("classification", ""), raw.get("category", "test_code_error"))
        entries.append(
            {
                "id": f"F-{index}",
                "case_id": f"TC-{index}",
                "target": "api",
                "category": category,
                "fix_proposal_eligible": False,
                "severity": "high",
                "evidence": {
                    "result_file": "",
                    "test_file": "",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "fixture",
                },
                "diagnosis": "fixture",
                "recommended_action": "review",
            }
        )
    (inspect / "failure-analysis.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "source_manifest": "execution/execution-manifest.yaml",
                "inspection_status": "completed",
                "batch_id": "b1",
                "source_batch_id": "b1",
                "final_status": "FAIL" if entries else "PASS",
                "inspect_mode": "primary",
                "classification_performed": True,
                "status": "analyzed" if entries else "no_failures",
                "failures": entries,
                "hard_fails": entries,
                "needs_review": [],
                "known_product_issues": [],
            }
        ),
        encoding="utf-8",
    )
    review = root / "review"
    review.mkdir()
    (review / "case-review.json").write_text(
        json.dumps({"schema_version": "1.0", "decision": review_decision, "findings": []}), encoding="utf-8"
    )
    healing = root / "healing"
    healing.mkdir()
    (healing / "api-apply-summary.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "target": "api",
                "applied": apply_status == "applied",
                "status": apply_status,
            }
        ),
        encoding="utf-8",
    )
    return root
