"""compute-constraint-coverage — marker+passed+fresh+B2 strong (Task 6 / §5-A2)."""

from __future__ import annotations

from assurance_agent.verification.property_scan import PropertyMarkerHit, extract_property_markers
from assurance_agent.workflow.execution.results import PropertyTestResult
from assurance_agent.workflow.metrics.constraint_coverage import compute_constraint_coverage

CHANGE_ID = "CH-A2-001"
BATCH = "20260805-100000"
KEY = "entities.dept.constraints.name_unique"
STRONG_SOURCE = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_duplicate_name_rejected():
    response = {"code": 400, "detail": "name exists"}
    assert response["code"] == 400
    assert response["detail"] == "name exists"
"""
WEAK_SOURCE = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_duplicate_name_rejected():
    assert 200 == 200
"""

STRONG_FAILED_SOURCE = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_strong_but_failed():
    response = {"code": 500, "detail": "server error"}
    assert response["code"] == 400
    assert response["detail"] == "name exists"
"""

WEAK_PASSED_SOURCE = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_weak_but_passed():
    assert True
"""


def _hit(file: str = "tests/api/test_props.py") -> PropertyMarkerHit:
    return PropertyMarkerHit(
        file=file,
        test_name="test_duplicate_name_rejected",
        constraint_keys=(KEY,),
        lineno=4,
    )


def _passed(*, batch_id: str = BATCH) -> PropertyTestResult:
    return PropertyTestResult(
        nodeid="tests/api/test_props.py::test_duplicate_name_rejected",
        file="tests/api/test_props.py",
        constraint_keys=(KEY,),
        outcome="passed",
        batch_id=batch_id,
    )


def test_covered_requires_marker_passed_fresh_and_strong_oracle() -> None:
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(_passed(),),
        marker_hits=(_hit(),),
        test_sources={"tests/api/test_props.py": STRONG_SOURCE},
    )
    assert evidence.declared is not None
    assert evidence.declared.covered == 1
    assert evidence.value == 1.0
    assert evidence.collection_gaps == ()


def test_weak_oracle_does_not_count_as_covered() -> None:
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(_passed(),),
        marker_hits=(_hit(),),
        test_sources={"tests/api/test_props.py": WEAK_SOURCE},
    )
    assert evidence.declared is not None
    assert evidence.declared.covered == 0
    assert evidence.value == 0.0


def test_passed_and_strong_oracle_must_belong_to_same_property_test() -> None:
    """Catch joining a passed weak test with a failed strong test by key alone."""
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            PropertyTestResult(
                nodeid="tests/api/test_strong.py::test_strong_but_failed",
                file="tests/api/test_strong.py",
                constraint_keys=(KEY,),
                outcome="failed",
                batch_id=BATCH,
            ),
            PropertyTestResult(
                nodeid="tests/api/test_weak.py::test_weak_but_passed",
                file="tests/api/test_weak.py",
                constraint_keys=(KEY,),
                outcome="passed",
                batch_id=BATCH,
            ),
        ),
        marker_hits=(
            PropertyMarkerHit(
                file="tests/api/test_strong.py",
                test_name="test_strong_but_failed",
                constraint_keys=(KEY,),
                lineno=4,
            ),
            PropertyMarkerHit(
                file="tests/api/test_weak.py",
                test_name="test_weak_but_passed",
                constraint_keys=(KEY,),
                lineno=4,
            ),
        ),
        test_sources={
            "tests/api/test_strong.py": STRONG_FAILED_SOURCE,
            "tests/api/test_weak.py": WEAK_PASSED_SOURCE,
        },
    )

    assert evidence.declared is not None
    assert evidence.declared.covered == 0
    assert evidence.value == 0.0


def test_class_qualified_property_identity_prevents_cross_class_oracle_join() -> None:
    """Same-named methods in different test classes are distinct pytest tests."""
    source = f'''
import pytest

class TestStrongFailed:
    @pytest.mark.property("{KEY}")
    def test_same_name(self):
        response = {{"detail": "wrong"}}
        assert response["detail"] == "expected"

class TestWeakPassed:
    @pytest.mark.property("{KEY}")
    def test_same_name(self):
        assert True
'''
    hits = extract_property_markers(source, file="tests/api/test_classes.py")

    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            PropertyTestResult(
                nodeid="tests/api/test_classes.py::TestStrongFailed::test_same_name",
                file="tests/api/test_classes.py",
                constraint_keys=(KEY,),
                outcome="failed",
                batch_id=BATCH,
            ),
            PropertyTestResult(
                nodeid="tests/api/test_classes.py::TestWeakPassed::test_same_name",
                file="tests/api/test_classes.py",
                constraint_keys=(KEY,),
                outcome="passed",
                batch_id=BATCH,
            ),
        ),
        marker_hits=hits,
        test_sources={"tests/api/test_classes.py": source},
    )

    assert evidence.declared is not None
    assert evidence.declared.covered == 0
    assert evidence.value == 0.0


def test_stale_batch_is_not_fresh() -> None:
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept"}),
        property_tests=(_passed(batch_id="older-batch"),),
        marker_hits=(_hit(),),
        test_sources={"tests/api/test_props.py": STRONG_SOURCE},
    )
    assert evidence.declared is not None
    assert evidence.declared.covered == 0


def test_touched_entity_without_constraints_emits_typed_gap() -> None:
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY}),
        touched_entities=frozenset({"dept", "ghost"}),
        property_tests=(_passed(),),
        marker_hits=(_hit(),),
        test_sources={"tests/api/test_props.py": STRONG_SOURCE},
    )
    subjects = {gap.subject for gap in evidence.collection_gaps}
    assert "ghost" in subjects
    assert all(gap.code == "entity_without_constraints" for gap in evidence.collection_gaps)
    # Partial metric still evaluates over the declared set.
    assert evidence.value == 1.0
