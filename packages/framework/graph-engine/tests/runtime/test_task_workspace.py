from __future__ import annotations

import hashlib
import os
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

import graph_engine.attempts.workspace as task_workspace
from graph_engine.attempts.keys import AttemptKey
from graph_engine.plugin_api import ResourceClaims, TaskWorkspaceBinding
from graph_engine.attempts.workspace import (
    TaskWorkspaceProvider,
    TaskWorkspaceStore,
    TaskWorkspaceViolation,
)


def _store(tmp_path: Path) -> tuple[TaskWorkspaceStore, Path, Path]:
    project = tmp_path / "project"
    attempts = tmp_path / "attempts"
    receipts = tmp_path / "receipts"
    project.mkdir()
    return TaskWorkspaceStore(project, attempts, receipts), project, attempts


def _begin(store: TaskWorkspaceStore, *, claims: tuple[str, ...] = ("out",)):
    return store.begin(task_id="task/unsafe-but-stable", attempt=1, output_paths=claims)


def test_begin_creates_empty_attempt_root_with_stable_path_free_identity(tmp_path: Path) -> None:
    store, project, attempts = _store(tmp_path)

    first = _begin(store)
    second = _begin(store)

    assert first.project_root == project.resolve()
    assert (
        first.write_root
        == attempts.resolve()
        / hashlib.sha256(b"task/unsafe-but-stable").hexdigest()
        / first.identity.attempt_id
    )
    assert list(first.write_root.iterdir()) == []
    assert first.identity == second.identity
    assert str(project) not in first.identity.model_dump_json()
    assert str(first.write_root) not in first.identity.model_dump_json()


def test_begin_accepts_an_empty_write_claim_set(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)

    binding = store.begin(task_id="read-only", attempt=1, output_paths=())

    assert binding.identity.output_paths == ()
    assert store.seal(binding.identity).files == ()


