from pathlib import Path

import pytest

from assurance_agent.workflow.graph.plan_output_validation import (
    PlanOutputValidationError,
    validate_fuzz_plan_outputs,
)


_CASE_YAML = """\
schema_version: "1.0"
added:
  - case_id: TC_DEPT_FUZZ_001
    title: Create department payload
    status: active
    priority: P1
    severity: major
    type: Fuzz
    module: dept
    automation:
      required: true
modified: []
removed: []
"""

_VALID_PLAN = """\
## Test Function Mapping

| Case ID | Test Function | Target File | Schema Acquisition |
|---|---|---|---|
| TC_DEPT_FUZZ_001 | `test_tc_dept_fuzz_001__create_payload` | `tests/fuzz/test_dept_fuzz.py` | `from_url: /openapi.json` |
"""


def _write_inputs(change_dir: Path, *, authored: str, codegen: str) -> None:
    case_path = change_dir / "cases" / "dept" / "case.yaml"
    case_path.parent.mkdir(parents=True)
    case_path.write_text(_CASE_YAML, encoding="utf-8")
    plans = change_dir / "plans"
    plans.mkdir()
    (plans / "fuzz-plan.md").write_text(authored, encoding="utf-8")
    (plans / "fuzz-codegen-plan.md").write_text(codegen, encoding="utf-8")


def test_accepts_identical_independently_parseable_fuzz_plans(tmp_path: Path) -> None:
    _write_inputs(tmp_path, authored=_VALID_PLAN, codegen=_VALID_PLAN)

    validate_fuzz_plan_outputs(tmp_path)


def test_rejects_schema_procedure_without_case_mapping(tmp_path: Path) -> None:
    invalid = (
        _VALID_PLAN.replace(" | Schema Acquisition", "").replace(" | `from_url: /openapi.json`", "")
        + "\n## Schema Acquisition\n\n```text\nGET /openapi.json\n```\n"
    )
    _write_inputs(tmp_path, authored=invalid, codegen=invalid)

    with pytest.raises(PlanOutputValidationError, match="missing_schema_acquisition"):
        validate_fuzz_plan_outputs(tmp_path)


def test_rejects_relations_that_differ_between_the_two_plans(tmp_path: Path) -> None:
    different = _VALID_PLAN.replace("test_dept_fuzz.py", "test_other_fuzz.py")
    _write_inputs(tmp_path, authored=_VALID_PLAN, codegen=different)

    with pytest.raises(PlanOutputValidationError, match="must contain the same"):
        validate_fuzz_plan_outputs(tmp_path)
