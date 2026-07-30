#!/usr/bin/env python3
"""Freeze and render benchmark evidence for the two architecture initiatives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import DataKnowledge, PlanReview, QualityGateResult
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.artifacts.models.trace import TraceProjection
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.evidence.sufficiency import SufficiencyReport
from assurance_agent.evidence.verify import VerifyResult, projection_digest
from assurance_agent.knowledge.capabilities import compute_missing_capabilities
from assurance_agent.verification.contract_render import render_output_contract
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view


_POLICY_ACTIONS = ("warn", "block", "require_human")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return payload


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        events.append(event)
    return events


def _atomic_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(raw, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _mechanical_summary(checks: dict[str, Any]) -> dict[str, Any]:
    by_check: dict[str, dict[str, Any]] = {}
    finding_count = 0
    raw_checks = checks.get("checks")
    for item in raw_checks if isinstance(raw_checks, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("check_id"), str):
            continue
        findings = item.get("findings")
        count = len(findings) if isinstance(findings, list) else 0
        finding_count += count
        by_check[item["check_id"]] = {
            "status": item.get("status", "unknown"),
            "finding_count": count,
        }
    return {
        "status": checks.get("status", "unknown"),
        "finding_count": finding_count,
        "by_check": dict(sorted(by_check.items())),
    }


def _contract_summary(events: list[dict[str, Any]], schema_root: Path) -> dict[str, Any]:
    schema = load_workflow_v2(schema_root)
    current_graph_digest = compile_workflow(schema).digest
    recorded_graph_digests = sorted(
        {
            event["graph_digest"]
            for event in events
            if event.get("type") == "graph_invocation_started" and isinstance(event.get("graph_digest"), str)
        }
    )
    schema_matches = recorded_graph_digests == [current_graph_digest]
    invocation_graph = {
        event.get("invocation_id"): event.get("graph_id")
        for event in events
        if event.get("type") == "graph_invocation_started"
        and isinstance(event.get("invocation_id"), str)
        and isinstance(event.get("graph_id"), str)
    }
    mechanical_digest: str | None = None
    agent_execution_digests: set[str] = set()
    rendered_contracts: set[tuple[str, str, str]] = set()
    output_contract_digests: set[str] = set()

    for event in events:
        if event.get("type") != "task_attempt_started":
            continue
        graph_id = invocation_graph.get(event.get("invocation_id"))
        node_id = event.get("node_id")
        digest = event.get("contract_digest")
        if graph_id == "api-plan-cycle" and node_id == "mechanical-plan-checks":
            mechanical_digest = digest if isinstance(digest, str) else None
        if not schema_matches or not isinstance(graph_id, str) or not isinstance(node_id, str):
            continue
        graph = schema.graphs.get(graph_id)
        node = graph.nodes.get(node_id) if graph is not None else None
        if node is None or node.agent is None:
            continue
        if isinstance(digest, str):
            agent_execution_digests.add(digest)
        clause = render_output_contract(node.outputs)
        if not clause:
            continue
        rendered_contracts.add((graph_id, node_id, clause))
        output_contract_digests.add(hashlib.sha256(clause.encode("utf-8")).hexdigest())

    return {
        "mechanical_execution_contract_digest": mechanical_digest,
        "agent_execution_contract_digests": sorted(agent_execution_digests),
        "rendered_output_contract_count": len(rendered_contracts),
        "rendered_output_contract_digests": sorted(output_contract_digests),
        "current_graph_digest": current_graph_digest,
        "recorded_graph_digests": recorded_graph_digests,
        "schema_digest_match": schema_matches,
        # Even with a matching graph, events do not persist the final prompt. A
        # mismatch cannot safely derive historical output clauses at all.
        "prompt_observability": ("derived_not_recorded" if schema_matches else "schema_digest_mismatch"),
    }


def _recorded_policy_digest(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "graph_invocation_started" and isinstance(event.get("policy_digest"), str):
            return event["policy_digest"]
    return None


def _policy_replay(
    *,
    project_root: Path,
    schema_root: Path,
    change_dir: Path,
    review: dict[str, Any],
    checks: dict[str, Any],
    knowledge: dict[str, Any],
) -> list[dict[str, Any]]:
    schema = load_workflow_v2(schema_root)
    base_policy = load_policy(project_root)
    rows: list[dict[str, Any]] = []

    for action in _POLICY_ACTIONS:
        replay_policy = base_policy.model_copy(update={"plan_check_action": action})
        with tempfile.TemporaryDirectory(prefix="aa-policy-replay-") as raw_temp:
            replay_root = Path(raw_temp)
            policy_path = replay_root / ".aa" / "policy.yaml"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                yaml.safe_dump(replay_policy.model_dump(mode="json"), sort_keys=False),
                encoding="utf-8",
            )
            context = GateEvaluationContext(
                project_root=replay_root,
                repo_root=project_root,
                change_dir=change_dir,
                change_id=change_dir.name,
                params={"force_continue": False},
                state_values={},
                node_results={"mechanical-plan-checks": {"status": "succeeded"}},
                artifact_overrides={
                    "review/api-plan-review.json": review,
                    "review/api-plan-checks.json": checks,
                    "repo:.aa/data-knowledge.yaml": knowledge,
                },
                # Replay policy only. Historical accept-risk decisions must not
                # mask a policy branch in this deterministic experiment.
                audit_events_dir=replay_root / "empty-audit-events",
            )
            report = check_gate_in_view(
                schema.gates,
                "api-plan-review-gate",
                context,
            )
        rows.append(
            {
                "action": action,
                "policy_digest": policy_digest(replay_policy),
                "verdict": report.verdict.value,
                "matched_rule": report.matched_rule,
                "missing_capabilities": (report.details or {}).get("missing_capabilities", []),
            }
        )
    return rows


def _projection_summary(projection: TraceProjection, *, reconciled: bool) -> dict[str, Any]:
    rows = projection.rows
    summary: dict[str, Any] = {
        "phase": projection.phase,
        "batch_id": projection.authoritative_batch_id,
        "integrity": projection.integrity,
        "row_count": len(rows),
        "source_count": len(projection.sources),
        "gap_count": len(projection.gaps),
        "unmapped_test_count": len(projection.unmapped_tests),
    }
    if not reconciled:
        return summary

    failure_rows = 0
    failure_links = 0
    problem_rows = 0
    problem_links = 0
    unique_problems: set[str] = set()
    for row in rows:
        failures = row.failures
        problems = row.open_problem_ids
        if failures:
            failure_rows += 1
        if problems:
            problem_rows += 1
        failure_links += len(failures)
        problem_links += len(problems)
        unique_problems.update(problems)
    summary.update(
        {
            "failure_row_count": failure_rows,
            "failure_link_count": failure_links,
            "open_problem_row_count": problem_rows,
            "open_problem_link_count": problem_links,
            "unique_open_problem_count": len(unique_problems),
        }
    )
    return summary


def _quality_summary(
    quality: QualityGateResult,
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage = quality.dimensions.coverage
    if coverage.evidence is None:
        raise ValueError("quality-gate coverage.evidence is required for specialty reporting")
    evidence = SufficiencyReport.model_validate(coverage.evidence)
    reasons: Counter[str] = Counter()
    for verdict in evidence.verdicts:
        if not verdict.sufficient:
            reasons.update(verdict.reason_codes)
    sufficient = sum(verdict.sufficient for verdict in evidence.verdicts)
    insufficient = len(evidence.verdicts) - sufficient
    return (
        {
            "status": coverage.status,
            "line": coverage.line_coverage,
            "branch": coverage.branch_coverage,
            "final_status": quality.final_status,
        },
        {
            "sufficient_count": sufficient,
            "insufficient_count": insufficient,
            "reason_counts": dict(sorted(reasons.items())),
        },
    )


def _validate_cross_artifact_identity(
    *,
    change_id: str,
    review: dict[str, Any],
    execution: dict[str, Any],
    reconciled: dict[str, Any],
    quality: dict[str, Any],
    verify: dict[str, Any],
) -> None:
    if review.get("change_id") != change_id:
        raise ValueError(
            f"review change_id mismatch: expected {change_id!r}, got {review.get('change_id')!r}"
        )
    if execution.get("change_id") != change_id:
        raise ValueError(
            f"execution trace change_id mismatch: expected {change_id!r}, got {execution.get('change_id')!r}"
        )
    if execution.get("phase") != "execution":
        raise ValueError(
            f"execution trace phase mismatch: expected 'execution', got {execution.get('phase')!r}"
        )
    if reconciled.get("change_id") != change_id:
        raise ValueError(
            "reconciled trace change_id mismatch: "
            f"expected {change_id!r}, got {reconciled.get('change_id')!r}"
        )
    if reconciled.get("phase") != "reconciled":
        raise ValueError(
            f"reconciled trace phase mismatch: expected 'reconciled', got {reconciled.get('phase')!r}"
        )
    execution_batch = execution.get("authoritative_batch_id")
    reconciled_batch = reconciled.get("authoritative_batch_id")
    if execution_batch != reconciled_batch:
        raise ValueError(
            f"trace batch mismatch: execution={execution_batch!r}, reconciled={reconciled_batch!r}"
        )
    if quality.get("change_id") != change_id:
        raise ValueError(
            f"quality gate change_id mismatch: expected {change_id!r}, got {quality.get('change_id')!r}"
        )
    if quality.get("batch_id") != execution_batch:
        raise ValueError(
            f"quality gate batch mismatch: expected {execution_batch!r}, got {quality.get('batch_id')!r}"
        )
    if verify.get("change_id") != change_id:
        raise ValueError(
            f"verify change_id mismatch: expected {change_id!r}, got {verify.get('change_id')!r}"
        )
    if verify.get("phase") != "reconciled":
        raise ValueError(f"verify phase mismatch: expected 'reconciled', got {verify.get('phase')!r}")


def _validate_verify_binding(reconciled: TraceProjection, verify: VerifyResult) -> None:
    expected_digest = projection_digest(reconciled)
    if verify.projection_digest != expected_digest:
        raise ValueError(
            "verify projection digest mismatch: "
            f"expected {expected_digest!r}, got {verify.projection_digest!r}"
        )
    if verify.scope is not None and verify.scope.batch != reconciled.authoritative_batch_id:
        raise ValueError(
            "verify scope batch mismatch: "
            f"expected {reconciled.authoritative_batch_id!r}, got {verify.scope.batch!r}"
        )


def collect_report(
    *,
    project_root: Path,
    schema_root: Path,
    change_id: str,
    trace_path: Path,
    verify_path: Path,
    trace_exit: int,
    verify_exit: int,
) -> dict[str, Any]:
    if trace_exit < 0 or verify_exit < 0:
        raise ValueError("trace and verify exit codes must be non-negative")
    change_dir = project_root / "qa" / "changes" / change_id
    raw_review = _load_json(change_dir / "review" / "api-plan-review.json")
    raw_checks = _load_json(change_dir / "review" / "api-plan-checks.json")
    raw_execution = _load_json(trace_path)
    raw_reconciled = _load_json(change_dir / "inspect" / "trace-projection.json")
    raw_quality = _load_json(change_dir / "execution" / "quality-gate-result.json")
    raw_verify = _load_json(verify_path)
    _validate_cross_artifact_identity(
        change_id=change_id,
        review=raw_review,
        execution=raw_execution,
        reconciled=raw_reconciled,
        quality=raw_quality,
        verify=raw_verify,
    )

    review_model = PlanReview.model_validate(raw_review)
    checks_model = PlanCheckDocument.model_validate(raw_checks)
    knowledge_model = DataKnowledge.model_validate(_load_yaml(project_root / ".aa" / "data-knowledge.yaml"))
    review = review_model.model_dump(mode="json")
    checks = checks_model.model_dump(mode="json")
    knowledge = knowledge_model.model_dump(mode="json", by_alias=True)
    events = _load_events(change_dir / "events.jsonl")
    policy = load_policy(project_root)

    execution_projection = TraceProjection.model_validate(raw_execution)
    reconciled_projection = TraceProjection.model_validate(raw_reconciled)
    quality = QualityGateResult.model_validate(raw_quality)
    verify = VerifyResult.model_validate(raw_verify)
    _validate_verify_binding(reconciled_projection, verify)
    coverage, sufficiency = _quality_summary(quality)

    required = sorted(review_model.required_capabilities or [])
    return {
        "schema_version": "1",
        "change_id": change_id,
        "capability_contract_policy": {
            "capabilities": {
                "required": required,
                "missing": compute_missing_capabilities(review, knowledge),
            },
            "contracts": _contract_summary(events, schema_root),
            "mechanical_checks": _mechanical_summary(checks),
            "policy": {
                "source": "project"
                if (project_root / ".aa" / "policy.yaml").exists()
                else "packaged_default",
                "plan_check_action": policy.plan_check_action,
                "digest": policy_digest(policy),
                "recorded_digest": _recorded_policy_digest(events),
            },
            "policy_replay": _policy_replay(
                project_root=project_root,
                schema_root=schema_root,
                change_dir=change_dir,
                review=review,
                checks=checks,
                knowledge=knowledge,
            ),
        },
        "traceability_evidence": {
            "command_status": {"trace_exit": trace_exit, "verify_exit": verify_exit},
            "execution_projection": _projection_summary(execution_projection, reconciled=False),
            "reconciled_projection": _projection_summary(reconciled_projection, reconciled=True),
            "coverage": coverage,
            "sufficiency": sufficiency,
            "verify": {
                "phase": verify.phase,
                "verdict": verify.verdict,
                "policy_digest": verify.policy_digest,
                "projection_digest": verify.projection_digest,
                "blocking_gap_count": len(verify.blocking_gaps),
                "open_problem_count": len(verify.open_problem_ids),
                "reported_insufficient_count": len(verify.insufficient),
                "observed_insufficient_count": sufficiency["insufficient_count"],
            },
        },
    }


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _reason_counts(reasons: dict[str, Any]) -> str:
    return ", ".join(f"{key}:{value}" for key, value in sorted(reasons.items())) or "none"


def evidence_row(report: dict[str, Any], *, expected_change_id: str) -> str:
    change_id = report.get("change_id")
    traceability = report.get("traceability_evidence")
    if not isinstance(change_id, str) or not isinstance(traceability, dict):
        raise ValueError("specialty report is missing change_id or traceability_evidence")
    if change_id != expected_change_id:
        raise ValueError(
            f"specialty report change_id mismatch: expected {expected_change_id!r}, got {change_id!r}"
        )
    execution = traceability.get("execution_projection")
    command_status = traceability.get("command_status")
    verify = traceability.get("verify")
    if (
        not isinstance(command_status, dict)
        or not isinstance(execution, dict)
        or not isinstance(verify, dict)
    ):
        raise ValueError("specialty report is missing command_status, execution_projection, or verify")
    trace_exit = command_status.get("trace_exit")
    verify_exit = command_status.get("verify_exit")
    integrity = execution.get("integrity")
    gaps = execution.get("gap_count")
    verdict = verify.get("verdict")
    blocking = verify.get("blocking_gap_count")
    insufficient = verify.get("reported_insufficient_count")
    if integrity not in {"complete", "degraded", "incomplete"}:
        raise ValueError("specialty report has invalid trace integrity")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (trace_exit, verify_exit, gaps, blocking, insufficient)
    ):
        raise ValueError("specialty report has invalid evidence counts")
    if not isinstance(verdict, str):
        raise ValueError("specialty report has invalid verify verdict")
    if verdict not in {"pass", "needs_human", "fail"}:
        raise ValueError("specialty report has invalid verify verdict")
    return f"{change_id}|{trace_exit}|{integrity}|{gaps}|{verify_exit}|{verdict}|{blocking}|{insufficient}"


def render_sections(reports: list[dict[str, Any]]) -> str:
    reports = sorted(reports, key=lambda item: str(item.get("change_id", "")))
    lines = [
        "## Capability + Contract + Policy",
        "",
        "| change_id | mechanical | checks | findings | capabilities required/missing | execution contracts | output contracts | prompt audit | policy source/action | digest match |",
        "|---|---|---|---:|---|---:|---:|---|---|---|",
    ]
    for report in reports:
        cap = report["capability_contract_policy"]
        checks = (
            ", ".join(
                f"{check_id}={item['status']}({item['finding_count']})"
                for check_id, item in cap["mechanical_checks"]["by_check"].items()
            )
            or "none"
        )
        current_digest = cap["policy"]["digest"]
        recorded_digest = cap["policy"]["recorded_digest"]
        lines.append(
            "| `{}` | {} | {} | {} | {}/{} | {} | {} | {} | {}/{} | {} |".format(
                _cell(report["change_id"]),
                _cell(cap["mechanical_checks"]["status"]),
                _cell(checks),
                cap["mechanical_checks"]["finding_count"],
                len(cap["capabilities"]["required"]),
                len(cap["capabilities"]["missing"]),
                len(cap["contracts"]["agent_execution_contract_digests"]),
                cap["contracts"]["rendered_output_contract_count"],
                _cell(cap["contracts"]["prompt_observability"]),
                _cell(cap["policy"]["source"]),
                _cell(cap["policy"]["plan_check_action"]),
                "yes" if current_digest == recorded_digest else "no",
            )
        )
    lines.extend(
        [
            "",
            "> `derived_not_recorded` means recorded and current graph digests match, so the output contract is reproducible from that schema; the exact dispatched prompt was not persisted. `schema_digest_mismatch` suppresses this derivation.",
            "",
            "### Policy Replay Matrix",
            "",
            "| change_id | warn | block | require_human |",
            "|---|---|---|---|",
        ]
    )
    for report in reports:
        replay = {
            row["action"]: row["verdict"] for row in report["capability_contract_policy"]["policy_replay"]
        }
        lines.append(
            "| `{}` | {} | {} | {} |".format(
                _cell(report["change_id"]),
                _cell(replay.get("warn", "missing")),
                _cell(replay.get("block", "missing")),
                _cell(replay.get("require_human", "missing")),
            )
        )

    lines.extend(
        [
            "",
            "## Traceability / Evidence Projection",
            "",
            "### Execution Projection",
            "",
            "| change_id | phase | batch | integrity | rows | sources | gaps | unmapped tests |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for report in reports:
        item = report["traceability_evidence"]["execution_projection"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(report["change_id"]),
                _cell(item["phase"]),
                _cell(item["batch_id"]),
                _cell(item["integrity"]),
                item["row_count"],
                item["source_count"],
                item["gap_count"],
                item["unmapped_test_count"],
            )
        )
    lines.extend(
        [
            "",
            "### Reconciled Projection",
            "",
            "| change_id | phase | batch | integrity | rows | sources | gaps | unmapped tests | failure rows/links | problem rows/links/unique |",
            "|---|---|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for report in reports:
        item = report["traceability_evidence"]["reconciled_projection"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} | {}/{} | {}/{}/{} |".format(
                _cell(report["change_id"]),
                _cell(item["phase"]),
                _cell(item["batch_id"]),
                _cell(item["integrity"]),
                item["row_count"],
                item["source_count"],
                item["gap_count"],
                item["unmapped_test_count"],
                item["failure_row_count"],
                item["failure_link_count"],
                item["open_problem_row_count"],
                item["open_problem_link_count"],
                item["unique_open_problem_count"],
            )
        )
    lines.extend(
        [
            "",
            "### Evidence Sufficiency and Coverage",
            "",
            "| change_id | sufficient | insufficient | reason counts | evidence coverage | line % | branch % | final status |",
            "|---|---:|---:|---|---|---:|---:|---|",
        ]
    )
    for report in reports:
        evidence = report["traceability_evidence"]
        sufficiency = evidence["sufficiency"]
        coverage = evidence["coverage"]
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(report["change_id"]),
                sufficiency["sufficient_count"],
                sufficiency["insufficient_count"],
                _cell(_reason_counts(sufficiency["reason_counts"])),
                _cell(coverage["status"]),
                _cell(coverage["line"]),
                _cell(coverage["branch"]),
                _cell(coverage["final_status"]),
            )
        )
    lines.extend(["", "### Verify Diagnostics", ""])
    for report in reports:
        verify = report["traceability_evidence"]["verify"]
        lines.append(
            "- `{}`: verdict={}; blocking gaps={}; open problems={}; reported insufficient={}; observed insufficient={}.".format(
                _cell(report["change_id"]),
                _cell(verify["verdict"]),
                verify["blocking_gap_count"],
                verify["open_problem_count"],
                verify["reported_insufficient_count"],
                verify["observed_insufficient_count"],
            )
        )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--project-root", type=Path, required=True)
    collect.add_argument("--schema-root", type=Path, required=True)
    collect.add_argument("--change-id", required=True)
    collect.add_argument("--trace", type=Path, required=True)
    collect.add_argument("--verify", type=Path, required=True)
    collect.add_argument("--trace-exit", type=int, required=True)
    collect.add_argument("--verify-exit", type=int, required=True)
    collect.add_argument("--output", type=Path, required=True)
    render = commands.add_parser("render")
    render.add_argument("reports", nargs="+", type=Path)
    evidence = commands.add_parser("evidence-row")
    evidence.add_argument("--change-id", required=True)
    evidence.add_argument("report", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "collect":
        report = collect_report(
            project_root=args.project_root.resolve(),
            schema_root=args.schema_root.resolve(),
            change_id=args.change_id,
            trace_path=args.trace.resolve(),
            verify_path=args.verify.resolve(),
            trace_exit=args.trace_exit,
            verify_exit=args.verify_exit,
        )
        _atomic_dump(args.output.resolve(), report)
        return 0
    if args.command == "evidence-row":
        print(evidence_row(_load_json(args.report), expected_change_id=args.change_id))
        return 0
    reports = [_load_json(path) for path in args.reports]
    print(render_sections(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
