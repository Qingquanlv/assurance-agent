"""Bounded revision views and exact manual-revision candidate capture."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from assurance_agent.workflow.core.graph_events import (
    GraphInterruptedEvent,
    ResumeAnchor,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph import manual_revision as manual_revision_mod
from assurance_agent.workflow.graph.manual_revision import (
    ManualRevisionError,
    RevisionPathBaseline,
    RevisionViewBinding,
    build_manual_revision_transition,
    capture_revision_candidate,
    derive_revision_recovery_state,
    materialize_revision_view,
    resolve_gate_evidence_epoch,
    stage_missing_resume_suffix,
    transition_from_committed_revision,
    validate_resume_prefix,
)
from assurance_agent.workflow.orchestration.gates import CHECKPOINT_GATE_ALIASES
from assurance_agent.workflow.graph.models import ResumeCommand
from assurance_agent.workflow.graph.workspace import (
    TreeFileRevision,
    TreePathRevision,
    TreeStore,
    WorkspaceError,
)


def _make_change(tmp_path: Path) -> tuple[Path, Path, TreeStore, str]:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    plans = change / "plans"
    plans.mkdir(parents=True)
    (plans / "fuzz-plan.md").write_text("# original\n", encoding="utf-8")
    (plans / "fuzz-codegen-plan.md").write_text("# codegen\n", encoding="utf-8")
    (change / "review").mkdir()
    (change / "review" / "fuzz-plan-review.json").write_text("{}\n", encoding="utf-8")
    (project / "app").mkdir(parents=True)
    (project / "app" / "source.py").write_text("app\n", encoding="utf-8")
    store = TreeStore(change)
    return project, change, store, store.capture(project)


_PATHS = ("change:plans/fuzz-codegen-plan.md", "change:plans/fuzz-plan.md")


def test_materialize_revision_view_writes_exact_allowlisted_files_only(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )

    assert binding.view_relpath == ".graph-runtime/revision-views/int-1"
    view = change / binding.view_relpath
    assert (view / "plans" / "fuzz-plan.md").read_bytes() == b"# original\n"
    assert (view / "plans" / "fuzz-codegen-plan.md").read_bytes() == b"# codegen\n"
    assert not (view / "review").exists()
    assert binding.logical_paths == _PATHS
    assert {item.logical_path: item.sha256 for item in binding.baseline} == {
        "change:plans/fuzz-codegen-plan.md": hashlib.sha256(b"# codegen\n").hexdigest(),
        "change:plans/fuzz-plan.md": hashlib.sha256(b"# original\n").hexdigest(),
    }


def test_orphan_view_is_recreated_from_base_tree(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    orphan = change / ".graph-runtime" / "revision-views" / "int-1" / "plans"
    orphan.mkdir(parents=True)
    (orphan / "fuzz-plan.md").write_text("# stale edit\n", encoding="utf-8")
    (orphan / "extra.md").write_text("extra\n", encoding="utf-8")

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )

    view = change / binding.view_relpath
    assert (view / "plans" / "fuzz-plan.md").read_bytes() == b"# original\n"
    assert not (view / "plans" / "extra.md").exists()


def test_committed_binding_preserves_user_edits(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    edited = change / binding.view_relpath / "plans" / "fuzz-plan.md"
    edited.write_text("# user edit\n", encoding="utf-8")

    rebound = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=binding,
    )

    assert rebound == binding
    assert edited.read_bytes() == b"# user edit\n"


def test_capture_rejects_missing_extra_symlink_and_non_file(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath

    (view / "plans" / "fuzz-plan.md").unlink()
    with pytest.raises((ManualRevisionError, WorkspaceError), match="missing|inventory|allowlist"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-2",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "extra.md").write_text("nope\n", encoding="utf-8")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="extra|inventory|allowlist"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-3",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "fuzz-plan.md").unlink()
    os.symlink("/tmp/outside-secret", view / "plans" / "fuzz-plan.md")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="symlink"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)

    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-4",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    (view / "plans" / "fuzz-plan.md").unlink()
    os.mkfifo(view / "plans" / "fuzz-plan.md")
    with pytest.raises((ManualRevisionError, WorkspaceError), match="non-file|regular file|inventory"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)


def test_capture_fails_closed_on_symlink_race_without_following_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-race",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    view = change / binding.view_relpath
    outside = tmp_path / "outside-secret"
    outside.write_text("SECRET\n", encoding="utf-8")
    target = view / "plans" / "fuzz-plan.md"
    outside_stat = outside.stat()
    opened_outside = False
    read_outside = False
    real_open = os.open
    real_read = os.read

    def _fd_is_outside(fd: int) -> bool:
        try:
            st = os.fstat(fd)
        except OSError:
            return False
        return st.st_dev == outside_stat.st_dev and st.st_ino == outside_stat.st_ino

    def guarded_open(path: str | bytes, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal opened_outside
        if dir_fd is None:
            fd = real_open(path, flags, mode)
        else:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if _fd_is_outside(fd):
            opened_outside = True
        return fd

    def guarded_read(fd: int, n: int, /) -> bytes:
        nonlocal read_outside
        if _fd_is_outside(fd):
            read_outside = True
        return real_read(fd, n)

    def _race(_view_root: Path) -> None:
        target.unlink()
        os.symlink(outside, target)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "read", guarded_read)
    monkeypatch.setattr(manual_revision_mod, "_after_revision_inventory_hook", _race)

    with pytest.raises((ManualRevisionError, WorkspaceError, OSError)):
        capture_revision_candidate(change_dir=change, store=store, binding=binding)
    assert opened_outside is False
    assert read_outside is False
    assert outside.read_text(encoding="utf-8") == "SECRET\n"


def test_capture_mixed_change_succeeds_and_noop_rejects(tmp_path: Path) -> None:
    _project, change, store, base_tree_id = _make_change(tmp_path)
    binding = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-1",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    (change / binding.view_relpath / "plans" / "fuzz-plan.md").write_text("# revised\n", encoding="utf-8")

    revision = capture_revision_candidate(change_dir=change, store=store, binding=binding)
    assert revision.target_tree_id != base_tree_id
    assert [path.logical_path for path in revision.paths] == list(_PATHS)
    assert revision.paths[0].before_sha256 == revision.paths[0].after_sha256
    assert revision.paths[1].before_sha256 != revision.paths[1].after_sha256
    assert store.read_bytes(revision.target_tree_id, "change:plans/fuzz-plan.md") == b"# revised\n"
    assert store.read_bytes(revision.target_tree_id, "change:review/fuzz-plan-review.json") == b"{}\n"

    binding_noop = materialize_revision_view(
        change_dir=change,
        store=store,
        interrupt_id="int-noop",
        owner_invocation_id="inv-leaf",
        base_tree_id=base_tree_id,
        logical_paths=_PATHS,
        committed_binding=None,
    )
    with pytest.raises((ManualRevisionError, WorkspaceError), match="manual_plan_revision_noop"):
        capture_revision_candidate(change_dir=change, store=store, binding=binding_noop)


def _interrupted_event() -> GraphInterruptedEvent:
    return GraphInterruptedEvent(
        type="graph_interrupted",
        invocation_id="leaf",
        checkpoint_ns="root/branch-node/branch/cycle-node/leaf",
        interrupt_id="ir-1",
        node_id="human-review",
        checkpoint="fuzz-plan-review-gate",
        actions=["fix_and_proceed", "accept_risk", "stop"],
        audited_reads_sha256={"review/fuzz-plan-review.json": "a" * 64},
        artifact_view=".graph-runtime/views/ir-1",
        revision_owner_invocation_id="leaf",
        revision_base_tree_id="tree-base",
        revision_view=".graph-runtime/revision-views/ir-1",
        revision_paths=["change:plans/fuzz-plan.md"],
        revision_before_sha256={"change:plans/fuzz-plan.md": "b" * 64},
        source_gate_attempt_id="ga-1",
        source_gate_tree_id="tree-src",
    )


def _resume_anchors() -> tuple[ResumeAnchor, ...]:
    return (
        ResumeAnchor(
            invocation_id="root",
            checkpoint_ns="root",
            node_id="branch-node",
            interrupt_id="ir-1",
        ),
        ResumeAnchor(
            invocation_id="branch",
            checkpoint_ns="root/branch-node/branch",
            node_id="cycle-node",
            interrupt_id="ir-1",
        ),
        ResumeAnchor(
            invocation_id="leaf",
            checkpoint_ns="root/branch-node/branch/cycle-node/leaf",
            node_id="human-review",
            interrupt_id="ir-1",
        ),
    )


def _tree_revision() -> TreeFileRevision:
    return TreeFileRevision(
        target_tree_id="tree-target",
        paths=(
            TreePathRevision(
                logical_path="change:plans/fuzz-plan.md",
                before_sha256="b" * 64,
                after_sha256="c" * 64,
            ),
        ),
    )


def _command() -> ResumeCommand:
    return ResumeCommand(
        interrupt_id="ir-1",
        action="fix_and_proceed",
        reason="revise plan",
        who="reviewer",
    )


def _pinned() -> dict[str, str]:
    return {
        "policy_digest": "pol",
        "gate_semantics_digest": "sem",
        "assurance_profile_digest": "prof",
        "graph_digest": "g",
    }


def test_build_manual_revision_transition_is_deterministic() -> None:
    first = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    second = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    assert first.revision.revision_transition_id == second.revision.revision_transition_id
    assert first.revision.revision_transition_id
    assert len(first.resumes) == 3
    assert [resume.revision_ordinal for resume in first.resumes] == [0, 1, 2]
    assert all(
        resume.revision_transition_id == first.revision.revision_transition_id for resume in first.resumes
    )
    assert first.resumes[0].parent_anchor_ref is None
    assert first.resumes[1].parent_anchor_ref is not None
    assert first == second


def test_validate_resume_prefix_accepts_all_legal_prefixes() -> None:
    transition = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    assert validate_resume_prefix([], transition) == 0
    for count in range(1, 4):
        prefix = [resume.model_dump(mode="json", exclude_none=True) for resume in transition.resumes[:count]]
        assert validate_resume_prefix(prefix, transition) == count


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda events: events[1:], id="gap"),
        pytest.param(lambda events: [events[1], events[0], events[2]], id="reorder"),
        pytest.param(lambda events: [events[0], events[0], events[1]], id="duplicate_ordinal"),
        pytest.param(
            lambda events: [{**events[0], "revision_transition_id": "other"}, *events[1:]],
            id="different_transition_id",
        ),
        pytest.param(
            lambda events: [
                {**events[0], "anchor": {**events[0]["anchor"], "node_id": "other"}},
                *events[1:],
            ],
            id="changed_anchor",
        ),
        pytest.param(
            lambda events: [{**events[1], "parent_anchor_ref": "wrong"}, events[0], events[2]],
            id="wrong_parent_anchor",
        ),
        pytest.param(
            lambda events: [{**event, "revision_chain_length": 2} for event in events],
            id="changed_chain_length",
        ),
    ],
)
def test_validate_resume_prefix_rejects_conflicts(mutate: object) -> None:
    transition = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    events = [resume.model_dump(mode="json", exclude_none=True) for resume in transition.resumes]
    mutated = mutate(events)  # type: ignore[operator]
    with pytest.raises(ManualRevisionError, match="manual_plan_revision_prefix_conflict"):
        validate_resume_prefix(mutated, transition)


def test_stage_missing_resume_suffix_appends_only_absent_events(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    transition = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    with transaction(change) as txn:
        txn.append_strict(transition.revision)
        txn.append_strict(transition.resumes[0])
    with transaction(change) as txn:
        missing = stage_missing_resume_suffix(txn=txn, transition=transition)
        assert missing == 1
    rebuilt = transition_from_committed_revision(transition.revision)
    with transaction(change) as txn:
        assert stage_missing_resume_suffix(txn=txn, transition=rebuilt) == 3
    events = [
        event
        for event in change.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines()
        if '"graph_resumed"' in event
    ]
    assert len(events) == 3


def test_resolve_gate_evidence_epoch_direct_and_aliased_checkpoints() -> None:
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "leaf",
            "entrypoint": "full",
            "graph_id": "cycle",
            "graph_digest": "g",
            "contract_digests": {},
            "params": {},
            "params_sha256": "",
            "root_tree_id": "tree-src",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "leaf",
            "structural_path": "cycle",
        },
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": "fuzz-plan-review-gate", "verdict": "needs_human_review"},
        },
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-1",
            "task_id": "safety-task",
            "attempt_id": "ga-safety",
            "gate_report": {"gate_id": "fixer-safety-gate", "verdict": "needs_human_review"},
        },
    ]
    gate_ids = frozenset({"fuzz-plan-review-gate", "fixer-safety-gate"})
    direct = resolve_gate_evidence_epoch(
        events=events,
        invocation_id="leaf",
        checkpoint="fuzz-plan-review-gate",
        gate_ids=gate_ids,
        checkpoint_gate_aliases=CHECKPOINT_GATE_ALIASES,
    )
    assert direct is not None
    assert direct.source_gate_attempt_id == "ga-1"
    assert direct.source_gate_tree_id == "tree-src"

    aliased = resolve_gate_evidence_epoch(
        events=events,
        invocation_id="leaf",
        checkpoint="healing.safety",
        gate_ids=gate_ids,
        checkpoint_gate_aliases=CHECKPOINT_GATE_ALIASES,
    )
    assert aliased is not None
    assert aliased.source_gate_attempt_id == "ga-safety"
    assert aliased.source_gate_tree_id == "tree-src"

    pairless = resolve_gate_evidence_epoch(
        events=events,
        invocation_id="leaf",
        checkpoint="improvement-review",
        gate_ids=gate_ids,
        checkpoint_gate_aliases=CHECKPOINT_GATE_ALIASES,
    )
    assert pairless is None


def test_resolve_gate_evidence_epoch_binds_attempt_time_tree_not_later_tree() -> None:
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "leaf",
            "entrypoint": "full",
            "graph_id": "cycle",
            "graph_digest": "g",
            "contract_digests": {},
            "params": {},
            "params_sha256": "",
            "root_tree_id": "tree-at-attempt",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "leaf",
            "structural_path": "cycle",
        },
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": "fuzz-plan-review-gate", "verdict": "needs_human_review"},
        },
        {
            "source": "graph",
            "type": "superstep_committed",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-2",
            "base_tree_id": "tree-at-attempt",
            "target_tree_id": "tree-after-superstep",
            "task_ids": [],
        },
        {
            "source": "graph",
            "type": "manual_plan_revision",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "revision_transition_id": "rt-later",
            "interrupt_id": "ir-other",
            "action": "fix_and_proceed",
            "who": "reviewer",
            "reason": "later revision",
            "audited_reads_sha256": {},
            "source_gate_attempt_id": "ga-other",
            "source_gate_tree_id": "tree-at-attempt",
            "base_tree_id": "tree-after-superstep",
            "target_tree_id": "tree-after-revision",
            "logical_paths": ["change:plans/fuzz-plan.md"],
            "before_sha256": {"change:plans/fuzz-plan.md": "a" * 64},
            "after_sha256": {"change:plans/fuzz-plan.md": "b" * 64},
            "resume_anchors": [],
        },
    ]
    epoch = resolve_gate_evidence_epoch(
        events=events,
        invocation_id="leaf",
        checkpoint="fuzz-plan-review-gate",
        gate_ids=frozenset({"fuzz-plan-review-gate"}),
        checkpoint_gate_aliases=CHECKPOINT_GATE_ALIASES,
    )
    assert epoch is not None
    assert epoch.source_gate_attempt_id == "ga-1"
    assert epoch.source_gate_tree_id == "tree-at-attempt"


def test_open_revision_prefix_derives_recovery_state() -> None:
    transition = build_manual_revision_transition(
        interrupted=_interrupted_event(),
        command=_command(),
        revision=_tree_revision(),
        pinned_definition_digests=_pinned(),
        resume_anchors=_resume_anchors(),
    )
    events = [
        transition.revision.model_dump(mode="json", exclude_none=True),
        transition.resumes[0].model_dump(mode="json", exclude_none=True),
    ]
    assert derive_revision_recovery_state(events) == "revision_resume_recovery_pending"
    complete = [
        transition.revision.model_dump(mode="json", exclude_none=True),
        *[resume.model_dump(mode="json", exclude_none=True) for resume in transition.resumes],
    ]
    assert derive_revision_recovery_state(complete) is None


# ---------------------------------------------------------------------------
# Step 2: runtime fix_and_proceed / accept_risk / stop ingestion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ManualRevisionRuntimeFixture:
    runtime: object
    result: object
    interrupt: object
    change: Path
    revision_view: str


def _force_v5_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_agent.workflow.graph import definition_pinning, runtime as runtime_mod

    original = definition_pinning.bind_root_definitions

    def _bind_v5(*, store, root_tree_id, event_schema_version=4):  # type: ignore[no-untyped-def]
        return original(store=store, root_tree_id=root_tree_id, event_schema_version=5)

    monkeypatch.setattr(definition_pinning, "bind_root_definitions", _bind_v5)
    monkeypatch.setattr(runtime_mod, "bind_root_definitions", _bind_v5)


def _manual_revision_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _ManualRevisionRuntimeFixture:
    """Build a v5 runtime interrupted on a manual_revision human-review node."""
    import json
    import textwrap

    from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
    from assurance_agent.workflow.graph.checkpoint import CheckpointStore
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.driver.operations_catalog import default_operations
    from assurance_agent.workflow.graph.handlers.operation import OperationHandler
    from assurance_agent.workflow.graph.leases import SystemClock
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import build_default_node_runner
    from assurance_agent.workflow.graph.workspace import WorkspaceBackend
    from tests.helpers_aa import write_aa_config
    from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
    from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime

    _force_v5_binding(monkeypatch)

    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(project)

    contracts_text = """\
