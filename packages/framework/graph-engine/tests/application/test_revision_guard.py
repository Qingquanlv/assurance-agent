from __future__ import annotations

import pytest

from graph_engine.application.revision_guard import RevisionMismatch, require_revision


def test_revision_guard_reports_required_deployment() -> None:
    with pytest.raises(RevisionMismatch, match="a{64}"):
        require_revision(required="a" * 64, installed="b" * 64)


def test_matching_revision_is_accepted() -> None:
    assert require_revision(required="a" * 64, installed="a" * 64) == "a" * 64
