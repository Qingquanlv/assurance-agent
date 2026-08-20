import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    CommitValidator,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)
from graph_engine.runtime.workspace import (
    HeadPublicationIndeterminate,
    SnapshotStore,
    WorkspaceViolation,
)


def _context(resources: ResourceClaims) -> ValidationContext:
    return ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=resources,
    )


class RecordingValidator:
    def __init__(
        self,
        validator_id: str,
        calls: list[str],
        result: ValidationResult | Exception | None = None,
    ) -> None:
        self.validator_id = validator_id
        self.calls = calls
        self.result = result or ValidationResult(accepted=True)

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        assert candidate.candidate_tree_id
        assert context.invocation_id == "inv-1"
        self.calls.append(self.validator_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_failed_attempt_never_moves_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"seed.txt": b"old"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "seed.txt").write_text("new", encoding="utf-8")
    attempt.discard()
    assert store.head_tree_id() == before
    assert store.read_head("seed.txt") == b"old"


def test_undeclared_write_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "outside.txt").write_text("x", encoding="utf-8")
    candidate = attempt.seal()
    with pytest.raises(WorkspaceViolation, match="outside.txt"):
        store.commit_candidate(candidate, ResourceClaims(writes=("allowed",)))


def test_content_address_uses_sorted_relative_paths_and_file_hashes(tmp_path: Path) -> None:
    first = SnapshotStore.create(tmp_path / "first", {"nested/b.txt": b"b", "a.txt": b"a"})
    second = SnapshotStore.create(tmp_path / "second", {"a.txt": b"a", "nested/b.txt": b"b"})
    renamed = SnapshotStore.create(tmp_path / "renamed", {"nested/a.txt": b"b", "a.txt": b"a"})
    assert first.head_tree_id() == second.head_tree_id()
    assert first.head_tree_id() != renamed.head_tree_id()


@pytest.mark.parametrize(
    "path",
    ["", "/absolute", "../escape", "nested/../escape", "./file", "nested//file", "C:\\escape"],
)
def test_initial_snapshot_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(WorkspaceViolation, match="path"):
        SnapshotStore.create(tmp_path / "store", {path: b"x"})


def test_initial_snapshot_rejects_non_utf8_path(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceViolation, match="UTF-8"):
        SnapshotStore.create(tmp_path / "store", {"bad-\udcff": b"x"})


@pytest.mark.parametrize("attempt_id", ["", ".", "..", "../escape", "nested/id", "/absolute"])
def test_attempt_ids_cannot_escape_attempts_directory(tmp_path: Path, attempt_id: str) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    with pytest.raises(WorkspaceViolation, match="attempt id"):
        store.create_attempt(attempt_id)


def test_attempt_creation_rejects_collisions_and_symlink_aliases(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    store.create_attempt("attempt-1")
    with pytest.raises(WorkspaceViolation, match="already exists"):
        store.create_attempt("attempt-1")

    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "attempts" / "attempt-2").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolation, match="already exists"):
        store.create_attempt("attempt-2")


def test_discard_removes_only_the_exact_attempt_directory(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    first = store.create_attempt("attempt-1")
    second = store.create_attempt("attempt-2")
    marker = second.root / "keep.txt"
    marker.write_bytes(b"keep")
    first.discard()
    assert not first.root.exists()
    assert marker.read_bytes() == b"keep"


def test_discard_unlinks_replaced_attempt_symlink_without_touching_target(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    attempt.root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"keep")
    attempt.root.symlink_to(outside, target_is_directory=True)
    attempt.discard()
    assert not attempt.root.exists()
    assert marker.read_bytes() == b"keep"


def test_seal_returns_complete_sorted_baseline_diff(tmp_path: Path) -> None:
    store = SnapshotStore.create(
        tmp_path / "store",
        {"change.txt": b"old", "delete.txt": b"gone", "same.txt": b"same"},
    )
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "add.txt").write_bytes(b"added")
    (attempt.root / "change.txt").write_bytes(b"new")
    (attempt.root / "delete.txt").unlink()

    candidate = attempt.seal()

    assert [item.path for item in candidate.files] == ["add.txt", "change.txt", "delete.txt"]
    by_path = {item.path: item for item in candidate.files}
    assert by_path["add.txt"].before_sha256 is None
    assert by_path["add.txt"].after_sha256 is not None
    assert by_path["change.txt"].before_sha256 != by_path["change.txt"].after_sha256
    assert by_path["delete.txt"].before_sha256 is not None
    assert by_path["delete.txt"].after_sha256 is None


