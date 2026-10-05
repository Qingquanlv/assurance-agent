from __future__ import annotations

import hashlib
from typing import cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import ResourceClaims, SealedFile, SealedWriteSet, ValidationContext

from assurance_intake.contracts.coverage_rework import REWORK_CONTEXT_PATH
from assurance_intake.contracts.plan import PREPARATION_REFS_PATH
from assurance_intake.validators import SealedArtifactRefsValidator

_HEX = "a" * 64


def _context(
    refs: list[dict[str, str]] | None = None,
    *,
    output: dict[str, object] | None = None,
    task_input: dict[str, object] | None = None,
) -> ValidationContext:
    return ValidationContext(
        invocation_id="test",
        task_id="test",
        graph_instance_id="test",
        node_id="intake.explore",
        resources=ResourceClaims(writes=("qa",)),
        task_output=cast(JSONValue, output if output is not None else {"artifacts": refs or []}),
        task_input=cast(JSONValue, task_input or {}),
    )


def _sealed(*items: tuple[str, bytes]) -> SealedWriteSet:
    files = tuple(
        SealedFile(
            path=path,
            before_sha256=None,
            before_mode=None,
            after_sha256=hashlib.sha256(content).hexdigest(),
            after_mode=0o644,
            content=content,
        )
        for path, content in items
    )
    return SealedWriteSet(files=files, sealed_digest="test")


def _draft(content: bytes) -> SealedWriteSet:
    return _sealed(("qa/results/explore/exploration-draft.json", content))


def test_seal_rejects_mutation_after_finalize_captured_bytes() -> None:
    captured = b'{"change_id":"CH-001"}\n'
    refs = [
        {"path": "qa/results/explore/exploration-draft.json", "digest": hashlib.sha256(captured).hexdigest()}
    ]
    validator = SealedArtifactRefsValidator()

    assert validator.validate(_draft(captured), _context(refs)).accepted
    rejected = validator.validate(_draft(b'{"change_id":"CH-002"}\n'), _context(refs))
    assert not rejected.accepted
    assert "exploration-draft.json" in (rejected.reason or "")


def test_seal_rejects_unbound_staged_file() -> None:
    rejected = SealedArtifactRefsValidator().validate(_draft(b"unbound"), _context([]))
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


def test_seal_binds_preparation_refs_document() -> None:
    content = b'{"schema_version":"1"}\n'
    digest = hashlib.sha256(content).hexdigest()
    accepted = SealedArtifactRefsValidator().validate(
        _sealed((PREPARATION_REFS_PATH, content)),
        _context(
            output={
                "preparation_refs_ref": {"path": PREPARATION_REFS_PATH, "digest": digest},
                "preparation_refs": [{"path": "qa/.qa.yaml", "digest": _HEX}],
            }
        ),
    )
    assert accepted.accepted


def test_seal_binds_rework_ref() -> None:
    content = b'{"schema_version":"1"}\n'
    digest = hashlib.sha256(content).hexdigest()
    accepted = SealedArtifactRefsValidator().validate(
        _sealed((REWORK_CONTEXT_PATH, content)),
        _context(output={"rework_ref": {"path": REWORK_CONTEXT_PATH, "digest": digest}}),
    )
    assert accepted.accepted


def test_seal_rejects_unbound_refs_document() -> None:
    rejected = SealedArtifactRefsValidator().validate(
        _sealed((PREPARATION_REFS_PATH, b"unbound")),
        _context(output={"rework_ref": {"path": REWORK_CONTEXT_PATH, "digest": _HEX}}),
    )
    assert not rejected.accepted
    assert "unbound Intake staged file" in (rejected.reason or "")
    assert PREPARATION_REFS_PATH in (rejected.reason or "")


def test_seal_rejects_rework_ref_missing_from_seal() -> None:
    rejected = SealedArtifactRefsValidator().validate(
        SealedWriteSet(files=(), sealed_digest="test"),
        _context(output={"rework_ref": {"path": REWORK_CONTEXT_PATH, "digest": _HEX}}),
    )
    assert not rejected.accepted
    assert "missing from seal" in (rejected.reason or "")
    assert REWORK_CONTEXT_PATH in (rejected.reason or "")


def test_seal_does_not_require_foreign_preparation_refs() -> None:
    content = b'{"schema_version":"1"}\n'
    digest = hashlib.sha256(content).hexdigest()
    accepted = SealedArtifactRefsValidator().validate(
        _sealed((PREPARATION_REFS_PATH, content)),
        _context(
            output={
                "preparation_refs_ref": {"path": PREPARATION_REFS_PATH, "digest": digest},
                "preparation_refs": [
                    {"path": "qa/.qa.yaml", "digest": _HEX},
                    {"path": "qa/results/explore/exploration.json", "digest": _HEX},
                ],
            }
        ),
    )
    assert accepted.accepted


def test_seal_ignores_preparation_refs_as_output_bindings() -> None:
    rejected = SealedArtifactRefsValidator().validate(
        SealedWriteSet(files=(), sealed_digest="test"),
        _context(
            output={
                "preparation_refs": [{"path": "qa/.qa.yaml", "digest": _HEX}],
            }
        ),
    )
    assert not rejected.accepted
    assert rejected.reason == "Intake output has no artifact refs"
