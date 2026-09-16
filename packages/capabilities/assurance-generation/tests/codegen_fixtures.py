from __future__ import annotations

from typing import Any

from graph_engine.plugin_api import CandidateFile, CandidateWriteSet, ResourceClaims, ValidationContext

from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    family_case_id,
    fake_agent_result as planning_agent_result,
    plan_input,
    reviewed_cases,
)

_SHA = "a" * 64
FAMILY_TEST_ROOTS = {
    "api": "tests/api",
    "e2e": "tests/e2e",
    "fuzz": "tests/fuzz",
    "performance": "tests/perf",
}
FAMILY_TEST_FILES = {
    "api": "tests/api/test_users.py",
    "e2e": "tests/e2e/test_users.py",
    "fuzz": "tests/fuzz/test_users.py",
    "performance": "tests/perf/test_users.py",
}


def family_test_file(family: str) -> str:
    # keep the same leaf names as today (tests/api/test_*.py etc.)
    return FAMILY_TEST_FILES[family]


def locked_oracle_paths(family: str, module: str = "items") -> tuple[str, str]:
    directory = "perf" if family == "performance" else family
    return (
        f"qa/tests/{directory}/{module}/test_{module.rsplit('/', 1)[-1]}.py",
        f"qa/tests/testdata/{directory}/{module}.py",
    )


def _is_testdata_path(path: str) -> bool:
    return path.startswith("qa/tests/testdata/")


def durable_oracle_path(target: str | None = None, family: str = "api") -> str:
    return target or locked_oracle_paths(family)[0]


def family_symbol(family: str) -> str:
    return f"test_{family_case_id(family).lower()}__happy_path"


def mapping_document(family: str, *, target_file: str | None = None) -> dict[str, Any]:
    path = target_file or durable_oracle_path(family=family)
    return {
        "schema_version": "1",
        "layer": family,
        "entries": [
            {
                "case_id": family_case_id(family),
                "symbol": family_symbol(family),
                "target_file": path,
            }
        ],
    }


def fake_agent_result(
    structured_result: dict[str, Any],
    *,
    include_scope_case_ids: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    result = planning_agent_result(structured_result, **kwargs)
    mapping = structured_result.get("mapping")
    if include_scope_case_ids and isinstance(mapping, dict) and isinstance(mapping.get("entries"), list):
        result["scope_case_ids"] = sorted(
            {item["case_id"] for item in mapping["entries"] if isinstance(item, dict) and "case_id" in item}
        )
    return result


def codegen_result(
    files: list[str],
    *,
    family: str = "api",
    required_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    case_id = family_case_id(family)
    mapping_target = next(
        (path for path in files if not _is_testdata_path(path)),
        durable_oracle_path(family=family),
    )
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "layer": family,
        "files": [
            {
                "repo_path": path,
                "disposition": "generated",
                "role": "support" if _is_testdata_path(path) else "test_entry",
                "case_ids": [] if _is_testdata_path(path) else [case_id],
            }
            for path in files
        ],
        "mapping": mapping_document(family, target_file=mapping_target),
        "required_capabilities": required_capabilities or ["entities.item.create"],
    }


def codegen_input(family: str) -> dict[str, Any]:
    payload = plan_input(family)
    return {
        "change_id": payload["change_id"],
        "plan_digest": payload["plan_digest"],
        "plan_ref": payload["plan_ref"],
        "capability_leafs": payload["capability_leafs"],
        "reviewed_cases": reviewed_cases(family),
        "family_constraints": payload["family_constraints"],
    }


def generated_candidate(family: str, extra_file: str | None = None) -> CandidateWriteSet:
    if family not in FAMILY_TEST_FILES:
        raise ValueError(f"unknown generation family: {family}")
    listed = [extra_file] if extra_file is not None else [durable_oracle_path(family=family)]
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in listed),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="capabilities-test",
        task_id="capabilities-task",
        graph_instance_id="capabilities-graph",
        node_id="capabilities-node",
        resources=ResourceClaims(),
    )


def candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


__all__ = [
    "FAMILIES",
    "FAMILY_TEST_FILES",
    "FAMILY_TEST_ROOTS",
    "VALID_LEAFS",
    "candidate_with",
    "codegen_input",
    "codegen_result",
    "family_case_id",
    "family_symbol",
    "durable_oracle_path",
    "locked_oracle_paths",
    "family_test_file",
    "fake_agent_result",
    "generated_candidate",
    "mapping_document",
    "validation_context",
]
