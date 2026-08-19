"""Precommit Class II checks must not live-read producer-writable paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.workflow.graph.precommit import (
    ARCHIVE_INTEGRITY_V1,
    CROSS_ARTIFACT_INVARIANTS_V1,
    CandidateValidationError,
    CandidateValidationReceiptV1,
    PrecommitValidationContext,
    validate_candidate,
)
from assurance_agent.workflow.graph.task_inputs import (
    TaskInputSnapshotEntryV1,
    TaskInputSnapshotV1,
    _entries_input_sha256,
    load_task_input_snapshot,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WriteSet
from assurance_agent.workflow.issues.identity import candidate_document_digest

_INV = "inv-isolation-1"
_TREE = "a" * 64
_WRITE_SET_ID = "b" * 64
_CONTRACT_DIGEST = "sha256:" + "c" * 64
_CLAIMS_DIGEST = "sha256:" + "e" * 64
_TASK = "isolation-task"
_ATTEMPT = "isolation-task-a1"

_CASE_REVIEW = {
    "schema_version": "1.0",
    "review_type": "case",
    "change_id": "CH-1",
    "decision": "pass",
    "findings": [],
    "auto_fix_plan": [],
    "next_action": "continue",
    "auto_fix_allowed": False,
    "human_review_required": False,
    "risk_level": "low",
    "minimum_coverage": {
        "total_required": 1,
        "covered": 1,
        "skipped_by_scope": 0,
        "missing": [],
    },
    "source_verification": {
        "independent": True,
        "reviewed_source_files": ["app/api.py"],
        "verified_claims": [{"claim": "route exists", "evidence_files": ["app/api.py"]}],
    },
}

_MATRIX = [
    {
        "mrc_id": "MRC-API-001",
        "key": "list_users",
        "required": True,
        "covered_by_cases": ["TC-1"],
        "status": "covered",
    }
]


def _store_bytes(store: TreeStore, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    store._write_object(digest, data)
    return digest


def _entry(
    *,
    physical: str,
    aliases: list[str],
    digest: str,
    claim: str,
) -> TaskInputSnapshotEntryV1:
    return TaskInputSnapshotEntryV1(
        physical_relpath=physical,
        repo_relpath=physical,
        logical_aliases=sorted(aliases),
        matched_claims=[claim],
        origins=["contract_read"],
        kind="file",
        mode=0o644,
        sha256=f"sha256:{digest}",
        symlink_target=None,
    )


def _bind(
    store: TreeStore,
    *,
    target: str,
    outputs: dict[str, bytes],
    snapshot_entries: list[TaskInputSnapshotEntryV1],
) -> tuple[PrecommitValidationContext, WriteSet]:
    outputs_sha256 = {logical: _store_bytes(store, data) for logical, data in sorted(outputs.items())}
    entries = sorted(snapshot_entries, key=lambda item: item.physical_relpath)
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id=_INV,
        task_id=_TASK,
        attempt_id=_ATTEMPT,
        base_tree_id=_TREE,
        materialized_tree_id=_TREE,
        input_sha256=_entries_input_sha256(entries),
        runtime_context_sha256=None,
        contract_digest=_CONTRACT_DIGEST,
        claims_digest=_CLAIMS_DIGEST,
        entries=list(entries),
    )
    raw = canonical_json_bytes(snapshot)
    snapshot_id = hashlib.sha256(raw).hexdigest()
    store_task_input_snapshot(store, snapshot_id, raw)
    write_set = WriteSet(
        write_set_id=_WRITE_SET_ID,
        task_id=_TASK,
        base_tree_id=_TREE,
        entries=(),
        outputs_sha256=outputs_sha256,
        base_tree_roots={"change": "qa/changes/CH-1", "project": "."},
    )
    context = PrecommitValidationContext.model_validate(
        {
            "root_invocation_id": _INV,
            "invocation_id": _INV,
            "task_id": _TASK,
            "attempt_id": _ATTEMPT,
            "target": target,
            "base_tree_id": _TREE,
            "current_tree_id": _TREE,
            "input_snapshot_id": snapshot_id,
            "contract_digest": _CONTRACT_DIGEST,
            "policy_object_id": "d" * 64,
            "policy_digest": "sha256:" + "d" * 64,
            "gate_attempt_id": None,
            "interrupt_id": None,
            "output_digests": dict(sorted(outputs_sha256.items())),
            "write_set_id": _WRITE_SET_ID,
            "definition_semantics": {
                "assurance_profile_digest": "unbound",
                "contract_digest": _CONTRACT_DIGEST,
                "gate_semantics_digest": "unbound",
                "graph_digest": "f" * 64,
            },
        }
    )
    return context, write_set


def _validate(
    validator_id: str,
    store: TreeStore,
    context: PrecommitValidationContext,
    write_set: WriteSet,
    *,
    project_root: Path | None = None,
    host_change_dir: Path | None = None,
) -> tuple[str, CandidateValidationReceiptV1]:
    snapshot = load_task_input_snapshot(store, context.input_snapshot_id)
    return validate_candidate(
        validator_id,
        context,
        store=store,
        write_set=write_set,
        input_snapshot=snapshot,
        plan_text="",
        cases=[],
        change_id="CH-1",
        layer="api",
        current_change_repo_path="qa/changes/CH-1",
        project_root=project_root,
        host_change_dir=host_change_dir,
    )


def test_archive_integrity_accepts_snapshot_source_when_live_disk_mutated(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    source = b"archived-from-snapshot\n"
    source_digest = _store_bytes(store, source)
    live = change / "proposal.md"
    live.write_bytes(b"mutated-live-disk\n")
    context, write_set = _bind(
        store,
        target="skill:aa-archive",
        outputs={"project:qa/archive/CH-1/proposal.md": source},
        snapshot_entries=[
            _entry(
                physical="qa/changes/CH-1/proposal.md",
                aliases=["change:proposal.md", "project:qa/changes/CH-1/proposal.md"],
                digest=source_digest,
                claim="project:qa/changes/**",
            )
        ],
    )
    _receipt_id, receipt = _validate(
        ARCHIVE_INTEGRITY_V1,
        store,
        context,
        write_set,
        project_root=tmp_path,
    )
    assert receipt.validator_id == ARCHIVE_INTEGRITY_V1


def test_archive_integrity_rejects_missing_snapshot_source_even_if_live_file_matches(
    tmp_path: Path,
) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    source = b"copied-bytes\n"
    (change / "proposal.md").write_bytes(source)
    context, write_set = _bind(
        store,
        target="skill:aa-archive",
        outputs={"project:qa/archive/CH-1/proposal.md": source},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="snapshot"):
        _validate(ARCHIVE_INTEGRITY_V1, store, context, write_set, project_root=tmp_path)


def test_archive_integrity_rejects_when_no_snapshot_source_is_compared(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    context, write_set = _bind(
        store,
        target="skill:aa-archive",
        outputs={"project:qa/archive/CH-1/archive-summary.md": b"# summary\n"},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="snapshot"):
        _validate(ARCHIVE_INTEGRITY_V1, store, context, write_set, project_root=tmp_path)


def test_cross_artifact_rejects_unreadable_must_compat_write_set(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    context, write_set = _bind(
        store,
        target="skill:aa-issue-analyzer",
        outputs={"change:inspect/issue-candidates.json": b"not-json{"},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError):
        _validate(CROSS_ARTIFACT_INVARIANTS_V1, store, context, write_set, project_root=tmp_path)


def test_case_review_mrc_ignores_live_change_dir_matrix(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "trace").mkdir()
    (change / "trace" / "minimum-coverage-matrix.json").write_text(
        json.dumps(_MATRIX),
        encoding="utf-8",
    )
    store = TreeStore(change)
    disagreeing = dict(_CASE_REVIEW)
    disagreeing["minimum_coverage"] = {
        "total_required": 0,
        "covered": 0,
        "skipped_by_scope": 0,
        "missing": [],
    }
    context, write_set = _bind(
        store,
        target="skill:aa-case-reviewer",
        outputs={"change:review/case-review.json": json.dumps(disagreeing).encode("utf-8")},
        snapshot_entries=[],
    )
    _receipt_id, receipt = _validate(
        CROSS_ARTIFACT_INVARIANTS_V1,
        store,
        context,
        write_set,
        project_root=tmp_path,
    )
    assert receipt.validator_id == CROSS_ARTIFACT_INVARIANTS_V1


def test_case_review_mrc_reads_matrix_from_snapshot(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    matrix_bytes = json.dumps(_MATRIX).encode("utf-8")
    matrix_digest = _store_bytes(store, matrix_bytes)
    (change / "trace").mkdir()
    (change / "trace" / "minimum-coverage-matrix.json").write_text("[]", encoding="utf-8")
    context, write_set = _bind(
        store,
        target="skill:aa-case-reviewer",
        outputs={"change:review/case-review.json": json.dumps(_CASE_REVIEW).encode("utf-8")},
        snapshot_entries=[
            _entry(
                physical="qa/changes/CH-1/trace/minimum-coverage-matrix.json",
                aliases=["change:trace/minimum-coverage-matrix.json"],
                digest=matrix_digest,
                claim="change:trace/minimum-coverage-matrix.json",
            )
        ],
    )
    _receipt_id, receipt = _validate(
        CROSS_ARTIFACT_INVARIANTS_V1,
        store,
        context,
        write_set,
        project_root=tmp_path,
    )
    assert receipt.validator_id == CROSS_ARTIFACT_INVARIANTS_V1


def test_source_verification_rejects_subgraph_project_root(tmp_path: Path) -> None:
    host = tmp_path / "sut"
    host_change = host / "qa" / "changes" / "CH-1"
    host_change.mkdir(parents=True)
    (host / "app").mkdir()
    (host / "app" / "api.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    task_ws = tmp_path / "task-ws"
    (task_ws / "app").mkdir(parents=True)
    (task_ws / "app" / "api.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (task_ws / ".aa").mkdir()
    (task_ws / ".aa" / "config.yaml").write_text(
        yaml.safe_dump({"sources": {"frontend": "web", "backend": "app"}}),
        encoding="utf-8",
    )
    store = TreeStore(host_change)
    proposal = (
        "# Proposal\n\n"
        "## Product Source Verification\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `app/api.py`\n"
    ).encode("utf-8")
    context, write_set = _bind(
        store,
        target="skill:aa-case-design",
        outputs={"change:proposal.md": proposal},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="subgraph|host"):
        _validate(
            CROSS_ARTIFACT_INVARIANTS_V1,
            store,
            context,
            write_set,
            project_root=task_ws,
            host_change_dir=host_change,
        )


def test_source_verification_uses_host_root_not_workspace_forgeries(tmp_path: Path) -> None:
    host = tmp_path / "sut"
    host_change = host / "qa" / "changes" / "CH-1"
    host_change.mkdir(parents=True)
    (host / "app").mkdir()
    (host / "app" / "api.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (host / ".aa").mkdir()
    (host / ".aa" / "config.yaml").write_text(
        yaml.safe_dump({"sources": {"frontend": "web", "backend": "app"}}),
        encoding="utf-8",
    )
    task_ws = tmp_path / "task-ws"
    (task_ws / "app").mkdir(parents=True)
    (task_ws / "app" / "api.py").write_text("def forged():\n    return 0\n", encoding="utf-8")
    (task_ws / "app" / "fake.py").write_text("def fake():\n    return 0\n", encoding="utf-8")
    store = TreeStore(host_change)
    proposal = (
        "# Proposal\n\n"
        "## Product Source Verification\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `app/api.py`\n"
    ).encode("utf-8")
    context, write_set = _bind(
        store,
        target="skill:aa-case-design",
        outputs={"change:proposal.md": proposal},
        snapshot_entries=[],
    )
    _receipt_id, receipt = _validate(
        CROSS_ARTIFACT_INVARIANTS_V1,
        store,
        context,
        write_set,
        project_root=host,
        host_change_dir=host_change,
    )
    assert receipt.validator_id == CROSS_ARTIFACT_INVARIANTS_V1

    forged = (
        "# Proposal\n\n"
        "## Product Source Verification\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `app/fake.py`\n"
    ).encode("utf-8")
    forged_context, forged_write_set = _bind(
        store,
        target="skill:aa-case-design",
        outputs={"change:proposal.md": forged},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="reviewed_source_files"):
        _validate(
            CROSS_ARTIFACT_INVARIANTS_V1,
            store,
            forged_context,
            forged_write_set,
            project_root=host,
            host_change_dir=host_change,
        )


def test_nested_runtime_context_source_verification_uses_host_not_workspace(
    tmp_path: Path,
) -> None:
    """Nested subgraph remaps ``project_root``; live-read must still use the SUT."""
    from assurance_agent.workflow.graph.attempt_engine import _precommit_project_root
    from assurance_agent.workflow.graph.models import RuntimeContext

    host = tmp_path / "sut"
    host_change = host / "qa" / "changes" / "CH-1"
    host_change.mkdir(parents=True)
    (host / "app").mkdir()
    (host / "app" / "api.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (host / ".aa").mkdir()
    (host / ".aa" / "config.yaml").write_text(
        yaml.safe_dump({"sources": {"frontend": "web", "backend": "app"}}),
        encoding="utf-8",
    )
    task_ws = tmp_path / "task-ws"
    (task_ws / "app").mkdir(parents=True)
    (task_ws / "app" / "api.py").write_text("def forged():\n    return 0\n", encoding="utf-8")
    (task_ws / "app" / "fake.py").write_text("def fake():\n    return 0\n", encoding="utf-8")
    store = TreeStore(host_change)
    proposal = (
        "# Proposal\n\n"
        "## Product Source Verification\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `app/api.py`\n"
    ).encode("utf-8")
    context, write_set = _bind(
        store,
        target="skill:aa-case-design",
        outputs={"change:proposal.md": proposal},
        snapshot_entries=[],
    )
    runtime = RuntimeContext(
        project_root=task_ws,
        repo_root=task_ws,
        change_dir=host_change,
        change_id="CH-1",
        host_project_root=host,
    )
    _receipt_id, receipt = _validate(
        CROSS_ARTIFACT_INVARIANTS_V1,
        store,
        context,
        write_set,
        project_root=_precommit_project_root(runtime),
        host_change_dir=runtime.change_dir,
    )
    assert receipt.validator_id == CROSS_ARTIFACT_INVARIANTS_V1

    forged = (
        "# Proposal\n\n"
        "## Product Source Verification\n"
        "- independently_read: true\n"
        "- reviewed_source_files:\n"
        "  - `app/fake.py`\n"
    ).encode("utf-8")
    forged_context, forged_write_set = _bind(
        store,
        target="skill:aa-case-design",
        outputs={"change:proposal.md": forged},
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="reviewed_source_files"):
        _validate(
            CROSS_ARTIFACT_INVARIANTS_V1,
            store,
            forged_context,
            forged_write_set,
            project_root=_precommit_project_root(runtime),
            host_change_dir=runtime.change_dir,
        )


def test_issue_candidate_digest_mismatch_is_precommit_error(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    candidate = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [],
    }
    status = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "status": "completed",
        "evidence_bundle_digest": "sha256:evidence",
        "candidate_count": 0,
        "candidate_digest": "sha256:raw-file-bytes",
    }
    context, write_set = _bind(
        store,
        target="skill:aa-issue-analyzer",
        outputs={
            "change:inspect/issue-candidates.json": json.dumps(candidate).encode("utf-8"),
            "change:inspect/issue-analysis-status.json": json.dumps(status).encode("utf-8"),
        },
        snapshot_entries=[],
    )
    with pytest.raises(CandidateValidationError, match="candidate_digest"):
        _validate(CROSS_ARTIFACT_INVARIANTS_V1, store, context, write_set, project_root=tmp_path)
    assert candidate_document_digest(candidate) != status["candidate_digest"]
