import os
from pathlib import Path

import pytest

from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)
from graph_engine.runtime.workspace import SnapshotStore, WorkspaceViolation


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
    with pytest.raises(WorkspaceViolation, match="tree id"):
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
    validators: list[tuple[str, object]] = []

    class AppendingValidator:
        def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
            calls.append("toy.first")
            validators.append(("not a valid id", RecordingValidator("unexpected", calls)))
            return ValidationResult(accepted=True)

    validators.append(("toy.first", AppendingValidator()))
    result = store.commit_candidate(  # type: ignore[arg-type]
        candidate, claims, validators, _context(claims)
    )
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
        def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
            candidate_path.chmod(0o600)
            candidate_path.write_bytes(b"tampered")
            return ValidationResult(accepted=True)

    with pytest.raises(WorkspaceViolation, match="tree id"):
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

    def fail_replace(_source: Path, _destination: Path) -> None:
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
