"""Real packaged GraphRuntime four-by-two and multi-layer codegen-only matrix."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.models import ImportResult
from tests.helpers_four_layer_runtime import (
    CHANGE_ID,
    LAYERS,
    BarrierNodeRunner,
    CoordinatorFaultInjector,
    FourLayerDeterministicAdapter,
    assert_no_plan_or_run_tests,
    count_attempts,
    events_for_root,
    make_fixture,
    root_events,
    run_codegen_only,
)

_REVIEWER = {
    "api": "skill:aa-api-plan-reviewer",
    "e2e": "skill:aa-e2e-plan-reviewer",
    "fuzz": "skill:aa-fuzz-plan-reviewer",
    "performance": "skill:aa-performance-plan-reviewer",
}
_CODEGEN = {
    "api": "skill:aa-api-codegen",
    "e2e": "skill:aa-e2e-codegen",
    "fuzz": "skill:aa-fuzz-codegen",
    "performance": "skill:aa-performance-codegen",
}


def _status(fixture, invocation_id: str) -> str:
    return fixture.bundle.runtime.status(invocation_id).status


def _assert_applicable_cell(fixture, layer: str, invocation_id: str) -> None:
    events = events_for_root(root_events(fixture.change_dir), invocation_id)
    assert_no_plan_or_run_tests(events)
    assert count_attempts(events, node_id="applicability") >= 1
    if layer in {"fuzz", "performance"}:
        assert count_attempts(events, node_id="applicability-preflight") == 1
    assert count_attempts(events, node_id="review") >= 1
    assert count_attempts(events, node_id="mechanical-plan-checks") >= 1
    assert count_attempts(events, node_id="review-gate") >= 1
    assert count_attempts(events, node_id="codegen-precheck") >= 1
    assert count_attempts(events, node_id="codegen") >= 1
    assert count_attempts(events, node_id="plan") == 0
    targets = {inv.target for inv in fixture.adapter.invocations}
    assert _REVIEWER[layer] in targets
    assert _CODEGEN[layer] in targets
    assert not (
        targets
        & {"skill:aa-api-plan", "skill:aa-e2e-plan", "skill:aa-fuzz-plan", "skill:aa-performance-plan"}
    )
    profile = get_layer_assurance_profile(layer)
    assert (fixture.change_dir / profile.review_artifact).is_file()
    assert (fixture.change_dir / profile.checks_artifact).is_file()
    assert (fixture.change_dir / f"codegen/{layer}-codegen-summary.md").is_file() or (
        fixture.change_dir / f"codegen/{layer.replace('performance', 'performance')}-codegen-summary.md"
    ).is_file()
    gf_summary = fixture.change_dir / f"codegen/{layer}-codegen-summary.md"
    gf_manifest = fixture.change_dir / f"codegen/{layer}-generated-files.json"
    assert gf_summary.is_file()
    assert gf_manifest.is_file()
    private = {
        "api": "tests/api",
        "e2e": "tests/e2e",
        "fuzz": "tests/fuzz",
        "performance": "tests/perf",
    }[layer]
    written = list((fixture.project_root / private).rglob("test_*.py")) + list(
        (fixture.project_root / private).rglob("locustfile_*.py")
    )
    assert written, f"expected private test write under {private}"
    assert _status(fixture, invocation_id) == "completed"
    assert any(e.get("type") == "graph_completed" for e in events)


def _assert_inapplicable_cell(fixture, layer: str, invocation_id: str) -> None:
    events = events_for_root(root_events(fixture.change_dir), invocation_id)
    assert_no_plan_or_run_tests(events)
    assert count_attempts(events, node_id="applicability") >= 1
    if layer in {"fuzz", "performance"}:
        assert count_attempts(events, node_id="applicability-preflight") == 1
    assert count_attempts(events, node_id="mechanical-plan-checks") == 1
    assert count_attempts(events, node_id="review") == 0
    assert count_attempts(events, node_id="codegen") == 0
    targets = {inv.target for inv in fixture.adapter.invocations}
    assert _REVIEWER[layer] not in targets
    assert _CODEGEN[layer] not in targets
    profile = get_layer_assurance_profile(layer)
    # Mechanical N/A checks are current; review/codegen outputs must not be produced.
    assert (fixture.change_dir / profile.checks_artifact).is_file()
    assert not (fixture.change_dir / f"codegen/{layer}-generated-files.json").exists()
    assert _status(fixture, invocation_id) == "completed"


@pytest.mark.parametrize("layer", LAYERS)
def test_applicable_codegen_only_cell(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(tmp_path, selected_layers=(layer,), applicable_layers=(layer,))
    result = run_codegen_only(fixture)
    _assert_applicable_cell(fixture, layer, result.invocation_id)


@pytest.mark.parametrize("layer", LAYERS)
def test_inapplicable_codegen_only_cell(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(),
        inapplicable_layers=(layer,),
    )
    result = run_codegen_only(fixture)
    _assert_inapplicable_cell(fixture, layer, result.invocation_id)


@pytest.mark.parametrize("layer", LAYERS)
def test_applicability_error_does_not_become_skip(tmp_path: Path, layer: LayerName) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=(layer,),
        applicable_layers=(layer,),
        applicability_error_layer=layer,
    )
    result = run_codegen_only(fixture)
    status = _status(fixture, result.invocation_id)
    assert status in {"failed", "stopped"}
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="codegen") == 0
    assert count_attempts(events, node_id="review") == 0


def test_stale_review_bytes_do_not_replace_missing_reviewer_output(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api",),
        applicable_layers=("api",),
        seed_stale_artifacts=True,
        mutations={"skill:aa-api-plan-reviewer": "missing_review"},
    )
    result = run_codegen_only(fixture)
    assert _status(fixture, result.invocation_id) in {"failed", "stopped"}
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="review") >= 1
    assert count_attempts(events, node_id="codegen") == 0
    # Stale on-disk review must not authorize mechanical/codegen after missing output.
    assert not any(
        e.get("type") == "task_attempt_succeeded" and e.get("node_id") == "codegen" for e in events
    )


def test_missing_manifest_fails_precommit_without_commit(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api",),
        applicable_layers=("api",),
        seed_stale_artifacts=True,
        mutations={"skill:aa-api-codegen": "missing_manifest"},
    )
    result = run_codegen_only(fixture)
    assert _status(fixture, result.invocation_id) in {"failed", "stopped"}
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="codegen") >= 1
    succeeded_codegen = [
        e for e in events if e.get("type") == "task_attempt_succeeded" and e.get("node_id") == "codegen"
    ]
    assert not succeeded_codegen


def test_manifest_write_set_mismatch_fails_precommit(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api",),
        applicable_layers=("api",),
        mutations={"skill:aa-api-codegen": "manifest_write_mismatch"},
    )
    result = run_codegen_only(fixture)
    assert _status(fixture, result.invocation_id) in {"failed", "stopped"}
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="codegen") >= 1
    assert not any(
        e.get("type") == "task_attempt_succeeded" and e.get("node_id") == "codegen" for e in events
    )


def test_byte_identical_rewrite_passes_with_fresh_authority(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api",),
        applicable_layers=("api",),
        seed_stale_artifacts=True,
    )
    result = run_codegen_only(fixture)
    _assert_applicable_cell(fixture, "api", result.invocation_id)


def test_forged_selected_role_import_rejected(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, selected_layers=("api",), applicable_layers=("api",))
    text = fixture.import_manifest_path.read_text(encoding="utf-8")
    forged = text.replace(
        "budgets: []\n",
        "- path: execute-workflow/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle\n"
        "  graph: api-plan-cycle\n"
        "  node: review\n"
        "  outputs: {}\n"
        "budgets: []\n",
    )
    fixture.import_manifest_path.write_text(forged, encoding="utf-8")
    with pytest.raises(Exception, match="(?i)import|predecessor|review|path|output|closure"):
        run_codegen_only(fixture)


def test_multi_layer_default_api_e2e(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api", "e2e"),
        applicable_layers=("api", "e2e"),
    )
    result = run_codegen_only(fixture)
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert_no_plan_or_run_tests(events)
    targets = {inv.target for inv in fixture.adapter.invocations}
    assert _CODEGEN["api"] in targets and _CODEGEN["e2e"] in targets
    assert _CODEGEN["fuzz"] not in targets and _CODEGEN["performance"] not in targets
    assert count_attempts(events, node_id="generation-join") == 1
    assert _status(fixture, result.invocation_id) == "completed"


def test_multi_layer_mixed_applicable(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api", "fuzz", "performance"),
        applicable_layers=("api",),
        inapplicable_layers=("fuzz", "performance"),
    )
    result = run_codegen_only(fixture)
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    targets = {inv.target for inv in fixture.adapter.invocations}
    assert _CODEGEN["api"] in targets
    assert _CODEGEN["fuzz"] not in targets
    assert _CODEGEN["performance"] not in targets
    assert _CODEGEN["e2e"] not in targets
    assert count_attempts(events, node_id="generation-join") == 1
    assert _status(fixture, result.invocation_id) == "completed"


def test_multi_layer_all_four_applicable(tmp_path: Path) -> None:
    fixture = make_fixture(
        tmp_path,
        selected_layers=LAYERS,
        applicable_layers=LAYERS,
    )
    result = run_codegen_only(fixture)
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    targets = {inv.target for inv in fixture.adapter.invocations}
    for layer in LAYERS:
        assert _CODEGEN[layer] in targets
    assert count_attempts(events, node_id="generation-join") == 1
    assert _status(fixture, result.invocation_id) == "completed"


def test_single_selected_layer_leaves_siblings_unselected(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, selected_layers=("fuzz",), applicable_layers=("fuzz",))
    result = run_codegen_only(fixture)
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    targets = {inv.target for inv in fixture.adapter.invocations}
    assert _CODEGEN["fuzz"] in targets
    for layer in ("api", "e2e", "performance"):
        assert _CODEGEN[layer] not in targets
        assert _REVIEWER[layer] not in targets
    structural = {e.get("structural_path") for e in events if e.get("type") == "task_attempt_started"}
    assert not any(isinstance(p, str) and "/api/" in p for p in structural)
    assert _status(fixture, result.invocation_id) == "completed"


def test_generation_join_waits_for_blocked_codegen(tmp_path: Path) -> None:
    from tests.helpers_four_layer_runtime import (
        FourLayerRuntimeFixture,
        build_four_layer_runtime,
        seed_four_layer_project,
    )

    adapter = FourLayerDeterministicAdapter()
    adapter.hold("skill:aa-e2e-codegen")
    project, change_dir, import_path = seed_four_layer_project(
        tmp_path,
        selected_layers=("api", "e2e"),
        applicable_layers=("api", "e2e"),
    )
    bundle = build_four_layer_runtime(project, adapter=adapter)
    fixture = FourLayerRuntimeFixture(
        project_root=project,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        import_manifest_path=import_path,
        selected_layers=("api", "e2e"),
        adapter=adapter,
        bundle=bundle,
        coordinator=CoordinatorFaultInjector(),
    )

    held: list[ImportResult] = []

    def _run() -> None:
        held.append(run_codegen_only(fixture))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    assert thread.is_alive(), "join should wait while last codegen is held"
    events_mid = root_events(fixture.change_dir)
    assert count_attempts(events_mid, node_id="generation-join") == 0
    assert "operation:run-tests" not in {
        e.get("target") for e in events_mid if e.get("type") == "task_attempt_started"
    }
    adapter.release("skill:aa-e2e-codegen")
    thread.join(timeout=60.0)
    assert not thread.is_alive()
    result = held[0]
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="generation-join") == 1
    assert _status(fixture, result.invocation_id) == "completed"


def test_generation_join_waits_for_barrier_on_inapplicable_gate(tmp_path: Path) -> None:
    barrier = BarrierNodeRunner(hold_node_id="review-gate")
    barrier.arm()
    fixture = make_fixture(
        tmp_path,
        selected_layers=("api", "e2e"),
        applicable_layers=("api",),
        inapplicable_layers=("e2e",),
        barrier=barrier,
    )
    held: list[ImportResult] = []

    def _run() -> None:
        held.append(run_codegen_only(fixture))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    assert thread.is_alive() or barrier.held_tasks, "barrier should observe inapplicable gate"
    events_mid = root_events(fixture.change_dir)
    assert count_attempts(events_mid, node_id="generation-join") == 0
    barrier.release()
    thread.join(timeout=60.0)
    assert not thread.is_alive()
    result = held[0]
    events = events_for_root(root_events(fixture.change_dir), result.invocation_id)
    assert count_attempts(events, node_id="generation-join") == 1
    assert _status(fixture, result.invocation_id) == "completed"