def test_sealed_tree_is_independent_of_later_attempt_changes(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    path = attempt.root / "file.txt"
    path.write_bytes(b"sealed")
    candidate = attempt.seal()
    sealed_root = store.root / "trees" / candidate.candidate_tree_id
    assert sealed_root.stat().st_mode & 0o222 == 0
    assert (sealed_root / "file.txt").stat().st_mode & 0o222 == 0
    path.write_bytes(b"changed later")

    store.commit_candidate(candidate, ResourceClaims(writes=("file.txt",)))
    assert store.read_head("file.txt") == b"sealed"


def test_seal_rejects_symlinks_and_special_files(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    symlink_attempt = store.create_attempt("symlink")
    (symlink_attempt.root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(WorkspaceViolation, match="symlink"):
        symlink_attempt.seal()

    fifo_attempt = store.create_attempt("fifo")
    os.mkfifo(fifo_attempt.root / "pipe")
    with pytest.raises(WorkspaceViolation, match="regular file"):
        fifo_attempt.seal()


def test_seal_rejects_hardlink_aliases(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    attempt = store.create_attempt("attempt-1")
    os.link(outside, attempt.root / "alias.txt")
    with pytest.raises(WorkspaceViolation, match="hard link"):
        attempt.seal()


def test_seal_rejects_non_utf8_filename(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    raw_root = os.fsencode(attempt.root)
    try:
        descriptor = os.open(raw_root + b"/bad-\xff", os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 names before workspace validation")
    os.close(descriptor)
    with pytest.raises(WorkspaceViolation, match="UTF-8"):
        attempt.seal()


def test_replaced_attempt_directory_cannot_be_sealed(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    attempt.root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.txt").write_bytes(b"escaped")
    attempt.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolation, match="attempt directory"):
        attempt.seal()


def test_write_prefix_matching_is_segment_aware(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "src2.txt").write_bytes(b"x")
    candidate = attempt.seal()
    with pytest.raises(WorkspaceViolation, match="src2.txt"):
        store.commit_candidate(candidate, ResourceClaims(writes=("src",)))


@pytest.mark.parametrize("claim_field", ["reads", "exclusive"])
def test_read_and_exclusive_only_claims_do_not_authorize_writes(tmp_path: Path, claim_field: str) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "claimed.txt").write_bytes(b"x")
    candidate = attempt.seal()
    claims = ResourceClaims.model_validate({claim_field: ("claimed.txt",)})
    with pytest.raises(WorkspaceViolation, match="claimed.txt"):
        store.commit_candidate(candidate, claims)


def test_deletion_also_requires_declared_write_coverage(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"protected.txt": b"old"})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "protected.txt").unlink()
    candidate = attempt.seal()
    with pytest.raises(WorkspaceViolation, match="protected.txt"):
        store.commit_candidate(candidate, ResourceClaims())


def test_stale_candidate_never_replaces_newer_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    first = store.create_attempt("first")
    second = store.create_attempt("second")
    (first.root / "value.txt").write_bytes(b"first")
    (second.root / "value.txt").write_bytes(b"second")
    first_candidate = first.seal()
    second_candidate = second.seal()
    claims = ResourceClaims(writes=("value.txt",))
    store.commit_candidate(first_candidate, claims)
    with pytest.raises(WorkspaceViolation, match="baseline"):
        store.commit_candidate(second_candidate, claims)
    assert store.read_head("value.txt") == b"first"


def test_missing_or_tampered_candidate_tree_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    tree_file = store.root / "trees" / candidate.candidate_tree_id / "value.txt"
    tree_file.chmod(0o600)
    tree_file.write_bytes(b"tampered")
    before = store.head_tree_id()
    with pytest.raises(WorkspaceViolation, match="tree id|writable"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_candidate_diff_must_match_tree_content(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    forged = candidate.model_copy(
        update={"files": (CandidateFile(path="other.txt", before_sha256=None, after_sha256="0" * 64),)}
    )
    with pytest.raises(WorkspaceViolation, match="diff"):
        store.commit_candidate(forged, ResourceClaims(writes=("other.txt",)))


def test_validators_run_in_declared_order_and_all_acceptance_moves_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    claims = ResourceClaims(writes=("value.txt",))
    calls: list[str] = []
    validators = (
        ("toy.first", RecordingValidator("toy.first", calls)),
        ("toy.second", RecordingValidator("toy.second", calls)),
    )

    result = store.commit_candidate(candidate, claims, validators, _context(claims))

    assert result.committed is True
    assert calls == ["toy.first", "toy.second"]
    assert [(receipt.validator_id, receipt.accepted) for receipt in result.receipts] == [
        ("toy.first", True),
        ("toy.second", True),
    ]
    assert store.read_head("value.txt") == b"candidate"


def test_selected_validators_are_snapshotted_before_execution(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    candidate = store.create_attempt("attempt-1").seal()
    claims = ResourceClaims()
    calls: list[str] = []
    validators: list[tuple[str, CommitValidator]] = []

    class AppendingValidator:
        def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
            assert candidate.candidate_tree_id
            assert context.invocation_id == "inv-1"
            calls.append("toy.first")
            validators.append(("not a valid id", RecordingValidator("unexpected", calls)))
            return ValidationResult(accepted=True)

    validators.append(("toy.first", AppendingValidator()))
    result = store.commit_candidate(candidate, claims, validators, _context(claims))
    assert result.committed is True
    assert calls == ["toy.first"]
    assert [receipt.validator_id for receipt in result.receipts] == ["toy.first"]


def test_validator_rejection_returns_named_receipt_without_moving_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    claims = ResourceClaims(writes=("value.txt",))
    calls: list[str] = []
    validators = (
        (
            "toy.reject",
            RecordingValidator("toy.reject", calls, ValidationResult(accepted=False, reason="policy denied")),
        ),
        ("toy.after", RecordingValidator("toy.after", calls)),
    )

    result = store.commit_candidate(candidate, claims, validators, _context(claims))

    assert result.committed is False
    assert calls == ["toy.reject", "toy.after"]
    assert result.receipts[0].model_dump() == {
        "validator_id": "toy.reject",
        "accepted": False,
        "reason": "policy denied",
    }
    assert store.head_tree_id() == before


def test_validator_exception_becomes_named_rejection_receipt(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    claims = ResourceClaims(writes=("value.txt",))
    validator = RecordingValidator("toy.explodes", [], RuntimeError("boom"))

    result = store.commit_candidate(candidate, claims, (("toy.explodes", validator),), _context(claims))

    assert result.committed is False
    assert result.receipts[0].validator_id == "toy.explodes"
    assert result.receipts[0].accepted is False
    assert result.receipts[0].reason == "validator raised RuntimeError: boom"


def test_validator_cannot_tamper_candidate_tree_before_head_publish(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    candidate_path = store.root / "trees" / candidate.candidate_tree_id / "value.txt"
    claims = ResourceClaims(writes=("value.txt",))

    class TamperingValidator:
        def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
            assert candidate.candidate_tree_id
            assert context.invocation_id == "inv-1"
            candidate_path.chmod(0o600)
            candidate_path.write_bytes(b"tampered")
            return ValidationResult(accepted=True)

    with pytest.raises(WorkspaceViolation, match="tree id|writable"):
        store.commit_candidate(
            candidate,
            claims,
            (("toy.tamper", TamperingValidator()),),
            _context(claims),
        )
    assert store.head_tree_id() == before


def test_validator_context_resources_must_match_commit_claims(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    candidate = store.create_attempt("attempt-1").seal()
    claims = ResourceClaims(writes=("value.txt",))
    validator = RecordingValidator("toy.accept", [], None)
    with pytest.raises(WorkspaceViolation, match="context resources"):
        store.commit_candidate(
            candidate,
            claims,
            (("toy.accept", validator),),
            _context(ResourceClaims(reads=("value.txt",))),
        )


def test_failed_atomic_head_replace_leaves_old_head_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()

    from graph_engine.runtime import workspace

    def fail_replace(_source: object, _destination: object, **_kwargs: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before
    assert store.read_head("value.txt") == b"base"


def test_corrupt_head_document_fails_closed(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    (store.root / "HEAD.json").write_text('{"tree_id":"missing"}', encoding="utf-8")
    with pytest.raises(WorkspaceViolation, match="HEAD"):
        store.head_tree_id()


def test_nested_source_directory_swap_cannot_escape_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    nested = attempt.root / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    displaced = attempt.root / "displaced"
    real_open = os.open
    swapped = False

    def swap_before_nested_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        if not swapped and path == "nested" and dir_fd is not None:
            swapped = True
            nested.rename(displaced)
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", swap_before_nested_open)
    with pytest.raises(WorkspaceViolation):
        attempt.seal()


def test_nested_destination_directory_swap_cannot_escape_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    nested = attempt.root / "nested"
    nested.mkdir()
    (nested / "file.txt").write_bytes(b"candidate")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir
    swapped = False

    def swap_destination_parent(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        nonlocal swapped
        real_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if not swapped and path == "nested" and dir_fd is not None:
            swapped = True
            os.rename(
                "nested",
                "displaced-destination",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.symlink(outside, "nested", target_is_directory=True, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_destination_parent)
    with pytest.raises(WorkspaceViolation):
        attempt.seal()
    assert not (outside / "file.txt").exists()


def test_post_verify_candidate_replacement_cannot_publish_unverified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    candidate_root = store.root / "trees" / candidate.candidate_tree_id
    displaced = store.root / "trees" / "displaced-candidate"
    real_replace = os.replace
    swapped = False

    def replace_after_candidate_swap(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped and Path(destination).name == "HEAD.json":
            swapped = True
            candidate_root.chmod(0o755)
            candidate_root.rename(displaced)
            shutil.copytree(displaced, candidate_root)
            candidate_file = candidate_root / "value.txt"
            candidate_file.chmod(0o600)
            candidate_file.write_bytes(b"unverified")
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_after_candidate_swap)
    with pytest.raises(WorkspaceViolation):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_post_verify_writable_mode_change_cannot_publish_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    candidate_file = store.root / "trees" / candidate.candidate_tree_id / "value.txt"
    real_replace = os.replace
    changed = False

    def change_mode_before_replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal changed
        if not changed and Path(destination).name == "HEAD.json":
            changed = True
            candidate_file.chmod(0o600)
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", change_mode_before_replace)
    with pytest.raises(WorkspaceViolation, match="writable"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_candidate_tree_durability_barrier_precedes_head_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "nested").mkdir()
    (attempt.root / "nested" / "value.txt").write_bytes(b"candidate")
    tree_stat = (store.root / "trees").stat()
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (tree_stat.st_dev, tree_stat.st_ino):
            events.append("trees-fsync")
        real_fsync(descriptor)

    def record_replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination).name == "HEAD.json":
            events.append("head-replace")
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    candidate = attempt.seal()
    store.commit_candidate(candidate, ResourceClaims(writes=("nested",)))
    assert "trees-fsync" in events
    assert events.index("trees-fsync") < events.index("head-replace")


def test_tree_directory_fsync_failure_prevents_candidate_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    tree_stat = (store.root / "trees").stat()
    real_fsync = os.fsync

    def fail_tree_barrier(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (tree_stat.st_dev, tree_stat.st_ino):
            raise OSError("simulated trees fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_tree_barrier)
    with pytest.raises(OSError, match="simulated trees fsync failure"):
        attempt.seal()
    assert store.head_tree_id() == before


def test_commit_rejects_replaced_lock_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    candidate = store.create_attempt("attempt-1").seal()
    lock_path = store.root.parent / store._lock_anchor_name
    displaced = store.root.parent / f"{store._lock_anchor_name}.displaced"
    real_open = os.open
    swapped = False

    def replace_lock_before_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        if not swapped and path == store._lock_anchor_name and dir_fd is not None:
            swapped = True
            lock_path.rename(displaced)
            lock_path.write_bytes(b"replacement")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", replace_lock_before_open)
    with pytest.raises(WorkspaceViolation, match="lock"):
        store.commit_candidate(candidate, ResourceClaims())


def test_commit_rejects_lock_symlink_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    candidate = store.create_attempt("attempt-1").seal()
    lock_path = store.root.parent / store._lock_anchor_name
    displaced = store.root.parent / f"{store._lock_anchor_name}.displaced"
    real_open = os.open
    swapped = False

    def symlink_lock_before_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        if not swapped and path == store._lock_anchor_name and dir_fd is not None:
            swapped = True
            lock_path.rename(displaced)
            lock_path.symlink_to(displaced)
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", symlink_lock_before_open)
    with pytest.raises(WorkspaceViolation, match="lock"):
        store.commit_candidate(candidate, ResourceClaims())


def test_ordinary_initialization_failure_removes_exact_new_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    with pytest.raises(WorkspaceViolation):
        SnapshotStore.create(root, {"../escape": b"x"})
    assert not root.exists()
    recovered = SnapshotStore.create(root, {"ok.txt": b"ok"})
    assert recovered.read_head("ok.txt") == b"ok"


def test_crash_incomplete_initialization_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    real_publish_head = SnapshotStore._publish_head
    crashed = False

    def crash_once(store: SnapshotStore, *args: Any, **kwargs: Any) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("simulated crash")
        real_publish_head(store, *args, **kwargs)

    monkeypatch.setattr(SnapshotStore, "_publish_head", crash_once)
    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        SnapshotStore.create(root, {"stale.txt": b"stale"})
    assert not root.exists()
    assert any(tmp_path.glob(".store.snapshot-init-*"))

    recovered = SnapshotStore.create(root, {"fresh.txt": b"fresh"})
    assert recovered.read_head("fresh.txt") == b"fresh"


def test_initialization_marker_never_resets_established_corrupt_store(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = SnapshotStore.create(root, {"original.txt": b"original"})
    (root / ".initializing").write_bytes(b"")
    (root / "HEAD.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(WorkspaceViolation, match="already exists"):
        SnapshotStore.create(root, {"replacement.txt": b"replacement"})
    assert (root / "trees").exists()
    with pytest.raises(WorkspaceViolation, match="HEAD"):
        store.head_tree_id()


def test_seal_rejects_file_added_after_directory_name_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "first.txt").write_bytes(b"first")
    attempt_stat = attempt.root.stat()
    real_listdir = os.listdir
    injected = False

    def add_after_snapshot(path: Any) -> list[str]:
        nonlocal injected
        names = real_listdir(path)
        if not injected and isinstance(path, int):
            opened = os.fstat(path)
            if (opened.st_dev, opened.st_ino) == (attempt_stat.st_dev, attempt_stat.st_ino):
                injected = True
                (attempt.root / "added.txt").write_bytes(b"added")
        return names

    monkeypatch.setattr(os, "listdir", add_after_snapshot)
    with pytest.raises(WorkspaceViolation, match="name set|changed"):
        attempt.seal()


def test_seal_rejects_processed_file_removed_before_scan_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    path = attempt.root / "first.txt"
    path.write_bytes(b"first")
    from graph_engine.runtime import workspace

    real_assert = workspace._assert_entry_identity
    removed = False

    def remove_after_entry_check(parent_fd: int, name: str, expected: os.stat_result, kind: str) -> None:
        nonlocal removed
        real_assert(parent_fd, name, expected, kind)
        if not removed and name == "first.txt" and kind == "source file":
            removed = True
            path.unlink()

    monkeypatch.setattr(workspace, "_assert_entry_identity", remove_after_entry_check)
    with pytest.raises(WorkspaceViolation, match="name set|changed"):
        attempt.seal()


def test_seal_rejects_processed_file_replaced_under_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    attempt = store.create_attempt("attempt-1")
    path = attempt.root / "first.txt"
    path.write_bytes(b"first")
    from graph_engine.runtime import workspace

    real_assert = workspace._assert_entry_identity
    replaced = False

    def replace_after_entry_check(parent_fd: int, name: str, expected: os.stat_result, kind: str) -> None:
        nonlocal replaced
        real_assert(parent_fd, name, expected, kind)
        if not replaced and name == "first.txt" and kind == "source file":
            replaced = True
            path.unlink()
            path.write_bytes(b"replacement")

    monkeypatch.setattr(workspace, "_assert_entry_identity", replace_after_entry_check)
    with pytest.raises(WorkspaceViolation, match="entry state|changed"):
        attempt.seal()


def test_read_head_rejects_path_added_after_manifest_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    from graph_engine.runtime import workspace

    real_head_tree = workspace.SnapshotStore._head_tree

    def add_after_authentication(self: SnapshotStore, root_fd: int, trees_fd: int):
        document, tree = real_head_tree(self, root_fd, trees_fd)
        tree_root = self.root / "trees" / document["tree_id"]
        tree_root.chmod(0o755)
        added = tree_root / "added.txt"
        added.write_bytes(b"unauthenticated")
        added.chmod(0o444)
        tree_root.chmod(0o555)
        return document, tree

    monkeypatch.setattr(workspace.SnapshotStore, "_head_tree", add_after_authentication)
    with pytest.raises(WorkspaceViolation, match="manifest|authenticated"):
        store.read_head("added.txt")


def test_read_head_rejects_rewritten_bytes_after_manifest_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"authenticated"})
    from graph_engine.runtime import workspace

    real_head_tree = workspace.SnapshotStore._head_tree

    def rewrite_after_authentication(self: SnapshotStore, root_fd: int, trees_fd: int):
        document, tree = real_head_tree(self, root_fd, trees_fd)
        path = self.root / "trees" / document["tree_id"] / "value.txt"
        path.chmod(0o600)
        path.write_bytes(b"rewritten")
        path.chmod(0o444)
        return document, tree

    monkeypatch.setattr(workspace.SnapshotStore, "_head_tree", rewrite_after_authentication)
    with pytest.raises(WorkspaceViolation, match="hash|authenticated"):
        store.read_head("value.txt")


def test_read_head_rejects_mixed_bytes_from_concurrent_in_place_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.runtime import workspace

    content = b"a" * (workspace._COPY_BUFFER_SIZE * 2)
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": content})
    tree_id = store.head_tree_id()
    path = store.root / "trees" / tree_id / "value.txt"
    target_identity = (path.stat().st_dev, path.stat().st_ino)
    real_head_tree = workspace.SnapshotStore._head_tree
    real_read = os.read
    armed = False
    mutated = False

    def arm_after_authentication(self: SnapshotStore, root_fd: int, trees_fd: int):
        nonlocal armed
        result = real_head_tree(self, root_fd, trees_fd)
        armed = True
        return result

    def rewrite_after_first_chunk(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        current = os.fstat(descriptor)
        if armed and not mutated and (current.st_dev, current.st_ino) == target_identity and chunk:
            mutated = True
            path.chmod(0o600)
            with path.open("r+b") as writable:
                writable.seek(len(chunk))
                writable.write(b"b" * (len(content) - len(chunk)))
            path.chmod(0o444)
        return chunk

    monkeypatch.setattr(workspace.SnapshotStore, "_head_tree", arm_after_authentication)
    monkeypatch.setattr(os, "read", rewrite_after_first_chunk)
    with pytest.raises(WorkspaceViolation, match="hash|authenticated"):
        store.read_head("value.txt")


def test_root_fsync_failure_after_head_replace_rolls_back_visible_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    root_stat = store.root.stat()
    real_fsync = os.fsync
    failed = False

    def fail_once_after_replace(descriptor: int) -> None:
        nonlocal failed
        current = os.fstat(descriptor)
        if not failed and (current.st_dev, current.st_ino) == (root_stat.st_dev, root_stat.st_ino):
            failed = True
            raise OSError("post-replace root fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once_after_replace)
    with pytest.raises(OSError, match="post-replace"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_raw_post_replace_scan_error_rolls_back_visible_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.runtime import workspace

    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    real_replace = os.replace
    real_scan = workspace._scan_directory_fd
    replaced = False
    failed = False

    def track_replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal replaced
        real_replace(source, destination, *args, **kwargs)
        if destination == "HEAD.json":
            replaced = True

    def fail_post_replace_scan(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal failed
        if replaced and not failed:
            failed = True
            raise FileNotFoundError("simulated post-replace entry removal")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(os, "replace", track_replace)
    monkeypatch.setattr(workspace, "_scan_directory_fd", fail_post_replace_scan)
    with pytest.raises(FileNotFoundError, match="entry removal"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_replace_error_after_visible_head_move_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    before = store.head_tree_id()
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    real_replace = os.replace
    failed = False

    def move_then_fail(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal failed
        real_replace(source, destination, *args, **kwargs)
        if not failed and destination == "HEAD.json":
            failed = True
            raise OSError("replace result unavailable")

    monkeypatch.setattr(os, "replace", move_then_fail)
    with pytest.raises(OSError, match="result unavailable"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))
    assert store.head_tree_id() == before


def test_failed_rollback_raises_dedicated_indeterminate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    attempt = store.create_attempt("attempt-1")
    (attempt.root / "value.txt").write_bytes(b"candidate")
    candidate = attempt.seal()
    root_stat = store.root.stat()
    real_fsync = os.fsync

    def fail_every_root_barrier(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (root_stat.st_dev, root_stat.st_ino):
            raise OSError("root barrier unavailable")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_every_root_barrier)
    with pytest.raises(HeadPublicationIndeterminate, match="indeterminate"):
        store.commit_candidate(candidate, ResourceClaims(writes=("value.txt",)))


def test_failed_new_head_transaction_preserves_prior_recovery_journal(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"value.txt": b"base"})
    authoritative = store.head_tree_id()
    first_attempt = store.create_attempt("first")
    (first_attempt.root / "value.txt").write_bytes(b"first candidate")
    first_candidate = first_attempt.seal()

    def leave_recovery_journal(_previous_tree_id: str, _tree_id: str) -> None:
        raise HeadPublicationIndeterminate("ledger outcome unavailable")

    with pytest.raises(HeadPublicationIndeterminate, match="unavailable"):
        store.finalize_candidate(
            first_candidate,
            ResourceClaims(writes=("value.txt",)),
            authorize_publish=lambda: None,
            publish_success=leave_recovery_journal,
        )
    assert store.head_tree_id() == first_candidate.candidate_tree_id

    second_attempt = store.create_attempt("second")
    (second_attempt.root / "value.txt").write_bytes(b"second candidate")
    second_candidate = second_attempt.seal()
    with pytest.raises(WorkspaceViolation, match="unfinished HEAD transaction"):
        store.finalize_candidate(
            second_candidate,
            ResourceClaims(writes=("value.txt",)),
            authorize_publish=lambda: None,
            publish_success=lambda _previous, _tree: None,
        )

    store.recover_head_transaction(authoritative)

    assert store.head_tree_id() == authoritative
    assert store.read_head("value.txt") == b"base"


def test_paired_layout_and_lock_replacement_cannot_change_lock_domain(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    candidate = store.create_attempt("attempt-1").seal()
    lock = store.root / ".commit.lock"
    layout = store.root / ".layout.json"
    if lock.exists():
        lock.chmod(0o600)
    lock.write_bytes(b"\0")
    lock.chmod(0o400)
    lock_stat = lock.stat()
    payload: dict[str, Any] = {
        "version": 1,
        "lock_dev": lock_stat.st_dev,
        "lock_ino": lock_stat.st_ino,
    }
    document = {**payload, "digest": canonical_digest(payload)}
    layout.chmod(0o600)
    layout.write_bytes(canonical_json_bytes(document))
    layout.chmod(0o400)
    with pytest.raises(WorkspaceViolation, match="anchor|lock"):
        store.commit_candidate(candidate, ResourceClaims())


def test_create_never_deletes_unrelated_marker_directory(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    marker = root / ".initializing"
    marker.write_bytes(b"")
    marker.chmod(0o400)
    unrelated = root / "unrelated.txt"
    unrelated.write_bytes(b"keep")
    with pytest.raises(WorkspaceViolation, match="already exists"):
        SnapshotStore.create(root, {})
    assert unrelated.read_bytes() == b"keep"


def test_create_never_deletes_established_store_with_missing_head(tmp_path: Path) -> None:
    root = tmp_path / "store"
    SnapshotStore.create(root, {"original.txt": b"original"})
    (root / "HEAD.json").unlink()
    marker = root / ".initializing"
    marker.write_bytes(b"")
    marker.chmod(0o400)
    with pytest.raises(WorkspaceViolation, match="already exists"):
        SnapshotStore.create(root, {"replacement.txt": b"replacement"})
    assert any((root / "trees").iterdir())


def test_initialization_fsyncs_parent_before_and_after_atomic_root_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    parent_stat = tmp_path.stat()
    events: list[str] = []
    real_fsync = os.fsync
    real_rename = os.rename

    def record_fsync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (parent_stat.st_dev, parent_stat.st_ino):
            events.append("parent-fsync")
        elif stat.S_ISDIR(current.st_mode):
            names = set(os.listdir(descriptor))
            if {"trees", "attempts", ".layout.json"}.issubset(names):
                events.append("root-fsync")
        real_fsync(descriptor)

    def record_rename(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if destination == root.name and str(source).startswith(f".{root.name}.snapshot-init-"):
            events.append("root-install")
        real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "rename", record_rename)
    SnapshotStore.create(root, {})
    install = events.index("root-install")
    assert "parent-fsync" in events[:install]
    assert "root-fsync" in events[:install]
    assert "parent-fsync" in events[install + 1 :]


def test_parent_fsync_failure_after_root_install_leaves_complete_reopenable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    parent_stat = tmp_path.stat()
    installed = False
    failed = False
    real_fsync = os.fsync
    real_rename = os.rename

    def track_install(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal installed
        real_rename(source, destination, *args, **kwargs)
        if destination == root.name:
            installed = True

    def fail_post_install_parent_barrier(descriptor: int) -> None:
        nonlocal failed
        current = os.fstat(descriptor)
        identity = current.st_dev, current.st_ino
        if installed and not failed and identity == (parent_stat.st_dev, parent_stat.st_ino):
            failed = True
            raise OSError("post-install parent barrier unavailable")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "rename", track_install)
    monkeypatch.setattr(os, "fsync", fail_post_install_parent_barrier)
    with pytest.raises(OSError, match="post-install"):
        SnapshotStore.create(root, {"value.txt": b"complete"})
    assert SnapshotStore(root).read_head("value.txt") == b"complete"
    with pytest.raises(WorkspaceViolation, match="already exists"):
        SnapshotStore.create(root, {})
