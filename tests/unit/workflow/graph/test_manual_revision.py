"""Bounded revision views and exact manual-revision candidate capture."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from assurance_agent.workflow.core.graph_events import (
    GraphInterruptedEvent,
    ResumeAnchor,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph import manual_revision as manual_revision_mod
from assurance_agent.workflow.graph.manual_revision import (
    ManualRevisionError,
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
