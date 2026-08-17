"""Metrics shortboards must not masquerade as execution failures.

Pins the §6 / Task 8 isolation claim against the live healing-loop gate
expression and a seed execution manifest: a metrics document full of gaps
leaves ``execution-manifest.final_status`` untouched and does not reroute
healing. It is deliberately *not* isolated from ``archive-gate``: a
``collection_gaps`` document routes ``metrics-sufficiency-gate`` to ``reject``,
which keeps the graph flowing to report/archive/retro (see
``workflow-schema.yaml``'s route table) but must still block the archive
itself, the same way an open Problem does.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view


def test_gapped_metrics_do_not_flip_healing_loop_or_archive_verdicts(tmp_path: Path) -> None:
    """With PASS execution + settled healing, both gates pass even if metrics are dirty."""
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    for rel, payload in {
        "execution/execution-manifest.json": {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "b1",
            "final_status": "PASS",
        },
        "inspect/failure-analysis.json": {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "source_manifest": "execution/execution-manifest.json",
            "inspection_status": "completed",
            "batch_id": "b1",
            "source_batch_id": "b1",
            "final_status": "PASS",
            "inspect_mode": "primary",
            "compat_fallback_reason": None,
            "classification_performed": False,
            "status": "no_failures",
            "failures": [],
            "hard_fails": [],
            "needs_review": [],
            "known_product_issues": [],
        },
        "healing/status.json": {
            "status": "not_needed",
            "attempts_used": 0,
            "all_fixers_no_op": False,
        },
        "inspect/metrics.json": {
            "schema_version": "2",
            "change_id": "CH-1",
            "cadence": "pr",
            "computed_at": "2026-08-05T02:00:00+00:00",
            "risk_tier": "low",
            "risk_tier_lower_bound": "low",
            "risk_tier_declared": None,
            "risk_declaration_lowered": False,
            "risk_lowered_declarations": [],
            "metrics": {
                "constraint_coverage": {
                    "layer": "api",
                    "status": "collection_failed",
                    "value": None,
                    "declared": None,
                    "touched": None,
                    "holds": None,
                    "surfaces": [],
                    "evidence": "",
                }
            },
            "collection_gaps": [
                {
                    "code": "collection_failed",
                    "detail": "boom",
                    "metric": "constraint_coverage",
                    "subject": "",
                }
            ],
            "shortboards": [],
            "floor_ratio": None,
            "policy_digest": "0" * 64,
        },
        "inspect/trace-sufficiency.json": {
            "schema_version": "1",
            "change_id": "CH-1",
            "authoritative_batch_id": "b1",
            "policy_digest": "0" * 64,
            "as_of": "2026-08-05T02:00:00+00:00",
            "integrity": "complete",
            "integrity_blocks_routing": False,
            "sufficient": True,
            "has_open_problems": False,
            "error_code": None,
            "insufficient_cases": [],
            "gap_codes": [],
        },
        "review/case-review.json": {"decision": "pass"},
        "review/api-plan-review.json": {"decision": "pass"},
        "review/plan-review.json": {"decision": "pass"},
    }.items():
        path = change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith((".yaml", ".yml")):
            path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    (tmp_path / ".aa").mkdir(parents=True, exist_ok=True)
    # Minimal policy so archive/healing can load if needed; packaged defaults via empty.
    from assurance_agent.artifacts.policy import load_policy_bytes

    (tmp_path / ".aa" / "policy.yaml").write_text(
        yaml.safe_dump(load_policy_bytes(None, origin="packaged").model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    schema = load_workflow_v2(Path.cwd())
    ctx = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id="CH-1",
        params={"max_healing_attempts": 3, "auto_archive": True, "test_types": ["api", "e2e"]},
        state_values={},
        node_results={},
    )

    healing = check_gate_in_view(schema.gates, "healing-loop-gate", ctx)
    assert healing.verdict.value == "exit"

    metrics = check_gate_in_view(schema.gates, "metrics-sufficiency-gate", ctx)
    assert metrics.verdict.value == "reject"

    archive = check_gate_in_view(schema.gates, "archive-gate", ctx)
    assert archive.verdict.value == "stop"
    assert archive.details is not None
    assert archive.details["cause"] == "archive.metrics_collection_gap"

    # Execution manifest bytes unchanged by the metrics gate (gate writes nothing).
    manifest = yaml.safe_load(
        (change_dir / "execution" / "execution-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["final_status"] == "PASS"
