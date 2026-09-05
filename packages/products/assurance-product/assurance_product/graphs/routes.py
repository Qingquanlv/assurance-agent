from __future__ import annotations

from collections.abc import Mapping

from graph_engine.stategraph.routing import select_exclusive_route

_NOT_ACHIEVED = "not-achieved"


def _has_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used < budget


def prepare_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"prepared": "prepared" if state.get("status") == "prepared" else None}


def case_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    decision = state.get("decision")
    reviewed = state.get("status") in {"passed", "reviewed"} and decision in {
        "pass",
        "approved",
    }
    return {"reviewed": "execute-tail" if reviewed else None}


def execute_tail_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"satisfied": "retro" if state.get("coverage_state") == "satisfied" else None}


def execute_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    status = state.get("status")
    return {
        "passed": "quality" if status == "passed" else None,
        "failed": "failed-join" if status == "failed" else None,
    }


def run_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return execute_named_matches(state)


def issue_analysis_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    classification = state.get("classification")
    eligible = state.get("fix_eligible") is True
    fixable = classification in {"test", "test-data"} and eligible and _has_budget(state)
    return {
        "fix_eligible": "fix-proposal" if fixable else None,
        "product_bug": "report-issue" if classification == "product_bug" else None,
        "environment_failure": "report-issue" if classification == "environment_failure" else None,
        "infrastructure_failure": "report-issue" if classification == "infrastructure_failure" else None,
    }


def quality_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    coverage = state.get("coverage_state")
    return {
        "repair_required": "coverage-needed"
        if coverage == "repair_required" and _has_budget(state)
        else None,
        "satisfied": "assess-satisfied" if coverage == "satisfied" else None,
        "exhausted": "assess-unsatisfied" if coverage == "exhausted" else None,
        "inconclusive": "assess-unsatisfied" if coverage == "inconclusive" else None,
        "needs_human": "coverage-human" if coverage == "needs_human" else None,
    }


def quality_recheck_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return quality_named_matches(state)


def coverage_repair_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    status = state.get("status")
    return {
        "repaired": "quality-recheck" if status == "repaired" else None,
        "exhausted": "report-unsatisfied-repair" if status == "exhausted" else None,
        "not_eligible": "report-unsatisfied-repair" if status == "not_eligible" else None,
        "failed": "report-unsatisfied-repair" if status == "failed" else None,
    }


def coverage_decision_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    action = state.get("coverage_decision") or state.get("human_action")
    return {
        "approve": "assess-satisfied" if action == "approve" else None,
        "reject": "not-achieved" if action == "reject" else None,
    }


def route_prepare(state: Mapping[str, object]) -> str:
    return select_exclusive_route(prepare_named_matches(state), otherwise="failed")


def route_case(state: Mapping[str, object]) -> str:
    return select_exclusive_route(case_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_execute_tail(state: Mapping[str, object]) -> str:
    return select_exclusive_route(execute_tail_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_execute(state: Mapping[str, object]) -> str:
    return select_exclusive_route(execute_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_run(state: Mapping[str, object]) -> str:
    return select_exclusive_route(run_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_issue_analysis(state: Mapping[str, object]) -> str:
    return select_exclusive_route(issue_analysis_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_quality(state: Mapping[str, object]) -> str:
    return select_exclusive_route(quality_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_quality_recheck(state: Mapping[str, object]) -> str:
    return select_exclusive_route(quality_recheck_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_coverage_repair(state: Mapping[str, object]) -> str:
    return select_exclusive_route(coverage_repair_named_matches(state), otherwise=_NOT_ACHIEVED)


def route_coverage_decision(state: Mapping[str, object]) -> str:
    return select_exclusive_route(coverage_decision_named_matches(state), otherwise=_NOT_ACHIEVED)


PRODUCT_EXCLUSIVE_ROUTES = {
    "prepare": route_prepare,
    "case": route_case,
    "execute-tail": route_execute_tail,
    "execute": route_execute,
    "run": route_run,
    "issue-analysis": route_issue_analysis,
    "quality": route_quality,
    "quality-recheck": route_quality_recheck,
    "coverage-repair": route_coverage_repair,
}


__all__ = [
    "PRODUCT_EXCLUSIVE_ROUTES",
    "coverage_decision_named_matches",
    "coverage_repair_named_matches",
    "case_named_matches",
    "execute_named_matches",
    "execute_tail_named_matches",
    "issue_analysis_named_matches",
    "prepare_named_matches",
    "quality_named_matches",
    "quality_recheck_named_matches",
    "route_coverage_decision",
    "route_coverage_repair",
    "route_execute",
    "route_execute_tail",
    "route_issue_analysis",
    "route_case",
    "route_prepare",
    "route_quality",
    "route_quality_recheck",
    "route_run",
    "run_named_matches",
]
