"""Pinned projection digest / canonical JSON / TraceSourceRecorder contracts."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes as artifacts_canonical_json_bytes
from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.issues import IssueEvidenceManifestEntry
from assurance_agent.artifacts.models.trace import (
    TraceGap,
    TraceProjection,
    TraceRow,
    TraceSource,
)
from assurance_agent.evidence import digests as digests_mod
from assurance_agent.evidence.digests import (
    EVIDENCE_ENTRY_DIGEST_SEMANTICS,
    EvidenceEntryPathError,
    TraceSourceConflictError,
    TraceSourceRecorder,
    canonical_json_bytes,
    evidence_bundle_digest_v1,
    evidence_entry_digest_v1,
    normalize_evidence_entry_path,
    projection_digest,
    raw_sha256,
    read_evidence_entry_v1,
    redact_evidence_text_v1,
)
from assurance_agent.evidence.trace import canonical_json_bytes as trace_canonical_json_bytes

# Captured from collector `_sha256_path` / `_fmt_digest` before extraction (literal goldens).
ENTRY_DIGEST_GOLDENS = {
    "secret": "sha256:2098d5edf597449cb5012104a48aabeac4d62db5cff0881fd1461f95f1d2fe49",
    "utf8": "sha256:ff3646740af56fe2b164917ad2917d64ee61394cd1142704af84ecd31dec8e25",
    "binary": "sha256:ef192b7af54e943f206ab27075ec1805384c972c9959fc5820f1fa7d5268fcef",
}
_SECRET_BYTES = b'{"access_token":"abcdefghijklmnop"}'
_SECRET_ALT_BYTES = b'{"access_token":"XXXXXXXXXXXXYYYY"}'
_SECRET_RAW_GOLDEN = "86ac991725e9d26393cf100b1c9f1872cbbc461b3ae7f2fa22470d3724d4bdff"
_SECRET_ALT_RAW_GOLDEN = "36b414c414cc33545a2b7277584a7d0640ba297fed235ad48d4ac7b369a67ed8"

# Compact sorted JSON, ensure_ascii=True (non-ASCII escaped), no trailing newline.
_CANONICAL_GOLDEN = (
    b'{"a":"\\u4e2d\\u6587","targets":{"api":true,"e2e":false,"fuzz":false,'
    b'"performance":true},"ts":"2026-07-29T12:00:00+00:00","z":1}'
)

_PINNED_PROJECTION_DIGEST = "1a7dffe527a81c67aa1f418d01b5afac0a08822be4b33947bfaa31c8bd0a7547"


def _sample_payload() -> dict[str, object]:
    return {
        "z": 1,
        "a": "中文",
        "ts": datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC),
        "targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=True),
    }


def _minimal_projection() -> TraceProjection:
    return TraceProjection(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="B1",
        sources=(TraceSource(path="cases/a/case.yaml", exists=True, sha256="aa"),),
        rows=(
            TraceRow(
                case_id="API-1",
                module="m",
                case_type="API",
                automation_required=True,
                coverage_state="covered",
                covering_tests=(),
                presence_in_current_batch="executed",
            ),
        ),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )


def test_canonical_json_bytes_pins_legacy_trace_contract() -> None:
    raw = canonical_json_bytes(_sample_payload())
    assert raw == _CANONICAL_GOLDEN
    assert not raw.endswith(b"\n")
    assert b"\\u4e2d\\u6587" in raw
    assert b"\xe4\xb8\xad" not in raw  # not UTF-8 raw for 中


def test_trace_reexports_same_canonical_bytes() -> None:
    assert trace_canonical_json_bytes(_sample_payload()) == _CANONICAL_GOLDEN


def test_artifacts_canonical_differs_from_trace_digest_contract() -> None:
    """Guard against accidental substitution of artifacts.canonical."""
    # artifacts.canonical accepts JSON-ready values and uses ensure_ascii=False + trailing newline.
    json_ready = {
        "a": "中文",
        "targets": {"api": True, "e2e": False, "fuzz": False, "performance": True},
        "ts": "2026-07-29T12:00:00+00:00",
        "z": 1,
    }
    artifacts_bytes = artifacts_canonical_json_bytes(json_ready)
    assert artifacts_bytes != _CANONICAL_GOLDEN
    assert artifacts_bytes.endswith(b"\n")
    assert "中文".encode() in artifacts_bytes


def test_projection_digest_pins_existing_unprefixed_hex() -> None:
    digest = projection_digest(_minimal_projection())
    assert digest == _PINNED_PROJECTION_DIGEST
    assert digest == digest.lower()
    assert not digest.startswith("sha256:")


def test_source_recorder_idempotent_and_sorted() -> None:
    recorder = TraceSourceRecorder()
    first = TraceSource(path="b.yaml", exists=True, sha256="bb")
    second = TraceSource(path="a.yaml", exists=False, sha256=None)
    recorder.add(first)
    recorder.add(second)
    recorder.add(first)  # identical re-add is idempotent
    assert recorder.freeze() == (second, first)


def test_source_recorder_rejects_conflicting_facts() -> None:
    recorder = TraceSourceRecorder()
    recorder.add(TraceSource(path="a.yaml", exists=True, sha256="aa"))
    with pytest.raises(TraceSourceConflictError, match="a.yaml"):
        recorder.add(TraceSource(path="a.yaml", exists=True, sha256="bb"))


def test_projection_digest_stable_under_gap_order_irrelevant_to_payload() -> None:
    """Digest follows model_dump order of the projection as constructed."""
    base = _minimal_projection()
    with_gap = base.model_copy(
        update={
            "gaps": (TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),),
            "integrity": "incomplete",
        }
    )
    assert projection_digest(with_gap) != projection_digest(base)


@pytest.mark.parametrize(
    ("content", "expected_digest"),
    [
        (b'{"access_token":"abcdefghijklmnop"}', ENTRY_DIGEST_GOLDENS["secret"]),
        ("普通文本".encode(), ENTRY_DIGEST_GOLDENS["utf8"]),
        (b"\xff\x00\x80", ENTRY_DIGEST_GOLDENS["binary"]),
    ],
)
def test_entry_digest_v1_matches_collector_semantics(
    content: bytes,
    expected_digest: str,
) -> None:
    assert evidence_entry_digest_v1(content) == expected_digest
    assert expected_digest.startswith("sha256:")
    assert EVIDENCE_ENTRY_DIGEST_SEMANTICS == "evidence_entry_digest/v1"


def test_entry_digest_changes_when_visible_text_or_binary_changes() -> None:
    assert evidence_entry_digest_v1(b"hello") != evidence_entry_digest_v1(b"hallo")
    assert evidence_entry_digest_v1(b"\xff\x00") != evidence_entry_digest_v1(b"\xff\x01")


def test_secret_value_change_preserves_entry_digest_but_changes_raw_sha256() -> None:
    assert evidence_entry_digest_v1(_SECRET_BYTES) == ENTRY_DIGEST_GOLDENS["secret"]
    assert evidence_entry_digest_v1(_SECRET_ALT_BYTES) == ENTRY_DIGEST_GOLDENS["secret"]
    assert raw_sha256(_SECRET_BYTES) == _SECRET_RAW_GOLDEN
    assert raw_sha256(_SECRET_ALT_BYTES) == _SECRET_ALT_RAW_GOLDEN
    assert raw_sha256(_SECRET_BYTES) != raw_sha256(_SECRET_ALT_BYTES)
    assert not raw_sha256(_SECRET_BYTES).startswith("sha256:")


def test_bundle_digest_v1_is_canonical_path_order() -> None:
    a = IssueEvidenceManifestEntry(path="execution/b.json", digest="sha256:bb")
    b = IssueEvidenceManifestEntry(path="execution/a.json", digest="sha256:aa")
    forward = evidence_bundle_digest_v1([a, b])
    reverse = evidence_bundle_digest_v1([b, a])
    assert forward == reverse
    assert forward.startswith("sha256:")
    canonical = (
        '[{"digest":"sha256:aa","path":"execution/a.json"},{"digest":"sha256:bb","path":"execution/b.json"}]'
    ).encode("utf-8")
    assert forward == f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def test_redaction_pattern_mutation_changes_pinned_v1_golden() -> None:
    """Removing/reordering/changing a pattern or replacement must move at least one golden."""
    secret_text = _SECRET_BYTES.decode("utf-8")
    assert redact_evidence_text_v1(secret_text) != secret_text

    # Remove access_token pattern → secret remains visible → digest moves.
    patterns_without_token = tuple(p for p in digests_mod._REDACT_PATTERNS if "access" not in p.pattern)
    text = secret_text
    for pat in patterns_without_token:
        text = pat.sub(digests_mod._REDACTED, text)
    removed = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    assert removed != ENTRY_DIGEST_GOLDENS["secret"]

    # Change replacement literal → digest moves.
    replaced = secret_text
    for pat in digests_mod._REDACT_PATTERNS:
        replaced = pat.sub("XX", replaced)
    assert f"sha256:{hashlib.sha256(replaced.encode('utf-8')).hexdigest()}" != ENTRY_DIGEST_GOLDENS["secret"]

    # Reverse pattern order on overlapping corpus → digest moves vs v1 order.
    overlapping = "Bearer sk-abcdefghijklmnopXXXX"
    v1_over = redact_evidence_text_v1(overlapping)
    rev_over = overlapping
    for pat in reversed(digests_mod._REDACT_PATTERNS):
        rev_over = pat.sub(digests_mod._REDACTED, rev_over)
    assert v1_over != rev_over
    assert evidence_entry_digest_v1(overlapping.encode()) != (
        f"sha256:{hashlib.sha256(rev_over.encode('utf-8')).hexdigest()}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/result.json",
        "../result.json",
        "execution/../../secret",
        "unknown/result.json",
        "execution\\result.json",
        "",
        ".",
        "execution",
        "execution/",
        "execution/./result.json",
        "execution//result.json",
        "execution/foo/../bar.json",
        "execution/.",
    ],
)
def test_normalize_evidence_entry_path_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(EvidenceEntryPathError):
        normalize_evidence_entry_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "execution/result.json",
        "cases/a/case.yaml",
        "facts/baseline.json",
        "review/api-plan-review.json",
        "healing/api-apply-summary.json",
        "codegen/plan.md",
    ],
)
def test_normalize_evidence_entry_path_accepts_allowlisted_prefixes(path: str) -> None:
    assert normalize_evidence_entry_path(path) == path


def test_read_evidence_entry_v1_opens_once_and_feeds_both_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = tmp_path / "CH-1"
    rel = "execution/runs/b/result.json"
    target = change_dir / rel
    target.parent.mkdir(parents=True)
    payload = _SECRET_BYTES
    target.write_bytes(payload)

    leaf_opens = 0
    real_open = os.open

    def counting_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
        **kwargs: object,
    ) -> int:
        nonlocal leaf_opens
        fd = real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if not (flags & os.O_DIRECTORY) and Path(os.fsdecode(path)).name == "result.json":
            leaf_opens += 1
        return fd

    monkeypatch.setattr(os, "open", counting_open)
    entry = read_evidence_entry_v1(change_dir, rel)
    assert leaf_opens == 1
    assert entry.path == rel
    assert entry.data == payload
    assert entry.entry_digest == evidence_entry_digest_v1(payload)
    assert entry.entry_digest == ENTRY_DIGEST_GOLDENS["secret"]
    assert entry.raw_sha256 == raw_sha256(payload)
    assert entry.raw_sha256 == _SECRET_RAW_GOLDEN


def test_read_rejects_symlinked_intermediate_directory(tmp_path: Path) -> None:
    change_dir = tmp_path / "CH-1"
    real_dir = tmp_path / "outside-dir"
    real_dir.mkdir()
    (real_dir / "result.json").write_text("x", encoding="utf-8")
    exec_dir = change_dir / "execution"
    exec_dir.mkdir(parents=True)
    os.symlink(real_dir, exec_dir / "runs")
    with pytest.raises(EvidenceEntryPathError):
        read_evidence_entry_v1(change_dir, "execution/runs/result.json")


def test_read_rejects_symlinked_final_file(tmp_path: Path) -> None:
    change_dir = tmp_path / "CH-1"
    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    target = change_dir / "execution" / "result.json"
    target.parent.mkdir(parents=True)
    os.symlink(outside, target)
    with pytest.raises(EvidenceEntryPathError):
        read_evidence_entry_v1(change_dir, "execution/result.json")


def test_read_rejects_non_regular_file(tmp_path: Path) -> None:
    change_dir = tmp_path / "CH-1"
    directory = change_dir / "execution" / "not-a-file"
    directory.mkdir(parents=True)
    with pytest.raises(EvidenceEntryPathError):
        read_evidence_entry_v1(change_dir, "execution/not-a-file")


def test_read_fails_closed_when_component_swapped_to_symlink_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = tmp_path / "CH-1"
    rel = "execution/result.json"
    target = change_dir / rel
    target.parent.mkdir(parents=True)
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside-secret"
    outside.write_text("SECRET", encoding="utf-8")
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

    def guarded_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
        **kwargs: object,
    ) -> int:
        nonlocal opened_outside
        fd = real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if _fd_is_outside(fd):
            opened_outside = True
        return fd

    def guarded_read(fd: int, n: int, /) -> bytes:
        nonlocal read_outside
        if _fd_is_outside(fd):
            read_outside = True
        return real_read(fd, n)

    def _race(_change_dir: Path, _path: str) -> None:
        target.unlink()
        os.symlink(outside, target)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "read", guarded_read)
    monkeypatch.setattr(digests_mod, "_after_evidence_entry_normalized_hook", _race)

    with pytest.raises(EvidenceEntryPathError):
        read_evidence_entry_v1(change_dir, rel)
    assert opened_outside is False
    assert read_outside is False
    assert outside.read_text(encoding="utf-8") == "SECRET"
    assert stat.S_ISLNK(os.lstat(target).st_mode)
