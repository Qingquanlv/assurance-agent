"""Immutable Improvement Review Subject invariants."""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.workflow.improvements.review_subject import (
    ImprovementReviewSubjectError,
    build_review_subject,
    publish_review_subject,
)
from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context


def test_subject_digest_is_stable_for_the_same_semantic_input() -> None:
    context = _context()
    _subject, digest, data = build_review_subject(_candidate(), context, improvement_id="IMP-1")

    # Ledger version and review attempt are not builder inputs, so rebuilding
    # the same frozen Candidate/Context cannot perturb content identity.
    _rebuilt, rebuilt_digest, rebuilt_data = build_review_subject(
        _candidate(), context, improvement_id="IMP-1"
    )

    assert rebuilt_digest == digest
    assert rebuilt_data == data


def test_subject_rejects_unresolvable_source_before_publication() -> None:
    candidate = _candidate(source_refs={"problem_ids": ["PROB-MISSING"]})

    with pytest.raises(ImprovementReviewSubjectError, match="unresolvable source refs"):
        build_review_subject(candidate, _context(), improvement_id="IMP-1")


def test_publish_is_immutable_and_idempotent(tmp_path) -> None:
    _subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")

    path = publish_review_subject(tmp_path, digest, data)
    assert publish_review_subject(tmp_path, digest, data) == path
    assert path.read_bytes() == data

    with pytest.raises(ImprovementReviewSubjectError, match="do not match"):
        publish_review_subject(tmp_path, digest, data + b" ")

    assert sha256_bytes(path.read_bytes()) == digest
