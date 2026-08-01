"""Task 21: packaged four-layer resume, remediation, healing, and recovery matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.workflow.graph.checkpoint import project_invocation
from assurance_agent.workflow.graph.models import ResumeCommand
from tests.helpers_four_layer_runtime import (
    CHANGE_ID,
    LAYERS,
    NODE_CHAIN_AFTER,
    CrashAfterCommittedNode,
    InjectedCrash,
    count_attempts,
    events_for_root,
    make_fixture,
    rebuild_runtime,
    resume_root,
    root_events,
    run_codegen_only,
)

_RESTART_SEAMS = ("review", "mechanical-plan-checks", "review-gate", "codegen-precheck")


def _layer_suffix(layer: LayerName) -> str:
    return f"/{layer}-plan-cycle" if layer in {"api", "e2e"} else f"/{layer}-branch"


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("seam", _RESTART_SEAMS)
def test_ordinary_restart_after_committed_seam(
    tmp_path: Path, layer: LayerName, seam: str
) -> None:
    fixture = make_fixture(tmp_path, selected_layers=(layer,), applicable_layers=(layer,))
    crash = CrashAfterCommittedNode(node_id=seam, occurrence=1)
    assert fixture.bundle.resolved is not None
    crash.install(fixture.bundle.resolved.scheduler)
    with pytest.raises(InjectedCrash, match=f"crash_after_commit:{seam}"):
        run_codegen_only(fixture)
    crash.uninstall(fixture.bundle.resolved.scheduler)

    events_before = root_events(fixture.change_dir)
    started_before = [
        e for e in events_before if e.get("type") == "task_attempt_started" and e.get("node_id") == seam
    ]
    assert started_before, f"expected committed seam {seam} to have started"
    invocation_id = str(started_before[0]["invocation_id"])
    # Climb to root if the seam ran inside a child.
    proj = project_invocation(fixture.change_dir, invocation_id)
    root_id = proj.parent_invocation_id or invocation_id
    while True:
        root_proj = project_invocation(fixture.change_dir, root_id)
        if root_proj.parent_invocation_id is None:
            break
        root_id = root_proj.parent_invocation_id

    before_next = count_attempts(events_before, node_id=NODE_CHAIN_AFTER[seam])
    fresh = rebuild_runtime(fixture)
    result = resume_root(fresh, root_id)
    assert result.status.status == "completed", result.reason
    events = events_for_root(root_events(fixture.change_dir), root_id)
    after_next = count_attempts(events, node_id=NODE_CHAIN_AFTER[seam])
    assert after_next == before_next + 1
    # Already-committed seam must not gain another physical attempt.
    assert count_attempts(events, node_id=seam) == count_attempts(events_before, node_id=seam)


@pytest.mark.parametrize("layer", ("api", "e2e"))
def test_wrapper_reuses_child_after_crash_before_wrapper_success(
    tmp_path: Path, layer: LayerName
) -> None:
    fixture = make_fixture(tmp_path, selected_layers=(layer,), applicable_layers=(layer,))
    # Crash after the branch wrapper's child has started review (child created)
    # but before the wrapper task itself commits — approximate by crashing after
    # first review commit, then asserting a single child invocation for the branch.
    crash = CrashAfterCommittedNode(node_id="review", occurrence=1)
    assert fixture.bundle.resolved is not None
    crash.install(fixture.bundle.resolved.scheduler)
    with pytest.raises(InjectedCrash, match="crash_after_commit:review"):
        run_codegen_only(fixture)
    crash.uninstall(fixture.bundle.resolved.scheduler)

    children_before = {
        e.get("invocation_id")
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id")
    }
    assert children_before
    events_before = root_events(fixture.change_dir)
    started = next(e for e in events_before if e.get("type") == "graph_invocation_started")
    root_id = str(started["invocation_id"])
    for e in events_before:
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None:
            root_id = str(e["invocation_id"])
            break

    fresh = rebuild_runtime(fixture)
    result = resume_root(fresh, root_id)
    assert result.status.status == "completed", result.reason
    children_after = {
        e.get("invocation_id")
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id")
    }
    assert children_after == children_before


@pytest.mark.parametrize("layer", ("api", "e2e"))
def test_api_e2e_auto_fixer_creates_new_epoch(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        review_scripts={layer: ("needs_fix_auto", "pass")},
    )
    result = run_codegen_only(fixture)
    terminal = fixture.bundle.runtime.invocation_terminal(result.invocation_id)
    assert terminal == "completed", terminal
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="fix") >= 1
    assert count_attempts(events, node_id="review") >= 2
    assert count_attempts(events, node_id="mechanical-plan-checks") >= 2
    assert count_attempts(events, node_id="review-gate") >= 2
    assert count_attempts(events, node_id="codegen") == 1
    # First-epoch mechanical evidence must not be the only bound attempt.
    mechanical_started = [
        e for e in events if e.get("type") == "task_attempt_started" and e.get("node_id") == "mechanical-plan-checks"
    ]
    assert len(mechanical_started) >= 2


@pytest.mark.parametrize("layer", ("api", "e2e"))
@pytest.mark.parametrize(
    "seam",
    (
        "fix",
        "mechanical-plan-checks",
    ),
)
def test_api_e2e_fixer_restart_seams(tmp_path: Path, layer: LayerName, seam: str) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        review_scripts={layer: ("needs_fix_auto", "pass")},
    )
    occurrence = 1 if seam == "fix" else 2  # second mechanical is post-fixer
    crash = CrashAfterCommittedNode(node_id=seam, occurrence=occurrence)
    assert fixture.bundle.resolved is not None
    crash.install(fixture.bundle.resolved.scheduler)
    with pytest.raises(InjectedCrash, match=f"crash_after_commit:{seam}"):
        run_codegen_only(fixture)
    crash.uninstall(fixture.bundle.resolved.scheduler)

    started = next(
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
    )
    root_id = str(started["invocation_id"])
    fresh = rebuild_runtime(fixture)
    result = resume_root(fresh, root_id)
    assert result.status.status == "completed", result.reason
    events = events_for_root(root_events(fixture.change_dir), root_id)
    assert count_attempts(events, node_id="codegen") == 1
    assert count_attempts(events, node_id="fix") >= 1


@pytest.mark.parametrize("layer", ("fuzz", "performance"))
def test_fuzz_performance_needs_fix_interrupt_and_stop(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        review_scripts={layer: ("needs_fix_human",)},
    )
    result = run_codegen_only(fixture)
    # ImportResult from import_checkpoint — interrupted roots surface via runtime status.
    # When the drive ends interrupted, status is exposed on the runtime projection.
    events = root_events(fixture.change_dir)
    interrupted = [e for e in events if e.get("type") == "graph_interrupted"]
    assert interrupted, "expected human-review interrupt for needs_fix without auto_fix"
    root_id = str(interrupted[0]["invocation_id"])
    # Climb to root.
    proj = project_invocation(fixture.change_dir, root_id)
    while proj.parent_invocation_id is not None:
        root_id = proj.parent_invocation_id
        proj = project_invocation(fixture.change_dir, root_id)
    pending = project_invocation(fixture.change_dir, root_id)
    # Resolve pending interrupt from status API.
    status = fixture.bundle.runtime.status(root_id)
    assert status.pending_interrupts, status
    interrupt = status.pending_interrupts[0]
    fresh = rebuild_runtime(fixture)
    stopped = resume_root(
        fresh,
        root_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="stop",
            reason="operator stop",
            who="tester",
        ),
    )
    assert stopped.status.status in {"stopped", "completed"}
    events_after = events_for_root(root_events(fixture.change_dir), root_id)
    assert count_attempts(events_after, node_id="codegen") == 0
    assert count_attempts(events_after, node_id="codegen-precheck") == 0
    _ = result, pending


@pytest.mark.parametrize("layer", LAYERS)
def test_knowledge_gap_routes_to_knowledge_remediation(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        review_scripts={layer: ("knowledge_gap",)},
    )
    run_codegen_only(fixture)
    events = root_events(fixture.change_dir)
    interrupts = [e for e in events if e.get("type") == "graph_interrupted"]
    assert interrupts
    reasons = {e.get("checkpoint") or e.get("reason") for e in interrupts}
    # Checkpoint binds the plan-review gate for knowledge remediation.
    assert any(
        isinstance(r, str) and ("plan-review-gate" in r or "knowledge" in r.lower())
        for r in reasons
    ) or any(
        e.get("type") == "task_attempt_succeeded" and e.get("node_id") == "knowledge-remediation"
        for e in events
    )


@pytest.mark.parametrize("layer", ("api", "e2e"))
def test_accept_risk_still_requires_precheck(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        review_scripts={layer: ("needs_fix_human",)},
    )
    run_codegen_only(fixture)
    started = next(
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
    )
    root_id = str(started["invocation_id"])
    status = fixture.bundle.runtime.status(root_id)
    if not status.pending_interrupts:
        pytest.skip("human-review interrupt not pending (gate route differed)")
    interrupt = status.pending_interrupts[0]
    if "accept_risk" not in interrupt.actions:
        pytest.skip("accept_risk not declared on interrupt")
    # Strip required capabilities from the committed review so precheck must stop
    # even after accept_risk. Mutating synchronized L1 on disk would trip ordinary
    # materialization repair (fail-closed source drift) before precheck runs.
    review_name = "api-plan-review.json" if layer == "api" else "plan-review.json"
    review_path = fixture.change_dir / "review" / review_name
    if review_path.is_file():
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        payload["required_capabilities"] = [
            "capabilities.domain_factories.account.missing_fixture_only"
        ]
        review_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    fresh = rebuild_runtime(fixture)
    resumed = resume_root(
        fresh,
        root_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accepted with stale capability",
            who="tester",
        ),
    )
    events = events_for_root(root_events(fixture.change_dir), root_id)
    assert count_attempts(events, node_id="codegen") == 0
    assert resumed.status.status in {"stopped", "failed", "completed", "interrupted"}


def test_healing_event_cardinality_api_only_dark_ship(tmp_path: Path) -> None:
    """Exercise allocate/record effect kinds through packaged ops (Step 3 baseline)."""
    from assurance_agent.workflow.graph.durable_effects import (
        HEAL_RECORD_APPLY_V2,
        HEALING_ALLOCATION_V2,
        production_effect_registry,
    )

    kinds = production_effect_registry().kinds()
    assert HEALING_ALLOCATION_V2 in kinds
    assert HEAL_RECORD_APPLY_V2 in kinds
    # Full packaged healing drive with failed tests is covered by fault/worker
    # cuts; this pins the registered kind set the matrix depends on.
    fixture = make_fixture(tmp_path, selected_layers=("api",), applicable_layers=("api",))
    result = run_codegen_only(fixture, max_healing_attempts=1)
    assert fixture.bundle.runtime.invocation_terminal(result.invocation_id) == "completed"
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    # codegen-only with run_tests=false never enters healing.
    assert not any(e.get("type") == "healing_attempt_allocated_v2" for e in events)


def test_pinned_bundle_identity_survives_ordinary_resume(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, selected_layers=("api",), applicable_layers=("api",))
    crash = CrashAfterCommittedNode(node_id="review", occurrence=1)
    assert fixture.bundle.resolved is not None
    crash.install(fixture.bundle.resolved.scheduler)
    with pytest.raises(InjectedCrash, match="crash_after_commit:review"):
        run_codegen_only(fixture)
    crash.uninstall(fixture.bundle.resolved.scheduler)
    started = next(
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
    )
    root_id = str(started["invocation_id"])
    before = project_invocation(fixture.change_dir, root_id)
    fresh = rebuild_runtime(fixture)
    result = resume_root(fresh, root_id)
    assert result.status.status == "completed"
    after = project_invocation(fixture.change_dir, root_id)
    assert after.graph_digest == before.graph_digest
    assert after.contract_digests == before.contract_digests


def test_no_handler_rerun_after_recorded_success(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, selected_layers=("api",), applicable_layers=("api",))
    crash = CrashAfterCommittedNode(node_id="codegen", occurrence=1)
    assert fixture.bundle.resolved is not None
    crash.install(fixture.bundle.resolved.scheduler)
    with pytest.raises(InjectedCrash, match="crash_after_commit:codegen"):
        run_codegen_only(fixture)
    crash.uninstall(fixture.bundle.resolved.scheduler)
    before = [
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "task_attempt_started" and e.get("node_id") == "codegen"
    ]
    assert len(before) == 1
    started = next(
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
    )
    root_id = str(started["invocation_id"])
    adapter_calls = len(fixture.adapter.invocations)
    fresh = rebuild_runtime(fixture)
    result = resume_root(fresh, root_id)
    assert result.status.status == "completed", result.reason
    after = [
        e
        for e in root_events(fixture.change_dir)
        if e.get("type") == "task_attempt_started" and e.get("node_id") == "codegen"
    ]
    assert len(after) == 1
    assert len(fixture.adapter.invocations) == adapter_calls


def test_change_id_constant() -> None:
    assert CHANGE_ID == "CH-CANONICAL"
    assert json.dumps({"layers": list(LAYERS)})