schema_version: "1"
contracts:
  operation:seed-plan:
    handler: operation
    side_effect_free: false
    writes: ["change:plans/**", "change:review/**"]
    authorization_writes: ["change:plans/**", "change:review/**"]
    retryable_errors: []
  builtin:gate:
    handler: builtin
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
"""
    body = """
main:
  max_supersteps: 8
  nodes:
    seed:
      uses: operation:seed-plan
    review:
      uses: builtin:gate
      with: {gate: plan-gate}
    human-review:
      uses: builtin:interrupt
      interrupt:
        reason: plan needs human revision
        checkpoint: plan-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
        manual_revision:
          action: fix_and_proceed
          paths: [change:plans/fuzz-plan.md]
  edges:
    - {from: START, to: seed}
    - {from: seed, to: review}
  routes:
    - from: review
      select: "node('review').gate.verdict"
      cases:
        needs_human_review: human-review
        pass: END
      default: STOP
    - from: human-review
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""
    footer = """
gates:
  plan-gate:
    reads: [review/plan-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_human_review_when: "plan_review.decision == 'needs_human_review'"
    pass_when: "plan_review.decision == 'pass'"
"""
    text = (
        'schema_version: "2"\nname: t\n'
        "params:\n  run_mode: {type: enum, values: [full], default: full}\n"
        "entrypoints:\n  full: {graph: main, allow: \"params.run_mode == 'full'\"}\n"
        "policies:\n"
        "  retry:\n    never: {max_attempts: 1, retry_on: []}\n"
        "  timeout:\n    local: {run_seconds: 60, heartbeat_seconds: 0.05}\n"
        "  scheduler: {max_parallel_tasks: 2}\n"
        "graphs:\n" + textwrap.indent(textwrap.dedent(body), "  ") + footer
    )
    contracts = parse_execution_contracts(contracts_text)
    compiled = compile_workflow(parse_workflow_v2(text), contracts)

    def seed_plan(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        plans = workspace.change_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "fuzz-plan.md").write_text("# original plan\n", encoding="utf-8")
        review = workspace.change_dir / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "plan-review.json").write_text(
            json.dumps({"decision": "needs_human_review"}),
            encoding="utf-8",
        )
        return TaskResult(status="succeeded")

    ops = default_operations()
    ops["operation:seed-plan"] = seed_plan

    class NeverInvoker:
        def invoke(self, request: AgentRequest) -> AgentResult:
            raise AssertionError(f"unexpected agent invoke: {request}")

    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    base = build_default_node_runner(
        NeverInvoker(),
        store,
        contracts,
        compiled=compiled,
        operations=ops,
        run_child=run_child,
    )
    op_handler = OperationHandler(ops)

    class Combined:
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            if task.target.startswith("operation:"):
                return op_handler.execute(task, workspace, context)
            return base.execute(task, workspace, context)

    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=SystemClock(),
        workspace_backend=workspaces,
        node_runner=Combined(),
        max_parallel_tasks=2,
        contracts=contracts,
        state_defs={},
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=Combined(),
            scheduler=scheduler,
        ),
        clock=SystemClock(),
    )
    holder["rt"] = runtime
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )
    result = runtime.run(compiled, "full", context)
    assert result.exit_code == 30, result.reason
    assert result.status.status == "interrupted"
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.revision_view is not None
    assert interrupt.source_gate_attempt_id is not None
    return _ManualRevisionRuntimeFixture(
        runtime=runtime,
        result=result,
        interrupt=interrupt,
        change=change,
        revision_view=interrupt.revision_view,
    )


