"""D13 attempt-bound input snapshots, sidecars, deferrals, and consumer guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.agent_api import AgentRequest, build_node_prompt
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, fold_invocation_events
from assurance_agent.workflow.graph.contracts import ExecutionContract, ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.models import ExecutableTask, PlanResult, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import BackoffDef, RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.graph.task_inputs import (
    PlanFixerRuntimeContextV1,
    TaskInputError,
    TaskInputSnapshotEntryV1,
    TaskInputSnapshotV1,
    assert_prompt_runtime_context_match,
    capture_task_input_snapshot,
    load_task_input_snapshot,
    runtime_context_digest,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend

DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)


def _entry(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "physical_relpath": "qa/changes/CH-1/plans/api-plan.md",
        "repo_relpath": None,
        "logical_aliases": ["change:plans/api-plan.md", "project:qa/changes/CH-1/plans/api-plan.md"],
        "matched_claims": ["change:plans/api-plan.md"],
        "origins": ["contract_read"],
        "kind": "file",
        "mode": 0o100644,
        "sha256": DIGEST_A,
        "symlink_target": None,
    }
    payload.update(overrides)
    return payload


def _snapshot(**overrides: object) -> dict[str, object]:
    entries = overrides.pop("entries", [_entry()])
    assert isinstance(entries, list)
    typed = [TaskInputSnapshotEntryV1.model_validate(item) for item in entries]
    payload: dict[str, object] = {
        "schema_version": "1",
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "attempt_id": "task-1-a1",
        "base_tree_id": "tree-base",
        "materialized_tree_id": "tree-mat",
        "input_sha256": sha256_bytes(canonical_json_bytes([e.model_dump(mode="json") for e in typed])),
        "runtime_context_sha256": None,
        "contract_digest": DIGEST_B,
        "claims_digest": DIGEST_A,
        "entries": [e.model_dump(mode="json") for e in typed],
    }
    payload.update(overrides)
    return payload


def _runtime_context(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "mode": "automatic_healing",
        "target": "api",
        "change_id": "CH-1",
        "root_invocation_id": "root-1",
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "attempt_id": "task-1-a1",
        "base_tree_id": "tree-base",
        "source_review_path": "change:review/api-plan-review.json",
        "source_review_sha256": DIGEST_A,
        "source_interrupt_task_id": None,
        "resume_action": None,
        "human_reason": None,
        "human_decision_sha256": None,
    }
    payload.update(overrides)
    return payload


def test_snapshot_accepts_canonical_unique_physical_entries() -> None:
    snapshot = TaskInputSnapshotV1.model_validate(_snapshot())
    assert snapshot.entries[0].physical_relpath.endswith("api-plan.md")
    assert canonical_json_bytes(snapshot) == canonical_json_bytes(snapshot)


def test_snapshot_rejects_duplicate_or_unsorted_physical_paths() -> None:
    first = _entry(physical_relpath="b.md", logical_aliases=["change:b.md"])
    second = _entry(physical_relpath="a.md", logical_aliases=["change:a.md"])
    with pytest.raises(ValidationError):
        TaskInputSnapshotV1.model_validate(_snapshot(entries=[first, second]))
    with pytest.raises(ValidationError):
        TaskInputSnapshotV1.model_validate(_snapshot(entries=[first, first]))


def test_snapshot_file_and_symlink_field_rules() -> None:
    with pytest.raises(ValidationError):
        TaskInputSnapshotEntryV1.model_validate(_entry(kind="file", sha256=None))
    with pytest.raises(ValidationError):
        TaskInputSnapshotEntryV1.model_validate(_entry(kind="symlink", sha256=None, symlink_target=None))
    with pytest.raises(ValidationError):
        TaskInputSnapshotEntryV1.model_validate(_entry(kind="symlink", sha256=DIGEST_A, symlink_target="x"))


def test_snapshot_rejects_wrong_input_digest() -> None:
    payload = _snapshot()
    payload["input_sha256"] = DIGEST_B
    with pytest.raises(ValidationError):
        TaskInputSnapshotV1.model_validate(payload)


def test_snapshot_rejects_empty_matched_claims() -> None:
    with pytest.raises(ValidationError):
        TaskInputSnapshotEntryV1.model_validate(_entry(matched_claims=[]))


def test_automatic_healing_requires_null_human_fields() -> None:
    PlanFixerRuntimeContextV1.model_validate(_runtime_context())
    with pytest.raises(ValidationError):
        PlanFixerRuntimeContextV1.model_validate(_runtime_context(human_reason="nope"))


def test_human_approved_requires_interrupt_action_reason_digest() -> None:
    payload = _runtime_context(
        mode="human_approved",
        source_interrupt_task_id="interrupt-1",
        resume_action="fix_and_proceed",
        human_reason="approved",
        human_decision_sha256=DIGEST_B,
    )
    PlanFixerRuntimeContextV1.model_validate(payload)
    with pytest.raises(ValidationError):
        PlanFixerRuntimeContextV1.model_validate({**payload, "human_reason": ""})
    with pytest.raises(ValidationError):
        PlanFixerRuntimeContextV1.model_validate({**payload, "resume_action": None})


def test_runtime_context_rejects_wrong_target_literal() -> None:
    with pytest.raises(ValidationError):
        PlanFixerRuntimeContextV1.model_validate(_runtime_context(target="fuzz"))


def _seed_declared_project(tmp_path: Path) -> tuple[Path, Path, str, ResourceClaims]:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    plan = change / "plans" / "api-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# api plan\n", encoding="utf-8")
    skill = project / "skills" / "aa-api-plan" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("plan skill\n", encoding="utf-8")
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").write_text("#!/fake\n", encoding="utf-8")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    (project / "node_modules" / "pkg" / "index.js").write_text("module.exports={}\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    store = TreeStore(change)
    tree_id = store.capture(project, repo_root=project)
    # Host coordinator ledger stays outside capture and must not poison the
    # strict events.jsonl seq stream used by scheduler tests.
    (change / "events.jsonl").write_text('{"type":"coordinator"}\n', encoding="utf-8")

    claims = ResourceClaims(
        reads=(
            ResourcePath.parse("change:plans/api-plan.md"),
            ResourcePath.parse("repo:tests/api/**"),
        ),
        writes=(ResourcePath.parse("change:plans/api-plan.md"),),
        authorization_writes=(ResourcePath.parse("change:plans/api-plan.md"),),
    )
    return project, change, tree_id, claims


def test_declared_only_sidecar_hides_control_and_host_paths(tmp_path: Path) -> None:
    project, change, tree_id, claims = _seed_declared_project(tmp_path)
    host_targets = {
        ".git/HEAD": project / ".git" / "HEAD",
        ".venv/bin/python": project / ".venv" / "bin" / "python",
        "node_modules/pkg/index.js": project / "node_modules" / "pkg" / "index.js",
        "events.jsonl": change / "events.jsonl",
    }
    host_snapshots = {
        key: (path.read_bytes(), path.lstat().st_mode, path.is_symlink())
        for key, path in host_targets.items()
    }
    backend = WorkspaceBackend(change)
    sidecar = backend.sidecar_root_for("plan-api")
    workspace = backend.create(
        task_id="plan-api",
        base_tree_id=tree_id,
        store=TreeStore(change),
        sidecar_root=sidecar,
        claims=claims,
        declared_reads_only=True,
        skill_name="aa-api-plan",
        initialize_git=False,
    )

    assert workspace.tree_manifest_path is not None
    assert workspace.tree_manifest_path.is_file()
    assert not (workspace.root / ".graph-runtime" / "tree.json").exists()
    assert not (workspace.project_root / ".git").exists()
    for rel in (".venv", "node_modules", "qa/changes/CH-1/events.jsonl"):
        target = workspace.project_root / rel
        assert not target.exists()
        assert not target.is_symlink()
        with pytest.raises(OSError):
            target.read_bytes()
        try:
            target.write_text("probe\n", encoding="utf-8")
            written = True
        except OSError:
            written = False
        if written:
            # Agent may create a new local file, but it must not be a host link.
            assert not target.is_symlink()
            target.unlink()

    for key, path in host_targets.items():
        data, mode, is_link = host_snapshots[key]
        assert path.read_bytes() == data
        assert path.lstat().st_mode == mode
        assert path.is_symlink() is is_link

    from assurance_agent.workflow.graph.workspace import TaskWorkspace

    reopened = TaskWorkspace.from_materialized_root(
        "plan-api",
        workspace.root,
        tree_id,
        materialized_tree_id=workspace.materialized_tree_id,
        tree_manifest_path=workspace.tree_manifest_path,
        sidecar_root=sidecar,
    )
    assert reopened.project_root == workspace.project_root
    assert reopened.change_dir == workspace.change_dir


def test_aliased_roots_record_one_physical_entry_with_both_aliases(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    test_file = project / "tests" / "api" / "test_login.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_login():\n    assert True\n", encoding="utf-8")
    skill = project / "skills" / "aa-api-codegen" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("codegen\n", encoding="utf-8")
    store = TreeStore(change)
    tree_id = store.capture(project, repo_root=project)
    claims = ResourceClaims(
        reads=(
            ResourcePath.parse("project:tests/api/test_login.py"),
            ResourcePath.parse("repo:tests/api/test_login.py"),
        ),
        writes=(ResourcePath.parse("repo:tests/api/test_login.py"),),
        authorization_writes=(ResourcePath.parse("repo:tests/api/test_login.py"),),
    )
    backend = WorkspaceBackend(change)
    workspace = backend.create(
        task_id="codegen",
        base_tree_id=tree_id,
        store=store,
        sidecar_root=backend.sidecar_root_for("codegen"),
        claims=claims,
        declared_reads_only=True,
        skill_name="aa-api-codegen",
        initialize_git=False,
    )
    task = _executable_task(
        task_id="codegen",
        target="skill:aa-api-codegen",
        claims=claims,
        contract_digest="c" * 64,
    )
    contract = ExecutionContract(
        target="skill:aa-api-codegen",
        handler="agent",
        reads=("project:tests/api/test_login.py", "repo:tests/api/test_login.py"),
        writes=("repo:tests/api/test_login.py",),
        authorization_writes=("repo:tests/api/test_login.py",),
        read_isolation="declared_only",
    )
    snapshot_id, raw = capture_task_input_snapshot(
        invocation_id="inv-1",
        task=task,
        attempt_id="codegen-a1",
        workspace=workspace,
        contract=contract,
        runtime_context=None,
    )
    store_task_input_snapshot(store, snapshot_id, raw)
    snapshot = load_task_input_snapshot(store, snapshot_id)
    matches = [entry for entry in snapshot.entries if entry.physical_relpath.endswith("test_login.py")]
    assert len(matches) == 1
    assert "project:tests/api/test_login.py" in matches[0].logical_aliases
    assert "repo:tests/api/test_login.py" in matches[0].logical_aliases
    assert matches[0].repo_relpath == "tests/api/test_login.py"


def _executable_task(
    *,
    task_id: str,
    target: str,
    claims: ResourceClaims,
    contract_digest: str,
    retry_on: list[ErrorKind] | None = None,
) -> ExecutableTask:
    kinds: tuple[ErrorKind, ...] = tuple(retry_on or ())
    return ExecutableTask(
        task_id=task_id,
        invocation_id="inv-1",
        checkpoint_ns="root",
        graph_id="g",
        node_id=task_id,
        structural_path=f"/g/{task_id}",
        input={},
        input_sha256="d" * 64,
        contract_digest=contract_digest,
        retryable_errors=kinds,
        retry_policy=RetryPolicyDef(
            max_attempts=3,
            retry_on=list(kinds),
        ),
        timeout_policy=TimeoutPolicyDef(run_seconds=30, heartbeat_seconds=5),
        target=target,
        resources=claims,
    )


def _seed_invocation(change: Path, tree_id: str) -> None:
    from assurance_agent.workflow.core.events import append_event_strict

    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "inv-1",
            "entrypoint": "main",
            "graph_id": "g",
            "graph_digest": "e" * 64,
            "contract_digests": {"skill:aa-api-plan": "c" * 64},
            "params": {},
            "params_sha256": "f" * 64,
            "root_tree_id": tree_id,
            "max_parallel_tasks": 1,
            "checkpoint_ns": "root",
            "structural_path": "/g",
        },
    )


def test_scheduler_snapshot_before_started_and_crash_retry(tmp_path: Path) -> None:
    project, change, tree_id, claims = _seed_declared_project(tmp_path)
    (change / "events.jsonl").unlink(missing_ok=True)
    _seed_invocation(change, tree_id)
    store = TreeStore(change)
    contract = ExecutionContract(
        target="skill:aa-api-plan",
        handler="agent",
        reads=("change:plans/api-plan.md",),
        writes=("change:plans/api-plan.md",),
        authorization_writes=("change:plans/api-plan.md",),
        read_isolation="declared_only",
    )
    from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog

    catalog = ExecutionContractCatalog(contracts={contract.target: contract})
    task = _executable_task(
        task_id="plan-api",
        target=contract.target,
        claims=claims,
        contract_digest="c" * 64,
    )
    crashes = {"count": 0}

    def crash_after_snapshot(_task: ExecutableTask, snapshot_id: str) -> None:
        crashes["count"] += 1
        if crashes["count"] == 1:
            raise RuntimeError(f"crash after snapshot:{snapshot_id}")

    calls: list[str] = []

    class Runner:
        def execute(self, task, workspace, context):  # noqa: ANN001
            calls.append(task.task_id)
            return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=Runner(),
        contracts=catalog,
        crash_after_snapshot=crash_after_snapshot,
    )
    projection = fold_invocation_events("inv-1", read_events_strict(change))
    plan = PlanResult(superstep_id="ss-1", checkpoint_id="cp-1", tasks=(task,))
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )
    with pytest.raises(RuntimeError, match="crash after snapshot"):
        scheduler.execute(plan, projection, context)

    events = read_events_strict(change)
    assert not any(event.get("type") == "task_attempt_started" for event in events)
    # Retry with same ledger-derived attempt number.
    scheduler2 = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=Runner(),
        contracts=catalog,
    )
    projection2 = fold_invocation_events("inv-1", read_events_strict(change))
    result = scheduler2.execute(plan, projection2, context)
    assert result.succeeded == ("plan-api",)
    started = [event for event in read_events_strict(change) if event.get("type") == "task_attempt_started"]
    succeeded = [
        event for event in read_events_strict(change) if event.get("type") == "task_attempt_succeeded"
    ]
    assert len(started) == 1
    assert started[0]["attempt_number"] == 1
    snapshot_id = started[0]["input_snapshot_id"]
    assert isinstance(snapshot_id, str) and snapshot_id
    assert load_task_input_snapshot(store, snapshot_id)
    assert succeeded[0]["input_snapshot_id"] == snapshot_id
    assert succeeded[0].get("runtime_context_sha256") == started[0].get("runtime_context_sha256")


def test_lock_conflict_emits_scheduling_deferred_without_attempt(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    task = _executable_task(
        task_id="update-issue",
        target="operation:no-op",
        claims=ResourceClaims(
            reads=synchronized,
            writes=synchronized,
            synchronized=synchronized,
            exclusive=("project:issue-registry",),
            authorization_writes=synchronized,
        ),
        contract_digest="c" * 64,
        retry_on=["conflict"],
    ).model_copy(
        update={
            "retryable_errors": ("conflict",),
            "retry_policy": RetryPolicyDef(
                max_attempts=3,
                retry_on=["conflict"],
                backoff=BackoffDef(initial_seconds=60, multiplier=2, max_seconds=300),
            ),
        }
    )

    class BlockingLocks:
        def acquire(self, tokens, timeout_seconds=5.0):  # noqa: ANN001
            from assurance_agent.workflow.graph.project_locks import ProjectResourceConflict

            raise ProjectResourceConflict(token=tokens[0], message="held")

    class Runner:
        def execute(
            self,
            task: ExecutableTask,
            workspace: TaskWorkspace,
            context: RuntimeContext,
        ) -> TaskResult:
            raise AssertionError("runner must not run on deferral")

    class FrozenClock:
        def __init__(self) -> None:
            self._now = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

        def now(self) -> datetime:
            return self._now

    clock = FrozenClock()
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=Runner(),
        project_lock_manager=BlockingLocks(),  # type: ignore[arg-type]
        project_lock_timeout_seconds=0.05,
        clock=clock,  # type: ignore[arg-type]
    )
    projection = fold_invocation_events("inv-1", read_events_strict(change))
    plan = PlanResult(superstep_id="ss-1", checkpoint_id="cp-1", tasks=(task,))
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )
    result = scheduler.execute(plan, projection, context)
    assert result.failed == ()
    assert result.retry_at is not None
    events = read_events_strict(change)
    assert not any(event.get("type") == "task_attempt_started" for event in events)
    assert not any(event.get("type") == "task_attempt_failed" for event in events)
    deferred = [event for event in events if event.get("type") == "task_scheduling_deferred"]
    assert len(deferred) == 1
    assert "attempt_id" not in deferred[0]
    assert "attempt_number" not in deferred[0]
    assert deferred[0]["deferral_ordinal"] == 1

    # Suppression before next_retry_at (no additional deferral spam).
    projection2 = fold_invocation_events("inv-1", events)
    result2 = scheduler.execute(plan, projection2, context)
    assert result2.retry_at == deferred[0]["next_retry_at"]
    events2 = read_events_strict(change)
    deferred2 = [e for e in events2 if e.get("type") == "task_scheduling_deferred"]
    assert len(deferred2) == 1
    assert deferred2[0]["deferral_id"] == deferred[0]["deferral_id"]


def test_runtime_context_prompt_binding_rejects_mismatch() -> None:
    context = PlanFixerRuntimeContextV1.model_validate(_runtime_context())
    prompt = build_node_prompt(
        "aa-api-plan-fixer",
        "fix",
        "CH-1",
        allowed_writes=("change:plans/api-plan.md",),
        runtime_context=context,
    )
    assert "## Runtime Context" in prompt
    request = AgentRequest(
        target="skill:aa-api-plan-fixer",
        node_id="fix",
        change_id="CH-1",
        workspace_root=Path("/tmp/ws"),
        allowed_writes=("change:plans/api-plan.md",),
        prompt=prompt,
        timeout_seconds=30,
        runtime_context_sha256=runtime_context_digest(context),
    )
    assert request.runtime_context_sha256 == runtime_context_digest(context)
    with pytest.raises(TaskInputError):
        assert_prompt_runtime_context_match("not the bound runtime context block", context)


def test_ast_consumer_set_guards_for_snapshot_and_runtime_context_fields() -> None:
    repo = Path(__file__).resolve().parents[4]
    inventory = {
        "input_snapshot_id": {
            "assurance_agent/workflow/graph/scheduler.py": {
                "_begin_attempt",
                "_persist_success",
            },
            "assurance_agent/workflow/graph/checkpoint.py": {"fold_invocation_events"},
            "assurance_agent/workflow/core/graph_events.py": {
                "TaskAttemptStartedEvent",
                "TaskAttemptSucceededEvent",
            },
            "assurance_agent/eval/scorers/current_codegen.py": {
                "WriteAttribution",
                "classify_write_set_entries",
                "load_plan_case_bytes_from_snapshot",
            },
        },
        "runtime_context_sha256": {
            "assurance_agent/workflow/graph/scheduler.py": {
                "_begin_attempt",
                "_persist_success",
            },
            "assurance_agent/workflow/graph/checkpoint.py": {"fold_invocation_events"},
            "assurance_agent/workflow/graph/agent_api.py": {
                "AgentRequest",
                "build_node_prompt",
            },
            "assurance_agent/workflow/graph/task_inputs.py": {
                "prepare_plan_fixer_runtime_context",
                "resume_plan_fixer_runtime_context",
            },
            "assurance_agent/workflow/core/graph_events.py": {
                "TaskAttemptStartedEvent",
                "TaskAttemptSucceededEvent",
            },
            "assurance_agent/eval/scorers/current_codegen.py": {
                "WriteAttribution",
                "load_plan_case_bytes_from_snapshot",
            },
        },
    }

    for field, files in inventory.items():
        for rel, symbols in files.items():
            path = repo / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found_symbols: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name not in symbols:
                        continue
                    for child in ast.walk(node):
                        if isinstance(child, ast.Name) and child.id == field:
                            found_symbols.add(node.name)
                        if isinstance(child, ast.Attribute) and child.attr == field:
                            found_symbols.add(node.name)
                        if isinstance(child, ast.Constant) and child.value == field:
                            found_symbols.add(node.name)
            missing = symbols - found_symbols
            assert not missing, f"{field} missing AST consumers in {rel}: {sorted(missing)}"


def test_dormant_runtime_context_surface_has_zero_events_jsonl_references() -> None:
    """Closed inventory for Task 4 dormant code only; skill SKILL.md flip waits for Task 15."""
    repo = Path(__file__).resolve().parents[4]
    closed_paths = (
        "assurance_agent/workflow/graph/task_inputs.py",
        "assurance_agent/workflow/graph/agent_api.py",
        "assurance_agent/workflow/graph/handlers/agent.py",
    )
    needle = "events.jsonl"
    for rel in closed_paths:
        path = repo / rel
        text = path.read_text(encoding="utf-8")
        assert needle not in text, f"{rel} must not reference {needle}"
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert needle not in node.value, f"{rel} AST string references {needle}"
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        assert needle not in part.value, f"{rel} f-string references {needle}"


def test_capture_rejects_wrong_tree_or_review_bytes(tmp_path: Path) -> None:
    project, change, _tree_id, _claims = _seed_declared_project(tmp_path)
    review = change / "review" / "api-plan-review.json"
    review.parent.mkdir(parents=True)
    review.write_text('{"decision":"needs_fix"}\n', encoding="utf-8")
    review_digest = sha256_bytes(review.read_bytes())
    store = TreeStore(change)
    tree_id = store.capture(project, repo_root=project)
    claims = ResourceClaims(
        reads=(
            ResourcePath.parse("change:plans/api-plan.md"),
            ResourcePath.parse("change:review/api-plan-review.json"),
        ),
        writes=(ResourcePath.parse("change:plans/api-plan.md"),),
        authorization_writes=(ResourcePath.parse("change:plans/api-plan.md"),),
    )
    backend = WorkspaceBackend(change)
    workspace = backend.create(
        task_id="plan-api",
        base_tree_id=tree_id,
        store=store,
        sidecar_root=backend.sidecar_root_for("plan-api"),
        claims=claims,
        declared_reads_only=True,
        skill_name="aa-api-plan",
        initialize_git=False,
    )
    task = _executable_task(
        task_id="plan-api",
        target="skill:aa-api-plan-fixer",
        claims=claims,
        contract_digest="c" * 64,
    )
    contract = ExecutionContract(
        target="skill:aa-api-plan-fixer",
        handler="agent",
        reads=("change:plans/api-plan.md", "change:review/api-plan-review.json"),
        writes=("change:plans/api-plan.md",),
        authorization_writes=("change:plans/api-plan.md",),
        read_isolation="declared_only",
    )
    context = PlanFixerRuntimeContextV1.model_validate(
        _runtime_context(
            task_id="plan-api",
            attempt_id="plan-api-a1",
            base_tree_id=tree_id,
            source_review_path="change:review/api-plan-review.json",
            source_review_sha256=review_digest,
        )
    )
    wrong_tree = PlanFixerRuntimeContextV1.model_validate(
        {**context.model_dump(mode="json"), "base_tree_id": "tree-other"}
    )
    with pytest.raises(TaskInputError, match="base_tree_id mismatch"):
        capture_task_input_snapshot(
            invocation_id="inv-1",
            task=task,
            attempt_id="plan-api-a1",
            workspace=workspace,
            contract=contract,
            runtime_context=wrong_tree,
        )
    wrong_review = PlanFixerRuntimeContextV1.model_validate(
        {**context.model_dump(mode="json"), "source_review_sha256": DIGEST_B}
    )
    with pytest.raises(TaskInputError, match="source_review_sha256 mismatch"):
        capture_task_input_snapshot(
            invocation_id="inv-1",
            task=task,
            attempt_id="plan-api-a1",
            workspace=workspace,
            contract=contract,
            runtime_context=wrong_review,
        )


def test_scheduler_crash_after_started_keeps_reachable_snapshot(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
    from assurance_agent.workflow.graph.leases import abandon_running_attempt

    project, change, tree_id, claims = _seed_declared_project(tmp_path)
    (change / "events.jsonl").unlink(missing_ok=True)
    _seed_invocation(change, tree_id)
    store = TreeStore(change)
    contract = ExecutionContract(
        target="skill:aa-api-plan",
        handler="agent",
        reads=("change:plans/api-plan.md",),
        writes=("change:plans/api-plan.md",),
        authorization_writes=("change:plans/api-plan.md",),
        read_isolation="declared_only",
    )
    catalog = ExecutionContractCatalog(contracts={contract.target: contract})
    task = _executable_task(
        task_id="plan-api",
        target=contract.target,
        claims=claims,
        contract_digest="c" * 64,
    )

    def crash_after_started(_task: ExecutableTask, attempt_id: str) -> None:
        raise RuntimeError(f"crash after started:{attempt_id}")

    class Runner:
        def execute(self, task, workspace, context):  # noqa: ANN001
            return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=Runner(),
        contracts=catalog,
        crash_after_started=crash_after_started,
    )
    projection = fold_invocation_events("inv-1", read_events_strict(change))
    plan = PlanResult(superstep_id="ss-1", checkpoint_id="cp-1", tasks=(task,))
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )
    with pytest.raises(RuntimeError, match="crash after started"):
        scheduler.execute(plan, projection, context)

    events = read_events_strict(change)
    started = [event for event in events if event.get("type") == "task_attempt_started"]
    assert len(started) == 1
    assert started[0]["attempt_number"] == 1
    snapshot_id = started[0]["input_snapshot_id"]
    assert isinstance(snapshot_id, str) and snapshot_id
    assert load_task_input_snapshot(store, snapshot_id)
    attempt_id = started[0]["attempt_id"]
    assert isinstance(attempt_id, str)

    abandoned = abandon_running_attempt(
        change,
        invocation_id="inv-1",
        checkpoint_ns="root",
        task_id="plan-api",
        attempt_id=attempt_id,
        reason="crash after started",
        abandoned_at=datetime(2026, 8, 1, 0, 0, 1, tzinfo=timezone.utc).isoformat(),
    )
    assert abandoned is True

    scheduler2 = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=Runner(),
        contracts=catalog,
    )
    projection2 = fold_invocation_events("inv-1", read_events_strict(change))
    result = scheduler2.execute(plan, projection2, context)
    assert result.succeeded == ("plan-api",)
    started_all = [
        event for event in read_events_strict(change) if event.get("type") == "task_attempt_started"
    ]
    assert len(started_all) == 2
    assert started_all[1]["attempt_number"] == 2
    first_snapshot = started_all[0]["input_snapshot_id"]
    second_snapshot = started_all[1]["input_snapshot_id"]
    assert isinstance(first_snapshot, str) and first_snapshot
    assert isinstance(second_snapshot, str) and second_snapshot
    assert load_task_input_snapshot(store, first_snapshot)
    assert load_task_input_snapshot(store, second_snapshot)


def test_lock_deferral_reselects_via_plan_superstep_drive_after_release(tmp_path: Path) -> None:
    """P0/D13: after backoff + lock release, real plan/_drive creates attempt_number 1 once."""
    from datetime import datetime, timedelta, timezone

    from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.driver.operations_catalog import default_operations
    from assurance_agent.workflow.graph.handlers.operation import OperationHandler
    from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
    from assurance_agent.workflow.graph.project_locks import ProjectResourceConflict
    from assurance_agent.workflow.graph.runtime import GraphRuntime
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
    from tests.helpers_aa import write_aa_config

    t0 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

    class FakeClock:
        def __init__(self) -> None:
            self._now = t0
            self._mono = 0.0

        def now(self) -> datetime:
            return self._now

        def monotonic(self) -> float:
            return self._mono

        def sleep(self, seconds: float) -> None:
            self._now += timedelta(seconds=seconds)
            self._mono += seconds

    class ReleaseAfterFirstConflict:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self, tokens, timeout_seconds=5.0):  # noqa: ANN001
            from contextlib import nullcontext

            self.acquires += 1
            if self.acquires == 1:
                raise ProjectResourceConflict(token=tokens[0], message="held")
            return nullcontext()

    contracts_text = """\
