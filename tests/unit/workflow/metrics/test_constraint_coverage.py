"""compute-constraint-coverage — marker+passed+fresh+B2 strong (Task 6 / §5-A2)."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.verification.property_scan import PropertyMarkerHit, extract_property_markers
from assurance_agent.workflow.execution.results import PropertyTestResult
from assurance_agent.workflow.metrics.constraint_coverage import (
    compute_constraint_coverage,
    compute_constraint_coverage_operation,
)

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


_DATA_KNOWLEDGE = """
version: 1
entities:
  dept:
    constraints:
      name_unique: true
  role:
    constraints:
      name_unique: true
"""


def _case_yaml(module: str) -> str:
    return f"""
schema_version: "1"
added:
  - case_id: TC_SCOPE_API_001
    title: current change scope
    status: active
    priority: P1
    severity: major
    type: API
    module: {module}
modified: []
removed: []
"""


def _operation_inputs(tmp_path: Path, *, module: str) -> tuple:
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
    from assurance_agent.workflow.graph.workspace import TaskWorkspace
    from tests.helpers_aa import write_aa_config

    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    case_dir = change_dir / "cases" / "current"
    case_dir.mkdir(parents=True)
    (project_root / ".aa" / "data-knowledge.yaml").write_text(
        _DATA_KNOWLEDGE,
        encoding="utf-8",
    )
    (case_dir / "case.yaml").write_text(_case_yaml(module), encoding="utf-8")
    task = ExecutableTask.model_construct(
        task_id="t1",
        node_id="compute-constraint-coverage",
        graph_id="assurance",
        target="operation:compute-constraint-coverage",
        input={"with": {"batch_id": BATCH}},
    )
    workspace = TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )
    context = RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={},
    )
    return task, workspace, context, change_dir


def _read_operation_evidence(change_dir: Path) -> dict:
    path = change_dir / "execution" / "runs" / BATCH / "constraint-coverage.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_operation_derives_touched_constraints_from_this_changes_cases(tmp_path: Path) -> None:
    task, workspace, context, change_dir = _operation_inputs(tmp_path, module="system.role")

    result = compute_constraint_coverage_operation(task, workspace, context)

    assert result.status == "succeeded"
    evidence = _read_operation_evidence(change_dir)
    assert evidence["declared"]["total"] == 2
    assert evidence["touched"] == {
        "total": 1,
        "covered": 0,
        "uncovered": ["entities.role.constraints.name_unique"],
        "value": 0.0,
    }
    assert evidence["collection_gaps"] == []


def test_operation_does_not_assign_global_constraints_to_an_unmodelled_entity(
    tmp_path: Path,
) -> None:
    task, workspace, context, change_dir = _operation_inputs(tmp_path, module="system.user")

    result = compute_constraint_coverage_operation(task, workspace, context)

    assert result.status == "succeeded"
    evidence = _read_operation_evidence(change_dir)
    assert evidence["touched"] == {"total": 0, "covered": 0, "uncovered": [], "value": None}
    assert [(gap["code"], gap["subject"]) for gap in evidence["collection_gaps"]] == [
        ("entity_without_constraints", "user")
    ]