def test_runtime_fix_and_proceed_noop_leaves_interrupt_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime, GraphRuntimeError

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    with pytest.raises(GraphRuntimeError, match="manual_plan_revision_noop"):
        runtime.resume(
            result.invocation_id,
            ResumeCommand(
                interrupt_id=interrupt.interrupt_id,
                action="fix_and_proceed",
                reason="no edits",
                who="reviewer",
            ),
        )
    status = runtime.status(result.invocation_id)
    assert status.status == "interrupted"
    assert status.pending_interrupts
    events = (fx.change / "events.jsonl").read_text(encoding="utf-8")
    assert "manual_plan_revision" not in events


def test_runtime_invalid_revision_inventory_raises_graph_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime, GraphRuntimeError

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    view = fx.change / fx.revision_view
    (view / "plans" / "fuzz-plan.md").write_text("# revised plan\n", encoding="utf-8")
    (view / "plans" / "extra.md").write_text("undeclared\n", encoding="utf-8")
    with pytest.raises(GraphRuntimeError, match="extra path|inventory"):
        runtime.resume(
            result.invocation_id,
            ResumeCommand(
                interrupt_id=interrupt.interrupt_id,
                action="fix_and_proceed",
                reason="revise plan",
                who="reviewer",
            ),
        )
    assert runtime.status(result.invocation_id).status == "interrupted"