def test_begin_captures_only_declared_output_baselines(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out").mkdir()
    (project / "out" / "nested.txt").write_bytes(b"before")
    (project / "ignored.txt").write_bytes(b"ignored")

    binding = _begin(store)

    assert [
        (file.path, file.before_sha256, file.after_sha256) for file in binding.identity.baseline_files
    ] == [("out/nested.txt", hashlib.sha256(b"before").hexdigest(), None)]


def test_begin_seeds_a_new_root_from_the_rejected_attempt(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    rejected = store.begin(task_id="rejected", attempt=1, output_paths=("out",))
    (rejected.write_root / "out" / "cases").mkdir(parents=True)
    (rejected.write_root / "out" / "cases" / "case.yaml").write_bytes(b"draft")
    (rejected.write_root / "out" / "cases" / "case.yaml").chmod(0o640)

    retry = store.begin(task_id="retry", attempt=1, output_paths=("out",), seed_task_id="rejected")

    assert (retry.write_root / "out" / "cases" / "case.yaml").read_bytes() == b"draft"
    assert retry.identity.seed is not None
    assert retry.identity.seed.source_identity_digest == rejected.identity.identity_digest
    assert [(file.path, file.sha256, file.mode) for file in retry.identity.seed.files] == [
        ("out/cases/case.yaml", hashlib.sha256(b"draft").hexdigest(), 0o640)
    ]
    assert [file.path for file in store.seal(retry.identity).files] == ["out/cases/case.yaml"]


def test_seed_copies_only_files_inside_the_new_claims(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    rejected = store.begin(task_id="rejected", attempt=1, output_paths=("kept", "dropped"))
    (rejected.write_root / "kept").write_bytes(b"kept")
    (rejected.write_root / "dropped").write_bytes(b"dropped")

    retry = store.begin(task_id="retry", attempt=1, output_paths=("kept",), seed_task_id="rejected")

    assert sorted(path.name for path in retry.write_root.iterdir()) == ["kept"]
    assert retry.identity.seed is not None
    assert [file.path for file in retry.identity.seed.files] == ["kept"]


def test_recovered_root_is_not_seeded_twice(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    rejected = store.begin(task_id="rejected", attempt=1, output_paths=("out.txt",))
    (rejected.write_root / "out.txt").write_bytes(b"draft")
    first = store.begin(task_id="retry", attempt=1, output_paths=("out.txt",), seed_task_id="rejected")
    (first.write_root / "out.txt").write_bytes(b"patched")

    recovered = store.begin(task_id="retry", attempt=1, output_paths=("out.txt",), seed_task_id="rejected")

    assert recovered.identity == first.identity
    assert (recovered.write_root / "out.txt").read_bytes() == b"patched"


def test_missing_seed_task_starts_an_empty_root(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)

    binding = store.begin(task_id="retry", attempt=1, output_paths=("out",), seed_task_id="never-opened")

    assert list(binding.write_root.iterdir()) == []
    assert binding.identity.seed is None
    assert "seed" not in binding.identity.model_dump(mode="json")


def test_replaced_seed_write_root_fails_closed(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    rejected = store.begin(task_id="rejected", attempt=1, output_paths=("out.txt",))
    retained = rejected.write_root.with_name("retained-original")
    rejected.write_root.rename(retained)
    rejected.write_root.mkdir()
    old_stat, new_stat = retained.stat(), rejected.write_root.stat()
    assert (old_stat.st_dev, old_stat.st_ino) != (new_stat.st_dev, new_stat.st_ino)
    (rejected.write_root / "out.txt").write_bytes(b"swapped")

    with pytest.raises(TaskWorkspaceViolation, match="seed write-root identity"):
        store.begin(task_id="retry", attempt=1, output_paths=("out.txt",), seed_task_id="rejected")


async def test_provider_seeds_from_the_previous_attempt_key(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    provider = TaskWorkspaceProvider(store)
    claims = ResourceClaims(writes=("out.txt",))
    first_key = AttemptKey(digest="a" * 64)
    rejected = await provider.open_or_create(first_key, claims)
    (rejected.write_root / "out.txt").write_bytes(b"draft")

    retry = await provider.open_or_create(AttemptKey(digest="b" * 64), claims, seed_from=first_key)

    assert (retry.write_root / "out.txt").read_bytes() == b"draft"


def test_seal_records_only_declared_regular_staged_files(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "created.txt").write_bytes(b"new")

    staged = store.seal(binding.identity)

    assert [(file.path, file.before_sha256, file.after_sha256) for file in staged.files] == [
        ("out/created.txt", None, hashlib.sha256(b"new").hexdigest())
    ]


def test_seal_rejects_undeclared_staged_file(tmp_path: Path) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    (binding.write_root / "not-claimed.txt").write_bytes(b"no")

    with pytest.raises(TaskWorkspaceViolation, match="write claim"):
        store.seal(binding.identity)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_seal_rejects_nonprivate_or_nonregular_staged_entries(tmp_path: Path, kind: str) -> None:
    store, _project, _attempts = _store(tmp_path)
    binding = _begin(store)
    target = binding.write_root / "out"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    elif kind == "hardlink":
        source = tmp_path / "source"
        source.write_bytes(b"same")
        os.link(source, target)
    else:
        os.mkfifo(target)

    with pytest.raises(TaskWorkspaceViolation):
        store.seal(binding.identity)


def test_promote_rejects_target_drift_since_begin(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out.txt").write_bytes(b"before")
    binding = _begin(store, claims=("out.txt",))
    (binding.write_root / "out.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)
    (project / "out.txt").write_bytes(b"drifted")

    with pytest.raises(TaskWorkspaceViolation, match="target drift"):
        store.promote(binding.identity, staged)


def test_promote_replaces_enumerated_files_and_writes_a_durable_receipt(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    (project / "out").mkdir()
    (project / "out" / "old.txt").write_bytes(b"old")
    (project / "out" / "unlisted.txt").write_bytes(b"keep")
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "old.txt").write_bytes(b"new")
    (binding.write_root / "out" / "new.txt").write_bytes(b"new file")

    receipt = store.promote(binding.identity, store.seal(binding.identity))

    assert (project / "out" / "old.txt").read_bytes() == b"new"
    assert (project / "out" / "new.txt").read_bytes() == b"new file"
    assert (project / "out" / "unlisted.txt").read_bytes() == b"keep"
    assert (store.receipts_root / f"{receipt.identity_digest}.json").is_file()


def test_identical_promotion_replay_is_idempotent_but_different_bytes_fail_closed(tmp_path: Path) -> None:
    store, project, _attempts = _store(tmp_path)
    binding = _begin(store, claims=("out.txt",))
    target = binding.write_root / "out.txt"
    target.write_bytes(b"first")
    staged = store.seal(binding.identity)
    receipt = store.promote(binding.identity, staged)

    assert store.promote(binding.identity, staged) == receipt
    target.write_bytes(b"different")
    different = store.seal(binding.identity)
    with pytest.raises(TaskWorkspaceViolation, match="receipt"):
        store.promote(binding.identity, different)
    assert (project / "out.txt").read_bytes() == b"first"


@pytest.mark.parametrize("mutation", ["staged", "target", "deleted-target"])
def test_completed_receipt_replay_reauthenticates_staged_and_target_state(
    tmp_path: Path, mutation: str
) -> None:
    store, project, _attempts = _store(tmp_path)
    binding = _begin(store, claims=("out.txt",))
    staged_file = binding.write_root / "out.txt"
    staged_file.write_bytes(b"first")
    staged = store.seal(binding.identity)
    store.promote(binding.identity, staged)
    if mutation == "staged":
        staged_file.write_bytes(b"changed")
    elif mutation == "target":
        (project / "out.txt").write_bytes(b"changed")
    else:
        (project / "out.txt").unlink()

    with pytest.raises(TaskWorkspaceViolation):
        store.promote(binding.identity, staged)


def test_ancestor_symlink_swap_cannot_redirect_descriptor_bound_target_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, _attempts = _store(tmp_path)
    target_parent = project / "out"
    target_parent.mkdir()
    (target_parent / "target.txt").write_bytes(b"before")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "target.txt"
    sentinel.write_bytes(b"outside")
    binding = _begin(store)
    (binding.write_root / "out").mkdir()
    (binding.write_root / "out" / "target.txt").write_bytes(b"after")
    staged = store.seal(binding.identity)

    real_replace = os.replace

    def swap_parent_then_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if target == "target.txt" and "dst_dir_fd" in kwargs:
            project_parent = project / "out"
            if project_parent.is_dir() and not project_parent.is_symlink():
                shutil.move(str(project_parent), str(project / "out-real"))
                project_parent.symlink_to(outside, target_is_directory=True)
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("graph_engine.attempts.workspace.os.replace", swap_parent_then_replace)

    with pytest.raises(TaskWorkspaceViolation):
        store.promote(binding.identity, staged)

    assert sentinel.read_bytes() == b"outside"
    assert (project / "out-real" / "target.txt").read_bytes() == b"after"


def test_ancestor_symlink_swap_cannot_redirect_baseline_or_staged_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "out").mkdir()
    (project / "out" / "baseline.txt").write_bytes(b"baseline")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    real_open = os.open

    def swap_baseline_parent(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "out" and kwargs.get("dir_fd") is not None:
            current = project / "out"
            if current.is_dir() and not current.is_symlink():
                shutil.move(str(current), str(project / "out-real"))
                current.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "open", swap_baseline_parent)

    with pytest.raises(TaskWorkspaceViolation):
        store.begin(task_id="baseline", attempt=1, output_paths=("out",))
    assert sentinel.read_bytes() == b"outside"

    monkeypatch.undo()
    binding = store.begin(task_id="staged", attempt=1, output_paths=("staged",))
    (binding.write_root / "staged").mkdir()
    (binding.write_root / "staged" / "file.txt").write_bytes(b"staged")
    staging_outside = tmp_path / "staging-outside"
    staging_outside.mkdir()
    staging_sentinel = staging_outside / "sentinel.txt"
    staging_sentinel.write_bytes(b"outside")

    def swap_staged_parent(name: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "staged" and kwargs.get("dir_fd") is not None:
            current = binding.write_root / "staged"
            if current.is_dir() and not current.is_symlink():
                shutil.move(str(current), str(binding.write_root / "staged-real"))
                current.symlink_to(staging_outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(task_workspace.os, "open", swap_staged_parent)
    with pytest.raises(TaskWorkspaceViolation):
        store.seal(binding.identity)
    assert staging_sentinel.read_bytes() == b"outside"


@pytest.fixture
def provider(tmp_path: Path) -> TaskWorkspaceProvider:
    store, _project, _attempts = _store(tmp_path)
    return TaskWorkspaceProvider(store)


@pytest.fixture
def binding(provider: TaskWorkspaceProvider) -> TaskWorkspaceBinding:
    opened = provider.store.begin(task_id="task/sealed-bytes", attempt=1, output_paths=("out",))
    (opened.write_root / "out").mkdir()
    (opened.write_root / "out" / "b.txt").write_bytes(b"bravo")
    (opened.write_root / "out" / "a.txt").write_bytes(b"alpha")
    return opened


async def test_seal_authenticates_complete_candidate_bytes(
    provider: TaskWorkspaceProvider, binding: TaskWorkspaceBinding
) -> None:
    all_staged_paths = ("out/a.txt", "out/b.txt")
    sealed = await provider.seal(binding)
    assert tuple(item.path for item in sealed.files) == tuple(sorted(all_staged_paths))
    assert all(sha256(item.content).hexdigest() == item.after_sha256 for item in sealed.files)

    def mutate_staged_file() -> None:
        (binding.write_root / "out" / "a.txt").write_bytes(b"mutated after seal")

    mutate_staged_file()
    with pytest.raises(TaskWorkspaceViolation, match="drifted after sealing"):
        await provider.prepare(binding, sealed)


async def test_promotion_consumes_durable_sealed_bytes_not_mutated_stage(
    provider: TaskWorkspaceProvider, binding: TaskWorkspaceBinding
) -> None:
    sealed = await provider.seal(binding)
    prepared = await provider.prepare(binding, sealed)

    def mutate_or_remove_live_stage() -> None:
        (binding.write_root / "out" / "a.txt").unlink()
        (binding.write_root / "out" / "b.txt").write_bytes(b"live stage mutated")

    mutate_or_remove_live_stage()
    receipt = await provider.promote(prepared)
    canonical_file = provider.store.project_root / sealed.files[0].path
    assert canonical_file.read_bytes() == sealed.files[0].content
    assert receipt.sealed_digest == sealed.sealed_digest


def test_claim_scan_excludes_bound_roots_by_path_identity_not_directory_name(tmp_path: Path) -> None:
    project = tmp_path / "project"
    claimed = project / "claimed"
    claimed.mkdir(parents=True)
    (claimed / "visible.txt").write_bytes(b"keep")
    decoy_runtime = claimed / ".runtime"
    decoy_runtime.mkdir()
    (decoy_runtime / "keep.txt").write_bytes(b"decoy")
    attempts = claimed / "attempts-root"
    attempts.mkdir()
    (attempts / "skip.txt").write_bytes(b"attempts")
    engine_root = claimed / "engine-root"
    receipts = engine_root / "receipts"
    receipts.mkdir(parents=True)
    (engine_root / "skip.txt").write_bytes(b"runtime")

    store = TaskWorkspaceStore(project, attempts, receipts)
    binding = store.begin(task_id="claim-skip", attempt=1, output_paths=("claimed",))

    assert {file.path for file in binding.identity.baseline_files} == {
        "claimed/visible.txt",
        "claimed/.runtime/keep.txt",
    }
