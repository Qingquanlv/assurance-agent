"""D15 pinned physical evidence-path resolver and write-set root binding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.evidence_paths import (
    EvidencePathError,
    pinned_write_set_roots,
    resolve_evidence_path,
    verify_write_set_base_tree_roots,
)
from assurance_agent.workflow.graph.workspace import (
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
    WriteSet,
    _canonical_json,
)

_PRIVATE_ROOTS = (
    "tests/api/test_sample.py",
    "tests/e2e/test_sample.py",
    "tests/fuzz/test_sample.py",
    "tests/perf/test_sample.py",
)

_ALIASED_ROOTS = {
    "change": "qa/changes/CH-1",
    "project": ".",
    "repo": ".",
}


def _resolve(logical_path: str, *, roots: dict[str, str] | None = None, change: str = "qa/changes/CH-1"):
    return resolve_evidence_path(
        logical_path=logical_path,
        tree_roots=roots or _ALIASED_ROOTS,
        current_change_repo_path=change,
    )


@pytest.mark.parametrize("repo_path", _PRIVATE_ROOTS)
def test_aliased_private_roots_resolve_to_repo_ownership(repo_path: str) -> None:
    # Freeze serializes as project:* when project/repo both map to ".".
    resolved = _resolve(f"project:{repo_path}")
    assert resolved.physical_relpath == repo_path
    assert resolved.repo_relpath == repo_path
    assert resolved.ownership == "repo"
    assert resolved.logical_aliases == sorted([f"project:{repo_path}", f"repo:{repo_path}"])


def test_current_change_output_owns_more_specific_than_repo_project() -> None:
    logical = "project:qa/changes/CH-1/codegen/api-codegen-summary.md"
    resolved = _resolve(logical)
    assert resolved.physical_relpath == "qa/changes/CH-1/codegen/api-codegen-summary.md"
    assert resolved.ownership == "current_change"
    assert "change:codegen/api-codegen-summary.md" in resolved.logical_aliases
    assert "project:qa/changes/CH-1/codegen/api-codegen-summary.md" in resolved.logical_aliases


def test_missing_pinned_root_map_fails_closed() -> None:
    with pytest.raises(EvidencePathError) as excinfo:
        resolve_evidence_path(
            logical_path="repo:tests/api/a.py",
            tree_roots={},
            current_change_repo_path="qa/changes/CH-1",
        )
    assert excinfo.value.code == "missing_pinned_roots"


def test_traversal_and_absolute_paths_fail_closed() -> None:
    with pytest.raises(EvidencePathError) as excinfo:
        _resolve("repo:tests/../secret.py")
    assert excinfo.value.code == "path_traversal"

    with pytest.raises(EvidencePathError) as excinfo:
        resolve_evidence_path(
            logical_path="repo:tests/api/a.py",
            tree_roots={"project": "/abs", "repo": "/abs", "change": "qa/changes/CH-1"},
            current_change_repo_path="qa/changes/CH-1",
        )
    assert excinfo.value.code == "absolute_path"

    with pytest.raises(EvidencePathError) as excinfo:
        resolve_evidence_path(
            logical_path="repo:tests/api/a.py",
            tree_roots=_ALIASED_ROOTS,
            current_change_repo_path="/abs/change",
        )
    assert excinfo.value.code == "absolute_path"


def test_another_change_fails_closed() -> None:
    with pytest.raises(EvidencePathError) as excinfo:
        _resolve("project:qa/changes/CH-OTHER/plans/api-plan.md")
    assert excinfo.value.code == "another_change"


def test_ambiguous_non_nested_containment_fails_closed() -> None:
    # Repo nested inside current_change: longest prefix says repo, precedence says
    # current_change → fail closed as ambiguous containment.
    roots = {
        "change": "qa/changes/CH-1",
        "project": ".",
        "repo": "qa/changes/CH-1/nested-repo",
    }
    with pytest.raises(EvidencePathError) as excinfo:
        resolve_evidence_path(
            logical_path="repo:pkg/mod.py",
            tree_roots=roots,
            current_change_repo_path="qa/changes/CH-1",
        )
    assert excinfo.value.code == "ambiguous_containment"


def test_unowned_physical_path_fails_closed() -> None:
    from assurance_agent.workflow.graph import evidence_paths as mod

    with pytest.raises(EvidencePathError) as excinfo:
        mod._assign_ownership(
            "orphan/file.py",
            {
                "current_change": "qa/changes/CH-1",
                "repo": "app",
                "project": "app",
            },
        )
    assert excinfo.value.code == "unowned_path"


def test_symlink_prefix_cannot_rewrite_lexical_physical_identity() -> None:
    # A pinned prefix that would be a symlink on a live FS is still lexical here.
    roots = {
        "change": "qa/changes/CH-1",
        "project": ".",
        "repo": "sym-link",
    }
    resolved = resolve_evidence_path(
        logical_path="repo:tests/api/a.py",
        tree_roots=roots,
        current_change_repo_path="qa/changes/CH-1",
    )
    assert resolved.physical_relpath == "sym-link/tests/api/a.py"
    assert resolved.repo_relpath == "tests/api/a.py"
    assert resolved.ownership == "repo"
    assert resolved.logical_aliases == sorted(["project:sym-link/tests/api/a.py", "repo:tests/api/a.py"])


def test_historical_write_set_without_roots_readable_but_not_valid_for_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "a.py").write_text("x\n", encoding="utf-8")
    store = TreeStore(change)
    base_tree_id = store.capture(project)
    # Historical payload: no base_tree_roots field.
    payload = {
        "base_tree_id": base_tree_id,
        "entries": [],
        "kind": "write_set",
        "outputs_sha256": {},
        "project_exclusive_tokens": [],
        "synchronized_paths": [],
        "task_id": "historical-task",
        "version": 1,
    }
    raw = _canonical_json(payload)
    write_set_id = hashlib.sha256(raw).hexdigest()
    store._write_object(write_set_id, raw)  # noqa: SLF001 — intentional CAS fixture
    loaded = store.load_write_set(write_set_id)
    assert loaded.base_tree_roots is None

    with pytest.raises(EvidencePathError) as excinfo:
        pinned_write_set_roots(loaded)
    assert excinfo.value.code == "missing_pinned_roots"


def test_base_tree_roots_mismatch_at_load_and_verify(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "a.py").write_text("x\n", encoding="utf-8")
    store = TreeStore(change)
    base_tree_id = store.capture(project)
    wrong_roots = {"change": "qa/changes/CH-1", "project": ".", "repo": "not-the-tree"}
    payload = {
        "base_tree_id": base_tree_id,
        "base_tree_roots": wrong_roots,
        "entries": [],
        "kind": "write_set",
        "outputs_sha256": {},
        "project_exclusive_tokens": [],
        "synchronized_paths": [],
        "task_id": "mismatch-task",
        "version": 1,
    }
    raw = _canonical_json(payload)
    write_set_id = hashlib.sha256(raw).hexdigest()
    store._write_object(write_set_id, raw)  # noqa: SLF001
    with pytest.raises(WorkspaceError, match="base_tree_roots disagree"):
        store.load_write_set(write_set_id)

    synthetic = WriteSet(
        write_set_id="a" * 64,
        task_id="t",
        base_tree_id=base_tree_id,
        entries=(),
        outputs_sha256={},
        base_tree_roots=wrong_roots,
    )
    with pytest.raises(EvidencePathError) as excinfo:
        verify_write_set_base_tree_roots(synthetic, store.tree_roots(base_tree_id))
    assert excinfo.value.code == "base_tree_roots_mismatch"


def test_freeze_embeds_verified_base_tree_roots_in_write_set_id(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "a.py").write_text("base\n", encoding="utf-8")
    store = TreeStore(change)
    backend = WorkspaceBackend(change)
    base_tree_id = store.capture(project)
    workspace = backend.create(task_id="task-a", base_tree_id=base_tree_id, store=store)
    (workspace.root / "tests" / "api" / "a.py").write_text("changed\n", encoding="utf-8")
    from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath

    claims = ResourceClaims(
        writes=(ResourcePath.parse("repo:tests/api/**"),),
        authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
    )
    write_set = store.freeze_write_set(workspace, claims=claims)
    expected_roots = dict(store.tree_roots(base_tree_id))
    assert write_set.base_tree_roots == expected_roots
    loaded = store.load_write_set(write_set.write_set_id)
    assert loaded == write_set
    assert verify_write_set_base_tree_roots(loaded, expected_roots) == expected_roots

    # Roots participate in write_set_id: dropping them yields a different digest.
    payload = json.loads(store.read_object(write_set.write_set_id))
    assert payload["base_tree_roots"] == expected_roots
    without = {k: v for k, v in payload.items() if k != "base_tree_roots"}
    assert hashlib.sha256(_canonical_json(without)).hexdigest() != write_set.write_set_id
