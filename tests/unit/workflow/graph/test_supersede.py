"""Task 13: audited legacy-root supersede and single-use v6 replacement."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.graph_events import (
    GraphInvocationStartedEvent,
    GraphInvocationSupersededEvent,
)
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.compiler import PinnedDefinitionRequest, canonical_digest
from assurance_agent.workflow.graph.effect_retry import (
    EffectRetryStore,
    RootEffectFenceStore,
    RootTerminalFenceError,
)
from assurance_agent.workflow.graph.leases import LeaseRegistry, new_lease
from assurance_agent.workflow.graph.models import GraphProjection, TaskProjection
from assurance_agent.workflow.graph.resume_compatibility import (
    LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
    ResumeCompatibilityDecision,
)
from assurance_agent.workflow.graph.runtime_commit_safety import (
    commit_safety_semantics_bytes,
    commit_safety_semantics_digest,
)
from assurance_agent.workflow.graph.supersede import (
    SupersedeError,
    build_supersede_event,
    compute_replacement_authorization_id,
    compute_supersede_id,
    definition_request_digest,
    descendant_invocation_closure,
    evaluate_subtree_quiescence,
    evaluate_supersede_eligibility,
    fence_blocks_invocation,
    find_supersede_event,
    recover_prepared_fence,
    stage_definition_request_record,
    subtree_digest_for,
)
from assurance_agent.workflow.graph.status import supersede_audit_id

# Pinned after leaf-aware resume anchors joined the commit-safety semantics.
_PINNED_COMMIT_SAFETY_DIGEST = "1ba77a6e59880f97a05b2883b085d781bf139fca08cdc1f7ee39d06d9c003f3e"


def _projection(
    *,
    invocation_id: str = "root-1",
    entrypoint: str = "full",
    parent: str | None = None,
    terminal: str | None = None,
    event_schema_version: int = 5,
    event_seq: int = 10,
    params: dict[str, object] | None = None,
    tasks: dict[str, TaskProjection] | None = None,
) -> GraphProjection:
    return GraphProjection(
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        checkpoint_ns=invocation_id,
        parent_invocation_id=parent,
        parent_task_id=None,
        structural_path="main",
        graph_digest="graph-digest",
        event_schema_version=event_schema_version,
        ingest_catalog_digest="catalog",
        contract_digests={"skill:aa-api-codegen": "c1"},
        params=params or {"run_mode": "full", "test_types": ["api"]},
        root_tree_id="tree",
        current_tree_id="tree",
        event_seq=event_seq,
        tasks=tasks or {},
        terminal=terminal,  # type: ignore[arg-type]
    )


def _blocked_decision(root_id: str = "root-1") -> ResumeCompatibilityDecision:
    return ResumeCompatibilityDecision(
        schema_version="1",
        allowed=False,
        reason=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
        audit_triggered=True,
        requires_receipt=True,
        remaining_work_class="commit_safety_bearing",
        event_schema_version=5,
        root_invocation_id=root_id,
    )


def _v6_request() -> PinnedDefinitionRequest:
    return PinnedDefinitionRequest(
        graph_digest="g",
        ingest_catalog_digest="i",
        contract_digests=(("t", "c"),),
        event_schema_version=6,
        gate_semantics_digest="gate",
        assurance_profile_digest="profile",
        gate_semantics_object_id="gate-obj",
        topology_safety_semantics_object_id="topo-obj",
        topology_safety_semantics_digest="topo",
        commit_safety_semantics_object_id="commit-obj",
        commit_safety_semantics_digest="commit",
    )


def test_cli_shape_requires_change_invocation_action_who_reason() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["workflow", "supersede", "--help"])
    assert result.exit_code == 0
    assert "--change" in result.output
    assert "--invocation" in result.output
    assert "rerun-v6" in result.output
    assert "stop" in result.output
    assert "--who" in result.output
    assert "--reason" in result.output
    assert "--params" in result.output


def test_cli_stop_with_params_rejected() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "workflow",
            "supersede",
            "--change",
            "CH-X",
            "--invocation",
            "root-1",
            "--action",
            "stop",
            "--who",
            "op",
            "--reason",
            "done",
            "--params",
            "{}",
        ],
    )
    assert result.exit_code != 0
    assert "stop rejects --params" in result.output


def test_eligibility_rejects_child_non_latest_terminal_and_non_blocked(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    events: list[dict[str, object]] = []
    child = _projection(invocation_id="child-1", parent="root-1")
    elig = evaluate_supersede_eligibility(
        projection=child,
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="stop",
        who="op",
        reason="r",
        params_provided=False,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=events,
    )
    assert elig.eligible is False
    assert elig.reason == "not_root"

    root = _projection()
    elig = evaluate_supersede_eligibility(
        projection=root,
        latest_root_id="other-root",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="stop",
        who="op",
        reason="r",
        params_provided=False,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=events,
    )
    assert elig.reason == "not_latest_root"

    elig = evaluate_supersede_eligibility(
        projection=_projection(terminal="stopped"),
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="stop",
        who="op",
        reason="r",
        params_provided=False,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=events,
    )
    assert elig.reason == "already_terminal"

    allowed = ResumeCompatibilityDecision(
        schema_version="1",
        allowed=True,
        event_schema_version=5,
        root_invocation_id="root-1",
    )
    elig = evaluate_supersede_eligibility(
        projection=root,
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=allowed,
        action="stop",
        who="op",
        reason="r",
        params_provided=False,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=events,
    )
    assert elig.reason == "not_legacy_blocked"


def test_eligibility_stop_with_params_and_non_v6_request(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    root = _projection()
    elig = evaluate_supersede_eligibility(
        projection=root,
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="stop",
        who="op",
        reason="r",
        params_provided=True,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=[],
    )
    assert elig.reason == "stop_with_params"

    bad = PinnedDefinitionRequest(
        graph_digest="g",
        ingest_catalog_digest="i",
        contract_digests=(),
        event_schema_version=5,
        gate_semantics_digest="g",
        assurance_profile_digest="p",
    )
    elig = evaluate_supersede_eligibility(
        projection=root,
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="rerun-v6",
        who="op",
        reason="r",
        params_provided=False,
        staged_request=bad,
        project_root=tmp_path,
        change_dir=change,
        events=[],
    )
    assert elig.reason == "staged_request_not_v6"


def test_quiescence_rejects_live_lease_and_retry_sidecar(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "task_attempt_started",
            "invocation_id": "root-1",
            "task_id": "t1",
            "attempt_id": "a1",
            "seq": 2,
        }
    ]
    LeaseRegistry(change).upsert(
        new_lease(
            task_id="t1",
            attempt_id="a1",
            session_id=None,
            started_at=now.isoformat(),
            lease_expires_at=(now + timedelta(hours=1)).isoformat(),
        )
    )
    detail = evaluate_subtree_quiescence(
        project_root=tmp_path,
        change_dir=change,
        root_invocation_id="root-1",
        descendant_invocation_ids=(),
        events=events,
    )
    assert detail is not None and detail.startswith("live_lease:")

    # Clear leases; add retry sidecar.
    (change / "running-tasks.json").unlink(missing_ok=True)
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    store.schedule_next(
        fence_store=fence,
        root_invocation_id="root-1",
        invocation_id="root-1",
        task_id="t1",
        attempt_id="a1",
        effect_id="effect-1",
        kind="test_marker/v1",
        lock_key="effect:effect-1",
        error_code="retryable_io",
        now=now,
    )
    detail = evaluate_subtree_quiescence(
        project_root=tmp_path,
        change_dir=change,
        root_invocation_id="root-1",
        descendant_invocation_ids=(),
        events=[],
    )
    assert detail == "active_retry_sidecar:effect-1"


def test_quiescence_rejects_uncommitted_superstep_and_pending_writes(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "superstep_planned",
            "invocation_id": "root-1",
            "superstep_id": "s1",
            "checkpoint_id": "c1",
            "seq": 3,
        },
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "root-1",
            "task_id": "t1",
            "attempt_id": "a1",
            "write_set_id": "ws-1",
            "seq": 4,
        },
    ]
    detail = evaluate_subtree_quiescence(
        project_root=tmp_path,
        change_dir=change,
        root_invocation_id="root-1",
        descendant_invocation_ids=(),
        events=events,
    )
    assert detail is not None
    assert "pending_write_set" in detail or "uncommitted_superstep" in detail


def test_fence_prepare_commit_protocol_and_retry_race(tmp_path: Path) -> None:
    fence = RootEffectFenceStore(tmp_path)
    store = EffectRetryStore(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    supersede_id = "sid-1"
    fence.prepare_terminal("root-1", supersede_id=supersede_id, now=now)
    errors: list[BaseException] = []

    def schedule() -> None:
        try:
            store.schedule_next(
                fence_store=fence,
                root_invocation_id="root-1",
                invocation_id="root-1",
                task_id="t",
                attempt_id="a",
                effect_id="effect-race",
                kind="test_marker/v1",
                lock_key="effect:effect-race",
                error_code="retryable_io",
                now=now,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def commit() -> None:
        try:
            with fence.guard("root-1"):
                pass
            fence.commit_terminal("root-1", now=now + timedelta(seconds=1))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=schedule), threading.Thread(target=commit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = fence.load("root-1")
    assert state is not None and state.status == "committed"
    assert state.supersede_id == supersede_id
    sidecar = store.load("effect-race")
    assert sidecar is None or any(isinstance(exc, RootTerminalFenceError) for exc in errors)


def test_recover_prepared_fence_commits_when_event_exists_else_aborts(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    fence = RootEffectFenceStore(tmp_path)
    fence.prepare_terminal("root-1", supersede_id="s1")
    assert recover_prepared_fence(fence, root_invocation_id="root-1", events=[]) is None
    assert fence.load("root-1") is None

    fence.prepare_terminal("root-1", supersede_id="s1")
    descendants: tuple[str, ...] = ()
    digest = subtree_digest_for(
        root_invocation_id="root-1", descendant_invocation_ids=descendants, source_sequence=1
    )
    event = GraphInvocationSupersededEvent(
        type="graph_invocation_superseded",
        invocation_id="root-1",
        checkpoint_ns="root-1",
        entrypoint="full",
        supersede_id="s1",
        reason_code="legacy_commit_safety_semantics_unbound",
        who="op",
        reason="r",
        action="stop",
        descendant_invocation_ids=[],
        subtree_digest=digest,
        source_sequence=1,
        event_schema_version=5,
    )
    raw = {"source": "graph", "seq": 2, "ts": "2026-08-01T00:00:00Z", **event.model_dump(mode="json")}
    recovered = recover_prepared_fence(fence, root_invocation_id="root-1", events=[raw])
    assert recovered is not None
    assert fence.load("root-1") is not None
    assert fence.load("root-1").status == "committed"  # type: ignore[union-attr]


def test_terminal_fence_projects_stopped_and_audit_queryable() -> None:
    started = {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": "root-1",
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "g",
        "event_schema_version": 5,
        "ingest_catalog_digest": "c",
        "contract_digests": {},
        "policy_digest": "policy",
        "policy_origin": "packaged",
        "gate_semantics_digest": "gate",
        "assurance_profile_digest": "profile",
        "params": {"run_mode": "full"},
        "params_sha256": canonical_digest({"run_mode": "full"}),
        "root_tree_id": "t",
        "max_parallel_tasks": 1,
        "checkpoint_ns": "root-1",
        "parent_invocation_id": None,
        "parent_task_id": None,
        "structural_path": "main",
        "seq": 1,
        "ts": "2026-08-01T00:00:00Z",
    }
    child_started = {
        **started,
        "invocation_id": "child-1",
        "checkpoint_ns": "root-1/n/child-1",
        "parent_invocation_id": "root-1",
        "seq": 2,
    }
    descendants = ("child-1",)
    digest = subtree_digest_for(
        root_invocation_id="root-1", descendant_invocation_ids=descendants, source_sequence=2
    )
    sid = compute_supersede_id(
        root_invocation_id="root-1",
        entrypoint="full",
        action="stop",
        reason_code=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
        who="op",
        reason="block exit",
        params_sha256=None,
        definition_request_digest=None,
        descendant_invocation_ids=descendants,
        subtree_digest=digest,
        source_sequence=2,
    )
    supersede = GraphInvocationSupersededEvent(
        type="graph_invocation_superseded",
        invocation_id="root-1",
        checkpoint_ns="root-1",
        entrypoint="full",
        supersede_id=sid,
        reason_code="legacy_commit_safety_semantics_unbound",
        who="op",
        reason="block exit",
        action="stop",
        descendant_invocation_ids=list(descendants),
        subtree_digest=digest,
        source_sequence=2,
        event_schema_version=5,
    )
    events = [
        started,
        child_started,
        {"source": "graph", "seq": 3, "ts": "2026-08-01T00:00:01Z", **supersede.model_dump(mode="json")},
    ]
    root_proj = fold_invocation_events("root-1", events)
    child_proj = fold_invocation_events("child-1", events)
    assert root_proj.terminal == "stopped"
    assert root_proj.terminal_reason == "superseded"
    assert supersede_audit_id(root_proj) == sid
    assert child_proj.terminal == "stopped"
    assert child_proj.terminal_reason == "superseded"
    assert fence_blocks_invocation(events, "child-1") is not None


def test_exact_replay_idempotent_conflicting_payload_is_corruption() -> None:
    digest = subtree_digest_for(root_invocation_id="root-1", descendant_invocation_ids=(), source_sequence=1)
    event = GraphInvocationSupersededEvent(
        type="graph_invocation_superseded",
        invocation_id="root-1",
        checkpoint_ns="root-1",
        entrypoint="full",
        supersede_id="s1",
        reason_code="legacy_commit_safety_semantics_unbound",
        who="op",
        reason="r",
        action="stop",
        descendant_invocation_ids=[],
        subtree_digest=digest,
        source_sequence=1,
        event_schema_version=5,
    )
    raw = {"source": "graph", "seq": 1, "ts": "t", **event.model_dump(mode="json")}
    assert find_supersede_event([raw, raw], "root-1") is not None
    conflict = {**raw, "reason": "other"}
    with pytest.raises(SupersedeError, match="conflicting"):
        find_supersede_event([raw, conflict], "root-1")


def test_replacement_authorization_id_deterministic_and_stop_null(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    req = _v6_request()
    staged_digest = definition_request_digest(req)
    params: dict[str, object] = {"run_mode": "full"}
    params_sha = canonical_digest(params)
    elig = evaluate_supersede_eligibility(
        projection=_projection(event_seq=3),
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="rerun-v6",
        who="op",
        reason="rerun",
        params_provided=False,
        staged_request=req,
        project_root=tmp_path,
        change_dir=change,
        events=[],
    )
    assert elig.eligible is True
    from assurance_agent.workflow.graph.supersede import StagedReplacementPlan

    staged = StagedReplacementPlan(
        request=req,
        definition_request_digest=staged_digest,
        params=params,
        params_sha256=params_sha,
    )
    event = build_supersede_event(
        eligibility=elig,
        action="rerun-v6",
        who="op",
        reason="rerun",
        event_schema_version=5,
        checkpoint_ns="root-1",
        staged=staged,
    )
    assert event.replacement_authorization_id == compute_replacement_authorization_id(event.supersede_id)
    stop_elig = evaluate_supersede_eligibility(
        projection=_projection(event_seq=3),
        latest_root_id="root-1",
        expected_entrypoint="full",
        decision=_blocked_decision(),
        action="stop",
        who="op",
        reason="stop",
        params_provided=False,
        staged_request=None,
        project_root=tmp_path,
        change_dir=change,
        events=[],
    )
    stop_event = build_supersede_event(
        eligibility=stop_elig,
        action="stop",
        who="op",
        reason="stop",
        event_schema_version=5,
        checkpoint_ns="root-1",
        staged=None,
    )
    assert stop_event.replacement_authorization_id is None


def test_started_event_replacement_pair_all_or_none() -> None:
    base: dict[str, Any] = dict(
        type="graph_invocation_started",
        invocation_id="r",
        entrypoint="full",
        graph_id="main",
        graph_digest="g",
        event_schema_version=6,
        ingest_catalog_digest="c",
        contract_digests={},
        policy_digest="p",
        policy_origin="packaged",
        gate_semantics_digest="g",
        assurance_profile_digest="a",
        gate_semantics_object_id="go",
        topology_safety_semantics_object_id="to",
        topology_safety_semantics_digest="td",
        commit_safety_semantics_object_id="co",
        commit_safety_semantics_digest="cd",
        params={},
        params_sha256=canonical_digest({}),
        root_tree_id="t",
        max_parallel_tasks=1,
        checkpoint_ns="r",
        structural_path="main",
    )
    GraphInvocationStartedEvent(**base)
    with pytest.raises(Exception):
        GraphInvocationStartedEvent(**base, supersedes_invocation_id="old")
    GraphInvocationStartedEvent(
        **base,
        supersedes_invocation_id="old",
        replacement_authorization_id="auth",
    )


def test_commit_safety_bytes_unchanged_with_supersede_caller() -> None:
    assert commit_safety_semantics_digest() == _PINNED_COMMIT_SAFETY_DIGEST
    first = commit_safety_semantics_bytes()
    # Touch supersede module (caller of frozen fence API).
    assert descendant_invocation_closure([], "root") == ()
    assert commit_safety_semantics_bytes() == first
    assert commit_safety_semantics_digest() == _PINNED_COMMIT_SAFETY_DIGEST


def test_stage_definition_request_round_trip(tmp_path: Path) -> None:
    change = tmp_path / "change"
    change.mkdir()
    req = _v6_request()
    digest = definition_request_digest(req)
    stage_definition_request_record(change, digest=digest, request=req)
    from assurance_agent.workflow.graph.supersede import load_staged_definition_request

    loaded = load_staged_definition_request(change, digest)
    assert definition_request_digest(loaded) == digest


def test_descendant_closure_sorted() -> None:
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "c2",
            "parent_invocation_id": "root",
        },
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "c1",
            "parent_invocation_id": "root",
        },
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "g1",
            "parent_invocation_id": "c1",
        },
    ]
    assert descendant_invocation_closure(events, "root") == ("c1", "c2", "g1")
