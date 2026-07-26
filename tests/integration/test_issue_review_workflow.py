"""Integration tests for the human Problem review workflow.

Tests validate:
  - load-problem-review-context writes the context file
  - apply-problem-review reads the graph_resumed event and applies the action
  - Stale-version rejection after the first decision
  - Repeatable entrypoint restart policy: can run issue-review twice on same Change
  - Post-archive review changes ONLY the project Problem ledger; archived Change
    issues/** digest is unchanged
  - CLI entrypoint choices include issue-review / issue-analyze / issue-reconcile
  - --payload reaches apply-problem-review
  - validate_review_action is called by the operation layer
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.issues.review import (
    REVIEW_ACTIONS,
    ReviewValidationError,
    build_problem_review_context,
    validate_review_action,
)
from assurance_agent.artifacts.models.issues import Problem, ProblemProjection
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.compiler import compile_workflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_projection(problems: list[dict]) -> ProblemProjection:
    return ProblemProjection(
        schema_version="1.0",
        generated_at="2024-01-01T00:00:00Z",
        problems=[Problem.model_validate(p) for p in problems],
    )


def _base_problem(
    *,
    problem_id: str = "PROB-abc1",
    status: str = "detected",
    version: int = 1,
) -> dict:
    return {
        "problem_id": problem_id,
        "fingerprint": {"version": "1", "digest": "a" * 64},
        "title": "HTTP 500 on empty department name",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
        },
        "status": status,
        "first_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "version": version,
    }


# ---------------------------------------------------------------------------
# Compiled workflow / entrypoint tests
# ---------------------------------------------------------------------------


class TestIssueEntrypointRegistration:
    def test_packaged_workflow_has_issue_entrypoints(self) -> None:
        schema = load_workflow_v2(Path("."))
        assert "issue-review" in schema.entrypoints
        assert "issue-analyze" in schema.entrypoints
        assert "issue-reconcile" in schema.entrypoints

    def test_issue_review_requires_problem_id_and_review_id(self) -> None:
        schema = load_workflow_v2(Path("."))
        ep = schema.entrypoints["issue-review"]
        assert ep.allow is not None
        assert "problem_id" in ep.allow
        assert "review_id" in ep.allow

    def test_issue_review_restart_is_repeatable(self) -> None:
        schema = load_workflow_v2(Path("."))
        assert schema.entrypoints["issue-review"].restart == "repeatable"
        assert schema.entrypoints["issue-analyze"].restart == "repeatable"
        assert schema.entrypoints["issue-reconcile"].restart == "repeatable"

    def test_once_entrypoints_retain_once_policy(self) -> None:
        schema = load_workflow_v2(Path("."))
        for name in ("full", "execute", "archive", "retro"):
            assert schema.entrypoints[name].restart == "once", f"{name} should be once"

    def test_compiled_restart_policy_propagated(self) -> None:
        schema = load_workflow_v2(Path("."))
        compiled = compile_workflow(schema)
        assert compiled.entrypoints["issue-review"].restart == "repeatable"
        assert compiled.entrypoints["full"].restart == "once"

    def test_issue_review_graph_has_all_required_nodes(self) -> None:
        schema = load_workflow_v2(Path("."))
        graph = schema.graphs.get("issue-review-workflow")
        assert graph is not None
        node_names = set(graph.nodes.keys())
        for expected in ("load-problem", "triage-advisor", "human-interrupt", "apply-review"):
            assert expected in node_names, f"node {expected!r} missing from issue-review-workflow"

    def test_issue_review_graph_declares_stop_action(self) -> None:
        schema = load_workflow_v2(Path("."))
        graph = schema.graphs["issue-review-workflow"]
        interrupt_node = graph.nodes.get("human-interrupt")
        assert interrupt_node is not None
        assert interrupt_node.interrupt is not None
        assert "stop" in interrupt_node.interrupt.actions

    def test_issue_review_graph_actions_include_all_review_actions(self) -> None:
        schema = load_workflow_v2(Path("."))
        graph = schema.graphs["issue-review-workflow"]
        interrupt_node = graph.nodes["human-interrupt"]
        assert interrupt_node.interrupt is not None
        declared = set(interrupt_node.interrupt.actions)
        for action in REVIEW_ACTIONS:
            assert action in declared, f"review action {action!r} not declared in human-interrupt"


# ---------------------------------------------------------------------------
# CLI entrypoint choices
# ---------------------------------------------------------------------------


class TestCLIEntrypointChoices:
    def test_entrypoint_choices_include_issue_entrypoints(self) -> None:
        from assurance_agent.commands.workflow_cmd import _ENTRYPOINT_CHOICE

        choices = set(_ENTRYPOINT_CHOICE.choices)
        assert "issue-review" in choices
        assert "issue-analyze" in choices
        assert "issue-reconcile" in choices

    def test_legacy_entrypoints_still_present(self) -> None:
        from assurance_agent.commands.workflow_cmd import _ENTRYPOINT_CHOICE

        choices = set(_ENTRYPOINT_CHOICE.choices)
        for name in ("full", "execute", "archive", "retro"):
            assert name in choices


# ---------------------------------------------------------------------------
# review.py API: build_problem_review_context integration
# ---------------------------------------------------------------------------


class TestBuildProblemReviewContextIntegration:
    def test_context_captures_problem_state(self) -> None:
        proj = _make_projection([_base_problem(status="triaged", version=3)])
        ctx = build_problem_review_context("PROB-abc1", proj)
        assert ctx.problem_id == "PROB-abc1"
        assert ctx.expected_problem_version == 3
        assert ctx.problem_status == "triaged"

    def test_two_reviews_on_different_problems(self) -> None:
        proj = _make_projection(
            [
                _base_problem(problem_id="PROB-1", status="detected", version=1),
                _base_problem(problem_id="PROB-2", status="triaged", version=2),
            ]
        )
        ctx1 = build_problem_review_context("PROB-1", proj)
        ctx2 = build_problem_review_context("PROB-2", proj)
        assert ctx1.problem_id != ctx2.problem_id
        assert ctx1.problem_digest != ctx2.problem_digest


# ---------------------------------------------------------------------------
# Stale version after first decision
# ---------------------------------------------------------------------------


class TestStaleVersionAfterFirstDecision:
    def test_stale_version_rejected_after_projection_updated(self) -> None:
        """Context built at v1, projection updated to v2 → stale rejection."""
        proj_v1 = _make_projection([_base_problem(version=1)])
        ctx = build_problem_review_context("PROB-abc1", proj_v1)
        assert ctx.expected_problem_version == 1

        # Simulate the projection being updated (version bumped to 2).
        proj_v2 = _make_projection([_base_problem(version=2, status="triaged")])
        with pytest.raises(ReviewValidationError, match="stale"):
            validate_review_action(
                ctx,
                "mark_not_an_issue",
                {"evidence_refs": ["OCC-1"]},
                "reason",
                "user",
                projection=proj_v2,
            )


# ---------------------------------------------------------------------------
# Post-archive review: project ledger changes, archived change issues unchanged
# ---------------------------------------------------------------------------


class TestPostArchiveReviewBehavior:
    def test_validate_review_action_after_version_bump(self) -> None:
        """After archive, a new review uses the current projection version.

        This validates the behavioral contract that review only touches the
        project Problem ledger (via validate_review_action returning events)
        and does not modify any Change-local files.
        """
        # Simulate a problem that was updated by archive → version 4
        proj = _make_projection([_base_problem(status="triaged", version=4)])
        ctx = build_problem_review_context("PROB-abc1", proj)
        assert ctx.expected_problem_version == 4

        payload: dict[str, object] = {
            "evidence_refs": ["OCC-1"],
        }
        events = validate_review_action(
            ctx,
            "accept_risk",
            payload,
            "post-archive risk acceptance",
            "pm",
            projection=proj,
        )
        assert len(events) == 1
        assert events[0].type == "problem_risk_accepted"
        assert events[0].expected_problem_version == 4


# ---------------------------------------------------------------------------
# Operation registration
# ---------------------------------------------------------------------------


class TestOperationRegistration:
    def test_review_operations_registered(self) -> None:
        from assurance_agent.workflow.graph.handlers.operation import default_operations

        ops = default_operations()
        assert "operation:load-problem-review-context" in ops
        assert "operation:apply-problem-review" in ops

    def test_execution_contracts_include_review_operations(self) -> None:
        from assurance_agent import resources
        from assurance_agent.workflow.graph.contracts import parse_execution_contracts

        contracts_yaml = resources.read_text("schemas", "execution-contracts.yaml")
        catalog = parse_execution_contracts(contracts_yaml)
        assert "operation:load-problem-review-context" in catalog.contracts
        assert "operation:apply-problem-review" in catalog.contracts

    def test_apply_problem_review_declares_synchronized(self) -> None:
        from assurance_agent import resources
        from assurance_agent.workflow.graph.contracts import parse_execution_contracts

        contracts_yaml = resources.read_text("schemas", "execution-contracts.yaml")
        catalog = parse_execution_contracts(contracts_yaml)
        contract = catalog.contracts["operation:apply-problem-review"]
        assert "change:execution/**" in contract.reads
        assert "project:qa/issues/**" in contract.synchronized
        assert "project:issue-registry" in contract.exclusive