def test_runtime_fix_and_proceed_captures_edited_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    view = fx.change / fx.revision_view
    (view / "plans" / "fuzz-plan.md").write_text("# revised plan\n", encoding="utf-8")

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="fix_and_proceed",
            reason="revise plan",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0, done.reason
    assert done.status.status == "completed"
    events = read_events_strict(fx.change)
    revisions = [e for e in events if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    after = revisions[0].get("after_sha256")
    assert isinstance(after, dict)
    assert after["change:plans/fuzz-plan.md"] == hashlib.sha256(b"# revised plan\n").hexdigest()
    resumes = [e for e in events if e.get("type") == "graph_resumed"]
    assert resumes
    assert all(e.get("revision_transition_id") == revisions[0].get("revision_transition_id") for e in resumes)


def test_runtime_accept_risk_and_stop_ignore_revision_view_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    view = fx.change / fx.revision_view
    (view / "plans" / "fuzz-plan.md").write_text("# should be ignored\n", encoding="utf-8")

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept as-is",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0, done.reason
    events = read_events_strict(fx.change)
    assert not any(e.get("type") == "manual_plan_revision" for e in events)
    resumed = [e for e in events if e.get("type") == "graph_resumed"]
    assert resumed and resumed[0]["action"] == "accept_risk"

    # Fresh interrupted run for stop
    fx2 = _manual_revision_runtime(tmp_path / "stop", monkeypatch)
    runtime2 = cast(GraphRuntime, fx2.runtime)
    result2 = cast(RunResult, fx2.result)
    interrupt2 = cast(InterruptProjection, fx2.interrupt)
    view2 = fx2.change / fx2.revision_view
    (view2 / "plans" / "fuzz-plan.md").write_text("# ignored on stop\n", encoding="utf-8")
    stopped = runtime2.resume(
        result2.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt2.interrupt_id,
            action="stop",
            reason="abort",
            who="reviewer",
        ),
    )
    assert stopped.exit_code == 20
    events2 = read_events_strict(fx2.change)
    assert not any(e.get("type") == "manual_plan_revision" for e in events2)