schema_version: "1"
contracts:
  operation:update-issue:
    handler: operation
    side_effect_free: false
    reads: ["project:qa/issues/**"]
    writes: ["project:qa/issues/**", "change:results/**"]
    authorization_writes: ["project:qa/issues/**", "change:results/**"]
    synchronized: ["project:qa/issues/**"]
    exclusive: ["project:issue-registry"]
    retryable_errors: [conflict]
"""
    workflow_text = """\
schema_version: "2"
name: deferred-reselect
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    conflict:
      max_attempts: 3
      retry_on: [conflict]
      backoff: {initial_seconds: 60, multiplier: 2, max_seconds: 300, jitter: false}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 5}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 8
    nodes:
      update:
        uses: operation:update-issue
        outputs:
          - project:qa/issues/ISSUE-1.json
          - change:results/update.json
        retry: conflict
        timeout: local
    edges:
      - {from: START, to: update}
      - {from: update, to: END}
gates: {}
"""
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(project)
    issue = project / "qa" / "issues" / "ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    (project / "app").mkdir()
    (project / "app" / "source.py").write_text("v1\n", encoding="utf-8")

    contracts = parse_execution_contracts(contracts_text)
    compiled = compile_workflow(parse_workflow_v2(workflow_text), contracts)
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    locks = ReleaseAfterFirstConflict()

    def update_issue(task, workspace, context):  # noqa: ANN001
        (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":2}\n', encoding="utf-8")
        result = workspace.change_dir / "results" / "update.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"updated":true}\n', encoding="utf-8")
        return TaskResult(status="succeeded")

    ops = default_operations()
    ops["operation:update-issue"] = update_issue
    op_handler = OperationHandler(ops)
    node_runner = HandlerNodeRunner({target: op_handler for target in ops})
    graph_id = compiled.entrypoints["full"].graph_id
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=clock,
        workspace_backend=workspaces,
        node_runner=node_runner,
        max_parallel_tasks=1,
        contracts=contracts,
        state_defs=dict(compiled.schema.graphs[graph_id].state),
        project_lock_manager=locks,  # type: ignore[arg-type]
        project_lock_timeout_seconds=0.05,
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )
    result = runtime.run(compiled, "full", context)
    assert result.exit_code == 0
    events = read_events_strict(change)
    deferred = [event for event in events if event.get("type") == "task_scheduling_deferred"]
    started = [event for event in events if event.get("type") == "task_attempt_started"]
    failed = [event for event in events if event.get("type") == "task_attempt_failed"]
    assert len(deferred) == 1
    assert len(started) == 1
    assert started[0]["attempt_number"] == 1
    assert failed == []
    assert locks.acquires >= 2
    assert issue.read_text(encoding="utf-8") == '{"version":2}\n'
