"""Runtime imports must not mutate graph modules behind their public APIs."""

from __future__ import annotations

from assurance_agent.workflow.graph import attempt_engine, leases, planner, scheduler


def test_graph_behavior_is_owned_by_its_declaring_module_after_runtime_import() -> None:
    import assurance_agent.workflow.graph.runtime  # noqa: F401

    owners = {
        "lease_decision": leases.next_attempt_decision.__module__,
        "scheduler_decision": scheduler.next_attempt_decision.__module__,
        "persist_result": attempt_engine.AttemptEngine._persist_result.__module__,
        "freeze_if_needed": attempt_engine.AttemptEngine._freeze_if_needed.__module__,
        "resolve_graph": planner._resolve_graph.__module__,
        "build_scope": planner._build_scope.__module__,
        "seed_outcomes": planner._seed_outcomes.__module__,
    }

    assert owners == {
        "lease_decision": leases.__name__,
        "scheduler_decision": leases.__name__,
        "persist_result": attempt_engine.__name__,
        "freeze_if_needed": attempt_engine.__name__,
        "resolve_graph": planner.__name__,
        "build_scope": planner.__name__,
        "seed_outcomes": planner.__name__,
    }
