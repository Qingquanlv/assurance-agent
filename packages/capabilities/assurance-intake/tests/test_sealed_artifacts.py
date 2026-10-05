from __future__ import annotations

import hashlib
from typing import cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import ResourceClaims, SealedFile, SealedWriteSet, ValidationContext

from assurance_intake.validators import SealedArtifactRefsValidator


def _context(refs: list[dict[str, str]]) -> ValidationContext:
    return ValidationContext(
        invocation_id="test",
        task_id="test",
        graph_instance_id="test",
        node_id="intake.explore",
        resources=ResourceClaims(writes=("qa",)),
        task_output=cast(JSONValue, {"artifacts": refs}),
    )


def _sealed(content: bytes) -> SealedWriteSet:
    return SealedWriteSet(
        files=(
            SealedFile(
                path="qa/results/explore/exploration-draft.json",
                before_sha256=None,
                before_mode=None,
                after_sha256=hashlib.sha256(content).hexdigest(),
                after_mode=0o644,
                content=content,
            ),
        ),
        sealed_digest="test",
    )


def test_seal_rejects_mutation_after_finalize_captured_bytes() -> None:
    captured = b'{"change_id":"CH-001"}\n'
    refs = [
        {"path": "qa/results/explore/exploration-draft.json", "digest": hashlib.sha256(captured).hexdigest()}
    ]
    validator = SealedArtifactRefsValidator()

    assert validator.validate(_sealed(captured), _context(refs)).accepted
    rejected = validator.validate(_sealed(b'{"change_id":"CH-002"}\n'), _context(refs))
    assert not rejected.accepted
    assert "exploration-draft.json" in (rejected.reason or "")


def test_seal_rejects_unbound_staged_file() -> None:
    rejected = SealedArtifactRefsValidator().validate(_sealed(b"unbound"), _context([]))
    assert not rejected.accepted
    assert "unbound" in (rejected.reason or "")


def test_seal_rejects_missing_captured_output_file() -> None:
    refs = [{"path": "qa/results/explore/exploration-draft.json", "digest": "a" * 64}]
    rejected = SealedArtifactRefsValidator().validate(
        SealedWriteSet(files=(), sealed_digest="test"), _context(refs)
    )
    assert not rejected.accepted
    assert "missing from seal" in (rejected.reason or "")


def test_noop_repair_allows_authenticated_unchanged_baseline() -> None:
    digest = "a" * 64
    context = _context([{"path": "qa/cases/menus/case.yaml", "digest": digest}]).model_copy(
        update={
            "task_output": {
                "artifacts": [{"path": "qa/cases/menus/case.yaml", "digest": digest}],
                "review_repair": {"baseline_file_digests": {"qa/cases/menus/case.yaml": digest}},
            }
        }
    )
    accepted = SealedArtifactRefsValidator().validate(SealedWriteSet(files=(), sealed_digest="test"), context)
    assert accepted.accepted
