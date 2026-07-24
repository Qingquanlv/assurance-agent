"""End-to-end unit chains for knowledge loop remediation (spec C6/C7 acceptance)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.knowledge.promote import promote_knowledge
from assurance_agent.retro.export import export_proposals
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gates import (
    GateEvaluationContext,
    check_gate_in_view,
)
from assurance_agent.workflow.orchestration.schema import normalize_gates
from tests.helpers_aa import write_aa_config
from tests.unit.retro.proposal_fixtures import knowledge_proposal

FIXTURES = Path(__file__).resolve().parents[1] / "artifacts" / "fixtures" / "data_knowledge"


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


def test_export_knowledge_then_promote_from_merges_auth_leaf(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))

    retro_dir = tmp_path / "qa" / "retro" / "retro-chain"
    retro_dir.mkdir(parents=True)
    proposal = knowledge_proposal()
    retro_dir.joinpath("proposals.json").write_text(
        json.dumps({"proposals": [proposal.model_dump(mode="json")]}, indent=2),
        encoding="utf-8",
    )

    outcomes = export_proposals(retro_dir, apply_kind="knowledge_delta")
    assert len(outcomes) == 1
    exported_path = outcomes[0].target_path
    assert exported_path.name.endswith(".proposal.yaml")

    outcome = promote_knowledge(tmp_path, proposal_path=exported_path, yes=True)
    assert outcome.changed is True
    merged = yaml.safe_load((tmp_path / ".aa/data-knowledge.yaml").read_text(encoding="utf-8"))
    assert merged["auth"]["api_admin_token"]["method"] == "token"


def test_plan_review_route_dsl_selects_knowledge_remediation_node(tmp_path: Path) -> None:
    ctx = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
        state_values={},
        node_results={
            "review": {
                "gate": {
                    "verdict": "needs_human_review",
                    "details": {"missing_capabilities": ["capabilities.domain_factories.api.make_api"]},
                }
            }
        },
    )

    def node_result(node_id: str) -> object:
        result = ctx.node_results.get(node_id)
        return result if isinstance(result, dict) else {}

    scope = Scope({"params": {}, "state": {}}, node_result=node_result)
    label = evaluate(parse_expression("plan_review_route('review')"), scope)
    assert label == "knowledge_remediation"

    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    ctx.change_dir.mkdir(parents=True)
    _write(
        ctx.change_dir,
        "review/api-plan-review.json",
        json.dumps(
            {
                "schema_version": "1",
                "decision": "pass",
                "findings": [],
                "codegen_readiness": "ready",
                "required_capabilities": ["capabilities.domain_factories.api.make_api"],
            }
        ),
    )
    report = check_gate_in_view(GATES, "api-plan-review-gate", ctx)
    assert report.verdict.value == "needs_human_review"
    assert report.details is not None
    assert "make_api" in report.details["missing_capabilities"][0]