def test_runtime_identical_suffix_repair_after_open_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.core.graph_events import GraphInterruptedEvent, ManualPlanRevisionEvent
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime, _resume_anchors_for

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    change = fx.change
    view = change / fx.revision_view
    (view / "plans" / "fuzz-plan.md").write_text("# revised once\n", encoding="utf-8")

    projection = runtime._checkpoints.project(result.invocation_id)  # noqa: SLF001
    pending = projection.interrupts[interrupt.interrupt_id]
    binding = RevisionViewBinding(
        interrupt_id=pending.interrupt_id,
        owner_invocation_id=pending.revision_owner_invocation_id or "",
        base_tree_id=pending.revision_base_tree_id or "",
        view_relpath=pending.revision_view or "",
        logical_paths=tuple(pending.revision_paths or ()),
        baseline=tuple(
            RevisionPathBaseline(logical_path=path, sha256=(pending.revision_before_sha256 or {})[path])
            for path in (pending.revision_paths or ())
        ),
    )
    tree_revision = capture_revision_candidate(
        change_dir=change,
        store=runtime._objects,  # noqa: SLF001
        binding=binding,
    )
    events = read_events_strict(change)
    interrupted = next(
        e
        for e in events
        if e.get("type") == "graph_interrupted" and e.get("interrupt_id") == interrupt.interrupt_id
    )
    owner = runtime._checkpoints.project(pending.revision_owner_invocation_id or "")  # noqa: SLF001
    transition = build_manual_revision_transition(
        interrupted=GraphInterruptedEvent.model_validate(
            {k: v for k, v in interrupted.items() if k not in {"seq", "ts", "source"}}
        ),
        command=ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="fix_and_proceed",
            reason="revise plan",
            who="reviewer",
        ),
        revision=tree_revision,
        pinned_definition_digests={
            "policy_digest": owner.policy_digest,
            "gate_semantics_digest": owner.gate_semantics_digest,
            "assurance_profile_digest": owner.assurance_profile_digest,
            "graph_digest": owner.graph_digest,
            "ir_digest": owner.ir_digest,
        },
        resume_anchors=_resume_anchors_for(pending),
    )
    # Crash after revision commit: only the revision event is durable.
    with transaction(change) as txn:
        txn.append_strict(transition.revision)

    assert derive_revision_recovery_state(read_events_strict(change)) == ("revision_resume_recovery_pending")
    # Identical CLI retry must repair the missing resume suffix (or no-op after recovery).
    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="fix_and_proceed",
            reason="revise plan",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0, done.reason
    events_after = read_events_strict(change)
    revisions = [e for e in events_after if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    rebuilt = transition_from_committed_revision(
        ManualPlanRevisionEvent.model_validate(
            {k: v for k, v in revisions[0].items() if k not in {"seq", "ts", "source"}}
        )
    )
    resumes = [
        e
        for e in events_after
        if e.get("type") == "graph_resumed"
        and e.get("revision_transition_id") == rebuilt.revision.revision_transition_id
    ]
    assert len(resumes) == len(rebuilt.resumes)


def test_runtime_non_identical_retry_after_commit_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.core.graph_events import GraphInterruptedEvent
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.graph.models import InterruptProjection, RunResult
    from assurance_agent.workflow.graph.runtime import GraphIntegrityError, GraphRuntime, _resume_anchors_for

    fx = _manual_revision_runtime(tmp_path, monkeypatch)
    runtime = cast(GraphRuntime, fx.runtime)
    result = cast(RunResult, fx.result)
    interrupt = cast(InterruptProjection, fx.interrupt)
    change = fx.change
    view = change / fx.revision_view
    (view / "plans" / "fuzz-plan.md").write_text("# first revision\n", encoding="utf-8")

    projection = runtime._checkpoints.project(result.invocation_id)  # noqa: SLF001
    pending = projection.interrupts[interrupt.interrupt_id]
    binding = RevisionViewBinding(
        interrupt_id=pending.interrupt_id,
        owner_invocation_id=pending.revision_owner_invocation_id or "",
        base_tree_id=pending.revision_base_tree_id or "",
        view_relpath=pending.revision_view or "",
        logical_paths=tuple(pending.revision_paths or ()),
        baseline=tuple(
            RevisionPathBaseline(logical_path=path, sha256=(pending.revision_before_sha256 or {})[path])
            for path in (pending.revision_paths or ())
        ),
    )
    tree_revision = capture_revision_candidate(
        change_dir=change,
        store=runtime._objects,  # noqa: SLF001
        binding=binding,
    )
    events = read_events_strict(change)
    interrupted = next(
        e
        for e in events
        if e.get("type") == "graph_interrupted" and e.get("interrupt_id") == interrupt.interrupt_id
    )
    owner = runtime._checkpoints.project(pending.revision_owner_invocation_id or "")  # noqa: SLF001
    transition = build_manual_revision_transition(
        interrupted=GraphInterruptedEvent.model_validate(
            {k: v for k, v in interrupted.items() if k not in {"seq", "ts", "source"}}
        ),
        command=ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="fix_and_proceed",
            reason="revise plan",
            who="reviewer",
        ),
        revision=tree_revision,
        pinned_definition_digests={
            "policy_digest": owner.policy_digest,
            "gate_semantics_digest": owner.gate_semantics_digest,
            "assurance_profile_digest": owner.assurance_profile_digest,
            "graph_digest": owner.graph_digest,
            "ir_digest": owner.ir_digest,
        },
        resume_anchors=_resume_anchors_for(pending),
    )
    with transaction(change) as txn:
        txn.append_strict(transition.revision)

    # Non-identical view content on retry must conflict while interrupt is still pending.
    (view / "plans" / "fuzz-plan.md").write_text("# different second try\n", encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="non-identical retry after commit"):
        runtime.resume(
            result.invocation_id,
            ResumeCommand(
                interrupt_id=interrupt.interrupt_id,
                action="fix_and_proceed",
                reason="revise plan",
                who="reviewer",
            ),
        )
