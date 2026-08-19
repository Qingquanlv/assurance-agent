import json
from pathlib import Path

import yaml

from assurance_agent.workflow.orchestration.gates import (
    GateEvaluationContext,
    check_gate_in_view,
)
from assurance_agent.workflow.orchestration.schema import normalize_gates


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


GATES = normalize_gates(
    yaml.safe_load(
        """
  api-plan-review-gate:
    reads:
      - review/api-plan-review.json
      - { path: repo:.aa/data-knowledge.yaml, as: data_knowledge }
    invalid_json: stop
    missing_field_is: stop
    stop_when: >
      not defined(api_plan_review.required_capabilities)
      or api_plan_review.required_capabilities == null
      or len(api_plan_review.required_capabilities) == 0
    needs_fix_when: "api_plan_review.decision == 'needs_fix' and api_plan_review.auto_fix_allowed == true"
    needs_human_review_when: >
      (
        defined(api_plan_review.required_capabilities)
        and len(api_plan_review.required_capabilities) > 0
        and not capabilities_present(api_plan_review, data_knowledge)
      )
      or api_plan_review.decision == 'needs_human_review'
    reject_when: "api_plan_review.decision == 'reject'"
    pass_when: >
      api_plan_review.decision == 'pass'
      and api_plan_review.codegen_readiness in ['ready','ready_with_warnings']
      and capabilities_present(api_plan_review, data_knowledge)
"""
    )
)


def _context(tmp_path: Path) -> GateEvaluationContext:
    return GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
    )


def _review(**overrides) -> dict:
    payload = {
        "schema_version": "1",
        "decision": "pass",
        "findings": [],
        "codegen_readiness": "ready",
        "required_capabilities": ["auth.api_admin_token"],
    }
    payload.update(overrides)
    return payload


def _l1_yaml() -> str:
    return """
version: 1
accounts: {}
auth:
  api_admin_token:
    method: token
entities: {}
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
"""


def test_plan_review_gate_stops_when_required_capabilities_missing(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.change_dir.mkdir(parents=True)
    _write(ctx.change_dir, "review/api-plan-review.json", json.dumps(_review(required_capabilities=None)))
    _write(tmp_path, ".aa/data-knowledge.yaml", _l1_yaml())
    report = check_gate_in_view(GATES, "api-plan-review-gate", ctx)
    assert report.verdict.value == "stop"


def test_plan_review_gate_needs_human_review_when_leaf_missing(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.change_dir.mkdir(parents=True)
    _write(
        ctx.change_dir,
        "review/api-plan-review.json",
        json.dumps(
            _review(
                required_capabilities=[
                    "auth.api_admin_token",
                    "capabilities.domain_factories.api.make_api",
                ]
            )
        ),
    )
    _write(tmp_path, ".aa/data-knowledge.yaml", _l1_yaml())
    report = check_gate_in_view(GATES, "api-plan-review-gate", ctx)
    assert report.verdict.value == "needs_human_review"
    assert report.details is not None
    assert report.details["missing_capabilities"] == ["capabilities.domain_factories.api.make_api"]


def test_plan_review_gate_passes_when_leaves_present(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.change_dir.mkdir(parents=True)
    _write(ctx.change_dir, "review/api-plan-review.json", json.dumps(_review()))
    _write(tmp_path, ".aa/data-knowledge.yaml", _l1_yaml())
    report = check_gate_in_view(GATES, "api-plan-review-gate", ctx)
    assert report.verdict.value == "pass"
    assert report.details == {"missing_capabilities": []}


def test_plan_review_gate_reads_host_l1_not_workspace_forgery(tmp_path: Path) -> None:
    host = tmp_path / "sut"
    workspace = tmp_path / "task-ws"
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    _write(change, "review/api-plan-review.json", json.dumps(_review()))
    _write(host, ".aa/data-knowledge.yaml", _l1_yaml())
    _write(
        workspace,
        ".aa/data-knowledge.yaml",
        "version: 1\ncapabilities:\n  domain_factories: {}\n",
    )
    ctx = GateEvaluationContext(
        project_root=workspace,
        repo_root=workspace,
        change_dir=change,
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
        host_project_root=host,
    )
    report = check_gate_in_view(GATES, "api-plan-review-gate", ctx)
    assert report.verdict.value == "pass"
